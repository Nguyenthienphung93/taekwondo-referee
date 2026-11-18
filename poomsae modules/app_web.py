
# Access shared modules while retaining project-root data paths.
from pathlib import Path as _ProjectPath
import sys as _project_sys
_PROJECT_ROOT = _ProjectPath(__file__).resolve().parent.parent
_project_sys.path.insert(0, str(_PROJECT_ROOT / "shared modules"))

# app_web.py
# Web version for Ken's Poomsae Judge Control Panel
# Run: pip install flask supabase && python app_web.py
# Open: http://127.0.0.1:5000

import os
from typing import Any, Dict, Optional, Tuple
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for, send_from_directory, g, abort
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from supabase import create_client
import json

# =========================
# SUPABASE CONFIG
# =========================
# Khuyến nghị khi deploy web: set biến môi trường SUPABASE_URL và SUPABASE_KEY.
# Nếu chạy local thì file vẫn dùng sẵn cấu hình cũ giống bản Kivy Ken gửi.
# This web app shares the DESKTOP event configuration with Poomsae-Score.
# Resolve all resources from the project root, not from the current working directory.
from dotenv import load_dotenv
ROOT = _PROJECT_ROOT
load_dotenv(ROOT / '.env', override=False)
SUPABASE_URL = (os.getenv('SUPABASE_URL') or '').strip()
SUPABASE_KEY = (os.getenv('SUPABASE_DESKTOP_KEY') or
                os.getenv('SUPABASE_KEY') or
                os.getenv('SUPABASE_PUBLIC_KEY') or
                os.getenv('SUPABASE_PUBLISHABLE_KEY') or '').strip()
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError('Judge Web: missing SUPABASE_URL or SUPABASE_DESKTOP_KEY in root .env')

def read_event():
    path = ROOT / 'Setup' / 'event.json'
    with path.open('r', encoding='utf-8-sig') as stream:
        return json.load(stream)

from datetime import datetime

# Judge web uses request-session event scope, never desktop-global event selection.
_raw_supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class JudgeScopedQuery:
    def __init__(self, builder, table):
        self.builder = builder
        self.table = table
        self.operation = 'select'
        self.filtered = False

    def _event(self):
        value = str(_judge_context().get('event_code') or '').strip()
        if not value:
            raise RuntimeError('Judge session missing Event Code. Please log in again.')
        return value

    def select(self, *args, **kwargs):
        self.builder = self.builder.select(*args, **kwargs)
        return self

    def insert(self, records, *args, **kwargs):
        self.operation = 'insert'
        event = self._event()
        items = records if isinstance(records, list) else [records]
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Invalid scoring payload')
            previous = str(item.get('event_code') or '').strip()
            if previous and previous != event:
                raise ValueError('Score Event Code mismatch')
            item['event_code'] = event
        self.builder = self.builder.insert(records, *args, **kwargs)
        return self

    def update(self, fields, *args, **kwargs):
        self.operation = 'update'
        if 'event_code' in fields and str(fields['event_code']) != self._event():
            raise ValueError('Cannot change Event Code')
        self.builder = self.builder.update(fields, *args, **kwargs)
        return self

    def upsert(self, records, *args, **kwargs):
        event = self._event()
        keys = str(kwargs.get('on_conflict') or '').replace(' ', '').split(',')
        if 'event_code' not in keys:
            raise ValueError('Upsert requires event_code in conflict key')
        items = records if isinstance(records, list) else [records]
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Invalid scoring payload')
            if str(item.get('event_code') or event) != event:
                raise ValueError('Event Code mismatch')
            item['event_code'] = event
        self.builder = self.builder.upsert(records, *args, **kwargs)
        self.operation = 'upsert'
        return self

    def delete(self, *args, **kwargs):
        self.builder = self.builder.delete(*args, **kwargs)
        self.operation = 'delete'
        return self

    def eq(self, column, value):
        if column == 'event_code':
            if str(value) != self._event():
                raise ValueError('Event Code mismatch')
            self.filtered = True
        self.builder = self.builder.eq(column, value)
        return self

    def execute(self, *args, **kwargs):
        if not self.filtered and self.operation not in ('insert', 'upsert'):
            self.builder = self.builder.eq('event_code', self._event())
            self.filtered = True
        return self.builder.execute(*args, **kwargs)

    def __getattr__(self, name):
        attr = getattr(self.builder, name)
        if not callable(attr):
            return attr
        def chain(*args, **kwargs):
            result = attr(*args, **kwargs)
            if result is not None:
                self.builder = result
            return self
        return chain

class JudgeScopedClient:
    SCOPED = frozenset(('Poomsae Matchs', 'Judge Score', 'Result', 'Breaking Score',
                        'Match Prepare', 'Information Lists', 'sigma_divisions'))
    def table(self, name):
        query = _raw_supabase.table(name)
        return JudgeScopedQuery(query, name) if name in self.SCOPED else query
    def __getattr__(self, name):
        return getattr(_raw_supabase, name)

supabase = JudgeScopedClient()

# Flask application must exist before any @app.route decorator is evaluated.
# Keep this server independent from the desktop window and its Tkinter thread.
app = Flask(__name__)
app.secret_key = (os.getenv('FLASK_SECRET_KEY') or __import__('secrets').token_urlsafe(48))
app.config['SESSION_COOKIE_NAME'] = 'poomsae_judge_session'
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

# Each browser tab receives its own signed credential: Flask cookies are shared
# between tabs, so they cannot identify judges independently.
_JUDGE_TOKEN = URLSafeTimedSerializer(app.secret_key, salt='poomsae-judge-tab-v1')
_JUDGE_TOKEN_SECONDS = 12 * 60 * 60

def _verify_judge_token(raw):
    if not raw:
        return None
    try:
        claims = _JUDGE_TOKEN.loads(str(raw), max_age=_JUDGE_TOKEN_SECONDS)
        if (isinstance(claims, dict) and
            claims.get('judge_id') in {f'Judge {i}' for i in range(1,8)} and
            claims.get('court_id') in {f'Court {i}' for i in range(0,7)} and
            str(claims.get('event_code') or '').strip()):
            return claims
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        pass
    return None

def _judge_context():
    # Authorization for API calls is the per-tab signed identity, never the
    # shared browser cookie when a tab token is present.
    raw = request.headers.get('X-Judge-Session') or request.args.get('judge_session')
    if raw:
        return _verify_judge_token(raw) or {}
    return {}  # A new tab must log in; never inherit a different tab via cookie.

@app.after_request
def _judge_security_headers(response):
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response




def event_options():
    '''All events sorted chronologically by starting date, most recent first.'''
    rows = []
    offset = 0
    while True:
        batch = (_raw_supabase.table('sigma_events')
                 .select('event_code,event_name,setup,registration_open_at,updated_at')
                 .range(offset, offset + 499).execute().data or [])
        rows.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    def event_date(row):
        setup = row.get('setup') if isinstance(row.get('setup'), dict) else {}
        dates = (setup.get('date_from'), setup.get('start_date'), setup.get('date'),
                 row.get('registration_open_at'), row.get('updated_at'))
        for value in dates:
            if not value:
                continue
            raw = str(value).strip()
            for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d',
                        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S%z'):
                try:
                    return datetime.strptime(raw[:19] if fmt == '%Y-%m-%dT%H:%M:%S' else raw, fmt).date()
                except (ValueError, TypeError):
                    pass
            try:
                return datetime.fromisoformat(raw.replace('Z', '+00:00')).date()
            except ValueError:
                pass
        return datetime.min.date()

    records = []
    for row in rows:
        code = str(row.get('event_code') or '').strip()
        if not code:
            continue
        when = event_date(row)
        records.append({'code': code, 'name': str(row.get('event_name') or code),
                        'date': when.strftime('%d/%m/%Y') if when != datetime.min.date() else '',
                        '_sort': when})
    # Events that are coming up soon go first (nearest future date first).
    # Events already in the past follow, most recent past event first.
    # Keep events with unknown dates at the bottom.
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).date()
    def nearest_event_key(record):
        start = record['_sort']
        if start == datetime.min.date():
            return (2, 0, record['code'])
        days = (start - today).days
        return (0, days, record['code']) if days >= 0 else (1, -days, record['code'])
    records.sort(key=nearest_event_key)
    for r in records:
        r.pop('_sort', None)
    return records


def judge_login_password(event_code):
    '''Read password for selected event, not the desktop's current event.'''
    rows = (_raw_supabase.table('sigma_events').select('connect_setup')
            .eq('event_code', event_code).limit(1).execute().data or [])
    if rows and isinstance(rows[0].get('connect_setup'), dict):
        password = str(rows[0]['connect_setup'].get('password') or '').strip()
        if password:
            return password
    path = ROOT / 'Setup' / 'connect_setup.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and str(data.get('password') or '').strip():
            return str(data['password']).strip()
    except (OSError, ValueError, TypeError):
        pass
    # If neither event nor local configuration has a password, fail closed.
    raise RuntimeError('Event has no Judge password configured.')

SIGMA_EVENTS_TABLE = "sigma_events"


# =========================
# LOGIC GIỐNG FILE KIVY
# =========================
def shorten_full_name(full_name: str) -> str:
    full_name = str(full_name or "").strip()
    if not full_name:
        return ""
    names = full_name.split()
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0][0]}. {names[1]}"
    family = names[0]
    given = names[-1]
    middle_initials = ".".join([m[0] for m in names[1:-1]])
    return f"{family[0]}.{middle_initials}. {given}"


def get_active_match(schedule_table: str, judge_id: str) -> Optional[Dict[str, Any]]:
    res = supabase.table(schedule_table).select("*").execute()
    court = str(_judge_context().get('court_id') or '').replace('Court', '').strip()
    for row in res.data or []:
        if court and str(row.get('Court') or '').replace('Court', '').strip() != court:
            continue
        judge_status = str(row.get(judge_id) or "").strip().lower()
        action = str(row.get("Action") or "").strip().lower()
        if action not in ["start", "ready"]:
            continue
        if judge_status == "finish":
            continue
        valid_status = [
            "start",
            "ready",
            "finish 1",

            "blue 1 ready",
            "red 1 ready",
            "blue 2 ready",
            "red 2 ready",

            "poom 1 ready",
            "poom 2 ready",
        ]
        if judge_status in valid_status:
            return row
    return None


def get_match_time(schedule_table: str, match_no: str) -> Tuple[int, int, str]:
    res = supabase.table(schedule_table).select("*").eq("Match", match_no).limit(1).execute()
    if not res.data:
        return 0, 0, ""
    row = res.data[0]
    return int(row.get("Time Minutes") or 0), int(row.get("Time Seconds") or 0), str(row.get("Control time") or "").strip().lower()


def send_judge_score(data: Dict[str, Any]):
    return supabase.table("Judge Score").insert(data).execute()


def mark_judge_finish(schedule_table: str, match_no: str, judge_id: str, status: str):
    return supabase.table(schedule_table).update({judge_id: status}).eq("Match", match_no).execute()


def clear_control_time(schedule_table: str, match_no: str):
    return supabase.table(schedule_table).update({"Control time": None}).eq("Match", match_no).execute()


def resolve_ready(row: Dict[str, Any], judge_status: str) -> Dict[str, str]:
    judge_status = str(judge_status or "").strip().lower()
    poomsae_type = str(row.get("Poomsae Type") or "").strip().lower()
    current = str(row.get("Current Poomsae") or "").strip().lower()

    p1_action = str(row.get("Poomsae 1 action") or "").strip().lower()
    p2_action = str(row.get("Poomsae 2 action") or "").strip().lower()

    if judge_status == "finish 1":
        return {
            "slot": "",
            "poomsae": "Waiting Poomsae 2 Ready..."
        }

    action = str(row.get("Action") or "").strip().lower()

    # Demon / Freestyle chỉ được hiện bài khi đã Ready thật sự
    if poomsae_type == "freestyle":
        if action == "ready" or current or p1_action == "ready" or "ready" in judge_status:
            return {
                "slot": "1",
                "poomsae": "1 - Free"
            }
        return {
            "slot": "",
            "poomsae": ""
        }

    if poomsae_type == "demonstration":
        if action == "ready" or current or p1_action == "ready" or "ready" in judge_status:
            return {
                "slot": "1",
                "poomsae": "1 - Demon"
            }
        return {
            "slot": "",
            "poomsae": ""
        }

    special_poomsae = {
        "freestyle breaking":
            "1 - Freestyle Breaking",

        "vertical spinning kicks":
            "1 - Vertical Spinning Kicks",

        "jumping multiple kicks":
            "1 - Jumping Multiple Kicks",
    }

    if poomsae_type in special_poomsae:

        if (
            action == "ready"
            or current
            or p1_action == "ready"
            or "ready" in judge_status
        ):
            return {
                "slot": "1",
                "poomsae":
                    special_poomsae[poomsae_type]
            }

        return {
            "slot": "",
            "poomsae": ""
        }

    # Ưu tiên Current Poomsae trước
    if "2" in current:
        return {
            "slot": "2",
            "poomsae": str(row.get("Poomsae 2") or "")
        }

    if "1" in current:
        return {
            "slot": "1",
            "poomsae": str(row.get("Poomsae 1") or "")
        }

    # Nếu Current Poomsae chưa rõ thì xét action
    if p2_action == "ready":
        return {
            "slot": "2",
            "poomsae": str(row.get("Poomsae 2") or "")
        }

    if p1_action == "ready":
        return {
            "slot": "1",
            "poomsae": str(row.get("Poomsae 1") or "")
        }

    # Sau cùng mới xét Judge status
    if "2" in judge_status:
        return {
            "slot": "2",
            "poomsae": str(row.get("Poomsae 2") or "")
        }

    if "1" in judge_status or judge_status == "ready":
        return {
            "slot": "1",
            "poomsae": str(row.get("Poomsae 1") or "")
        }

    return {
        "slot": "",
        "poomsae": "Waiting Poomsae Ready..."
    }


def detect_screen(row: Dict[str, Any]) -> str:

    poomsae_type = str(
        row.get("Poomsae Type") or ""
    ).strip().lower()

    method = str(
        row.get("Method") or ""
    ).strip().lower()

    division = str(
        row.get("Division") or ""
    ).strip().lower()


    if poomsae_type == "freestyle":
        return "freestyle_tech"


    if poomsae_type == "demonstration":
        return "demonstration"


    # =====================================
    # NEW SPECIAL EVENTS
    # PHẢI nằm trước Cut Off
    # =====================================

    if poomsae_type == "freestyle breaking":
        return "freestyle_breaking"


    if poomsae_type == "vertical spinning kicks":
        return "vertical_spinning_kicks"


    if poomsae_type == "jumping multiple kicks":
        return "jumping_multiple_kicks"


    if (
        method in ["cut off", "cutoff"]
        or "cut" in method
    ):
        return "recognized_cutoff"


    if (
        "pair" in division
        or "team" in division
    ):
        return "recognized_obo"


    return "recognized_sbs"


def clean_noc(noc: str) -> str:
    noc = str(noc or "").strip().upper()
    return noc.split()[0] if " " in noc else noc

# =========================
# HTML/CSS/JS WEB UI
# =========================
PAGE = r'''
<!doctype html>
<html lang="en">
<head>
<link rel="manifest" href="{{ request.script_root }}/manifest.json">
<!-- Judge controller favicon: route relative to Judge Web mount point. -->
<link rel="icon" type="image/png" sizes="any" href="{{ request.script_root }}/favicon.png?v=judge2">
<link rel="shortcut icon" href="{{ request.script_root }}/favicon.ico?v=judge2">
<link rel="apple-touch-icon" href="{{ request.script_root }}/apple-touch-icon.png?v=judge2">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Poomsae Scoring Web</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Poomsae Score">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">

<style>
html, body{
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
}
</style>
<style>
:root{--bg:#05070d;--panel:#101827;--gold:#ffd447;--blue:#0a63d8;--blue2:#102d59;--red:#be1720;--red2:#4b0608;--green:#00c853;--muted:#9fb1c8}
html,body{
    width:100%;
    height:100%;
    overflow:hidden;
    overscroll-behavior:none;
    touch-action:manipulation;
}

*{box-sizing:border-box}

body{
    margin:0;
    background:#02040a;
    color:white;
    font-family:Arial,Helvetica,sans-serif;
    overflow:hidden;
}

/* Judge login background: file is loaded from project-root icons/TKD_web.png. */
.page.center{
    background-image:linear-gradient(rgba(2,7,18,.55),rgba(2,7,18,.74)),url('{{ request.script_root }}/judge-login-background');
    background-size:cover;
    background-position:center center;
    background-repeat:no-repeat;
}
.page.center .login-card{
    background:rgba(3,10,22,.84);
    border:1px solid rgba(165,203,255,.18);
    backdrop-filter:blur(7px);
}
.page.center .login-card select{
    text-overflow:ellipsis;
    font-size:clamp(14px,2.3vw,21px);
}
.page{
    height:100vh;
    width:100vw;
    background:radial-gradient(circle at 50% 0%,#102340 0,#05070d 48%,#020308 100%);
    padding:26px;
    display:flex;
    flex-direction:column;
    gap:18px;
}

.center{display:flex;align-items:center;justify-content:center}
.login-card{
    width:min(570px,92vw);
    padding:clamp(14px,2vw,36px);
    border-radius:clamp(18px,2vw,28px);
    background:rgba(3,10,22,.72);
    box-shadow:0 30px 80px rgba(0,0,0,.55);
    border:1px solid rgba(255,255,255,.08);
}
.logo{
    font-size:clamp(28px,5vw,44px);
    font-weight:900;
    color:var(--gold);
    text-align:center;
    letter-spacing:1px;
}
.sub{
    text-align:center;
    font-size:clamp(16px,2.5vw,22px);
    font-weight:800;
    margin:8px 0 20px;
}
.field{margin:16px 0}
.field label{
    display:block;
    color:var(--gold);
    font-size:clamp(14px,2vw,18px);
    font-weight:900;
    margin-bottom:6px;
    text-align:center;
}

select,input{
    width:100%;
    height:clamp(46px,9vh,68px);
    border:0;
    border-radius:clamp(10px,1.5vw,16px);
    background:#111a2a;
    color:white;
    font-size:clamp(18px,3vw,28px);
    font-weight:900;
    text-align:center;
    padding:0 12px;
}

.btn{border:0;border-radius:24px;color:white;font-weight:900;cursor:pointer;box-shadow:inset 0 -4px 0 rgba(0,0,0,.25)}
.btn-main{
    width:100%;
    height:clamp(54px,10vh,86px);
    font-size:clamp(22px,3vw,32px);
    background:#00a8ff;
    margin-top:18px;
}
.logout{position:fixed;right:12px;top:12px;z-index:99;background:rgba(40,40,40,.45);height:38px;border-radius:13px;padding:0 18px;color:rgba(255,255,255,.75);border:0;font-weight:800}

.top-title{text-align:center;font-size:30px;font-weight:900;color:#2fd8ff;height:54px}
.content{flex:1;display:flex;flex-direction:column;gap:16px}

.score-page{
    height:100vh;
    width:100vw;
    display:flex;
    flex-direction:column;
    background:#050505;
    overflow:auto;
}

.score-top{
    height:215px;
    display:grid;
    grid-template-columns:42% 16% 42%;
    background:#050505;
}

.top-side{padding:12px 14px 8px;display:flex;flex-direction:column;gap:6px}
.blue-bg{background:#071e3d}
.red-bg{background:#4a0404}

.score-cards{
    height:125px;
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
}

.score-card{
    border-radius:18px;
    display:flex;
    align-items:center;
    justify-content:center;
    text-align:center;
    font-size:28px;
    font-weight:900;
    white-space:pre-line;
}

.score-card.blue{background:#092a59}
.score-card.red{background:#790808}

.center-info{padding:5px 8px;display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center}
.division{width:100%;background:#1c1c1c;border-radius:16px;padding:10px 6px;font-size:24px;font-weight:900}
.timer{font-size:42px;font-weight:900;color:#ffef00}
.small{font-size:14px;font-weight:900}
.poomsae{color:var(--gold);font-weight:900;font-size:13px}

.score-bottom{
    flex:1;
    display:grid;
    grid-template-columns:33% 34% 33%;
}

.side-score{padding:50px 30px 30px;display:flex;flex-direction:column;gap:14px;align-items:stretch}
.mid-score{background:#171717;padding:28px 20px 20px;display:flex;flex-direction:column;gap:18px}

/* =========================================================
   DEDUCTION PILL BUTTON
========================================================= */

.deduct-pill{
    width:100%;

    min-height:250px;

    border:2px solid rgba(255,255,255,.14);
    border-radius:999px;

    padding:10px 12px 10px 28px;

    display:grid;
    grid-template-columns:1fr auto;
    align-items:center;
    gap:14px;

    color:#ffffff;

    cursor:pointer;

    background:linear-gradient(
        90deg,
        #0c4278 0%,
        #1266b8 52%,
        #0b4e8e 100%
    );

    text-shadow:
        0 2px 4px rgba(0,0,0,.45),
        0 0 10px rgba(255,255,255,.18);

    box-shadow:
        0 8px 20px rgba(0,0,0,.30),
        inset 0 2px 0 rgba(255,255,255,.14);

    transition:
        transform .10s ease,
        filter .12s ease,
        box-shadow .12s ease;
}

.deduct-pill:hover{
    filter:brightness(1.06);
}

.deduct-pill:active{
    transform:scale(.985);
}

.deduct-pill.red{
    background:linear-gradient(
        90deg,
        #8f0e16 0%,
        #d91e28 52%,
        #9e1118 100%
    );
}

.deduct-value{
    font-size:clamp(38px,10vw,90px);
    font-weight:900;
    line-height:1;
    text-align:center;
    letter-spacing:1px;
}

.deduct-badge{
    width:clamp(72px,10vw,150px);
    height:clamp(72px,10vw,150px);

    border-radius:50%;

    display:flex;
    align-items:center;
    justify-content:center;

    background:rgba(255,255,255,.92);

    box-shadow:
        0 4px 12px rgba(0,0,0,.26),
        inset 0 -2px 0 rgba(0,0,0,.10);

    font-size:clamp(28px,4vw,40px);
    font-weight:900;
    line-height:1;

    color:#0b4f90;
}

.deduct-pill.red .deduct-badge{
    color:#b1141c;
}

@media screen and (orientation:landscape) and (max-height:600px){
    .deduct-pill{
        min-height:150px;
        padding:8px 10px 8px 18px;
        gap:10px;
    }

    .deduct-value{
        font-size:clamp(24px,10vw,70px);
    }

    .deduct-badge{
        width:100px;
        height:100px;
        font-size:30px;
    }
}

.tech-total-row{
    display:grid;
    grid-template-columns:78px 1fr 78px;
    gap:10px;
    align-items:center;
    margin-bottom:4px;
}

.tech-total{
    height:58px;
    border-radius:18px;
    display:flex;
    align-items:center;
    justify-content:center;
    font-size:20px;
    font-weight:900;
    color:white;
    box-shadow:inset 0 -4px 0 rgba(0,0,0,.25);
}

.tech-total-title{
    text-align:center;
    font-size:19px;
    font-weight:900;
    color:#ffd447;
}

.blue-tech{
    background:#0d56a8;
}

.red-tech{
    background:#b3131d;
}


.pre-row{height:58px;display:grid;grid-template-columns:78px 1fr 78px;gap:10px;align-items:center}
.pre-row.cutoff{grid-template-columns:90px 1fr}
.pre-btn{height:58px;border-radius:18px;background:#0d3b78;color:white;font-size:20px;font-weight:900;border:0}
.pre-btn.red{background:#7a1010}
.pre-label{text-align:center;font-size:19px;font-weight:900}
.pre-row-slider{
    display:grid;
    grid-template-columns:60px 1fr 60px 1fr;
    gap:10px;
    align-items:center;
    min-height:70px;
}

.pre-slider-box{
    display:flex;
    flex-direction:column;
    gap:6px;
}

.pre-slider{
    width:100%;
    height:28px;
    accent-color:#00c853;
}

.pre-value{
    text-align:center;
    font-size:22px;
    font-weight:900;
    color:white;
}
.pre-slider{
    width:100%;
    height:34px;
    accent-color:#00c853;
}

.pre-value{
    text-align:center;
    font-size:20px;
    font-weight:900;
    color:white;
}

.send{height:105px;border-radius:24px;background:#00c853;font-size:36px;color:white;font-weight:900;border:0}
.send-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.send-blue{background:#0d73ff}
.send-red{background:#ff2933}

#appRoot{
    width:100vw;
    height:100vh;
    overflow:hidden;
}

.rotate-lock{
    position:fixed;
    inset:0;
    z-index:9999;
    display:none;
    align-items:center;
    justify-content:center;
    text-align:center;
    padding:28px;
    background:#02040a;
    color:white;
}

.rotate-lock-box{
    max-width:520px;
    border-radius:26px;
    padding:28px;
    background:rgba(16,24,39,.96);
    border:1px solid rgba(255,255,255,.12);
    box-shadow:0 30px 80px rgba(0,0,0,.6);
}

.rotate-lock-icon{font-size:64px;margin-bottom:14px}
.rotate-lock-title{font-size:30px;font-weight:900;color:var(--gold);margin-bottom:10px}
.rotate-lock-text{font-size:19px;font-weight:800;color:#c8dbf1;line-height:1.35}

@media screen and (orientation:portrait) and (max-width:900px){
    #appRoot{display:none}
    .logout{display:none}
    .rotate-lock{display:flex}
}

@media screen and (orientation:landscape) and (max-height:520px){
    body{overflow:hidden}
    .page{height:100dvh;width:100dvw;padding:10px;gap:8px}
    .score-page{height:100dvh;width:100dvw;overflow:hidden}
    .top-title{height:34px;font-size:20px}
    .content{gap:8px}
    .status-card{font-size:18px;padding:8px}
    .match-title{font-size:20px}
    .ready-row{height:calc(100dvh - 140px);gap:10px}
    .team-card{padding:12px;border-radius:18px}
    .noc{font-size:28px}
    .athlete{font-size:18px}

    .score-top{height:132px;grid-template-columns:42% 16% 42%}
    .score-cards{height:78px}
    .score-card{font-size:17px;border-radius:12px}
    .center-info{gap:2px;padding:3px 5px}
    .division{font-size:10px;padding:5px 3px;border-radius:10px}
    .timer{font-size:27px}
    .small{font-size:10px}
    .poomsae{font-size:9px}
    .top-side{padding:7px;gap:3px}
    .top-side .noc{font-size:22px!important}
    .top-side .athlete{font-size:14px!important}

    .score-bottom{
        grid-template-columns:33% 34% 33%;
        height:calc(100dvh - 132px);
    }

    .side-score{padding:16px 16px 10px;gap:5px}
    .mid-score{padding:12px 10px 8px;gap:8px}
    .deduct{height:62px;border-radius:16px;font-size:28px}
    .count{font-size:15px}
    .tech-total-row{
        height:clamp(38px,8vh,62px);
        grid-template-columns:clamp(58px,9vw,90px) 1fr clamp(58px,9vw,90px);
        gap:clamp(6px,1vw,12px);
        align-items:center;
        margin-bottom:0;
    }

    .tech-total{
        height:clamp(36px,7vh,58px)!important;
        border-radius:clamp(10px,1.8vw,18px);
        font-size:clamp(16px,2.8vw,24px)!important;
    }

    .tech-total-title{
        font-size:clamp(13px,2.4vw,22px)!important;
        line-height:1;
    }
    .pre-row{height:36px;grid-template-columns:52px 1fr 52px;gap:6px}
    .pre-row.cutoff{grid-template-columns:62px 1fr}
    .pre-btn{height:36px;border-radius:11px;font-size:14px}
    .pre-label{font-size:13px}
    .pre-row-slider{
        display:grid;
        grid-template-columns:55px 1fr 55px 1fr;
        gap:8px;
        align-items:center;
        min-height:58px;
    }

    .pre-slider-box{
        display:flex;
        flex-direction:column;
        gap:4px;
    }

    .pre-slider{
        width:100%;
        height:26px;
        accent-color:#00c853;
    }

    .pre-value{
        text-align:center;
        font-size:20px;
        font-weight:900;
        color:white;
    }
    .send{height:58px;border-radius:16px;font-size:22px}

    .fs-bottom{padding:8px 18px;gap:7px}
    .fs-title-row{height:46px;grid-template-columns:1fr 150px}
    .fs-title-row .send{height:42px!important;font-size:17px!important}
    .fs-card{padding:8px 14px;gap:4px}
    .fs-row{height:35px;grid-template-columns:210px repeat(10,1fr);gap:4px}
    .fs-row>div:first-child{font-size:13px!important}
    .num-btn{height:31px;font-size:13px;border-radius:6px}

    .demo-card{grid-template-columns:1.2fr .8fr;padding:10px 16px;gap:14px}
    .demo-row{height:42px;grid-template-columns:155px 55px 1fr;gap:7px}
    .demo-row div{font-size:14px!important}
    .demo-row input{height:28px}
    .ded-row{height:37px;grid-template-columns:58px 45px 45px 45px;gap:7px}
    .mini-btn{height:31px;font-size:16px}

    .logout{top:5px;right:5px;height:28px;padding:0 10px;font-size:11px;border-radius:9px}
}
.slider-popup{
    position:fixed;
    inset:0;
    z-index:9998;
    display:none;
    align-items:center;
    justify-content:center;
    background:rgba(0,0,0,.45);
}

.slider-popup-box{
    width:min(560px,90vw);

    border-radius:24px;

    padding:20px 26px 14px;

    background:#101827;
    border:1px solid rgba(255,255,255,.18);
    box-shadow:0 30px 80px rgba(0,0,0,.6);
}

.slider-popup-title{
    font-size:24px;
    font-weight:900;
    color:var(--gold);
    text-align:center;
    margin-bottom:10px;
}

.slider-popup-value{
    font-size:54px;
    font-weight:900;
    text-align:center;
    color:white;
    margin-bottom:18px;
}

.popup-range{
    width:100%;
    height:68px;

    background:transparent;

    -webkit-appearance:none;
    appearance:none;

    cursor:pointer;
}

/* =========================
   TRACK
========================= */

.popup-range::-webkit-slider-runnable-track{
    height:16px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );

    box-shadow:
        0 0 18px rgba(0,255,170,.35),
        inset 0 0 10px rgba(255,255,255,.18);

    border:1px solid rgba(255,255,255,.15);
}

/* =========================
   THUMB WEBKIT
========================= */

.popup-range::-webkit-slider-thumb{
    -webkit-appearance:none;
    appearance:none;

    width:44px;
    height:44px;

    border-radius:50%;

    background:
        radial-gradient(
            circle at 30% 30%,
            #ffffff,
            #9dffca 35%,
            #00e676 70%
        );

    border:4px solid #eafff4;

    margin-top:-14px;

    box-shadow:
        0 0 24px rgba(0,255,140,.95),
        0 0 50px rgba(0,255,170,.55),
        0 6px 18px rgba(0,0,0,.45);

    transition:.12s;
}

.popup-range::-webkit-slider-thumb:hover{
    transform:scale(1.08);
}

/* =========================
   FIREFOX
========================= */

.popup-range::-moz-range-track{
    height:16px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );

    box-shadow:
        0 0 18px rgba(0,255,170,.35),
        inset 0 0 10px rgba(255,255,255,.18);

    border:1px solid rgba(255,255,255,.15);
}

.popup-range::-moz-range-thumb{
    width:44px;
    height:44px;

    border-radius:50%;

    background:
        radial-gradient(
            circle at 30% 30%,
            #ffffff,
            #9dffca 35%,
            #00e676 70%
        );

    border:4px solid #eafff4;

    box-shadow:
        0 0 24px rgba(0,255,140,.95),
        0 0 50px rgba(0,255,170,.55),
        0 6px 18px rgba(0,0,0,.45);
}

/* Firefox */
.popup-range::-moz-range-track{
    height:18px;
    border-radius:999px;
    background:#243447;
}

.popup-range::-moz-range-thumb{
    width:46px;
    height:46px;
    border-radius:50%;

    background:#00c853;
    border:4px solid #eafff1;

    box-shadow:
        0 0 18px rgba(0,200,83,.65),
        0 4px 12px rgba(0,0,0,.45);
}

/* ===== Slider ruler / scale ===== */

.slider-popup-scale{
    position:relative;
    margin-top:4px;
    margin-bottom:10px;
    padding:8px 10px 0;
}

.slider-popup-scale::before{
    content:"";

    position:absolute;

    left:10px;
    right:10px;

    top:8px;

    height:1px;

    border-radius:999px;

    background:rgba(120,180,210,.35);

    box-shadow:none;
}

.slider-popup-scale-inner{
    display:grid;
    grid-template-columns:repeat(16, minmax(0,1fr));
    align-items:start;
}

.slider-popup-tick{
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:flex-start;
    gap:4px;
    min-width:0;
}

/* ===== MỐC PHỤ ===== */

.slider-popup-tick-line{
    width:1px;
    height:8px;

    border-radius:999px;

    background:rgba(150,180,205,.58);

    box-shadow:none;
}


.slider-popup-tick-label{
    margin-top:1px;

    font-size:9px;
    line-height:1;

    font-weight:500;

    color:rgba(190,205,220,.72);

    white-space:nowrap;

    letter-spacing:0;
}


/* ===== MỐC CHÍNH 0.5 / 1.0 / 1.5 / 2.0 ===== */

.slider-popup-tick.major .slider-popup-tick-line{
    width:3px;
    height:16px;

    background:#6fe8ff;

    box-shadow:
        0 0 5px rgba(80,220,255,.55),
        0 0 10px rgba(80,220,255,.18);
}


.slider-popup-tick.major .slider-popup-tick-label{
    margin-top:2px;

    font-size:11px;

    font-weight:900;

    color:#ffffff;

    text-shadow:
        0 0 5px rgba(80,220,255,.45);
}

@media screen and (orientation:landscape) and (max-height:600px){
    .slider-popup-scale{
        padding:6px 8px 0;
        margin-bottom:8px;
    }

    .slider-popup-scale::before{
        left:8px;
        right:8px;
        top:6px;
    }

    .slider-popup-tick-line{
        height:7px;
    }

    .slider-popup-tick.major .slider-popup-tick-line{
        width:3px;
        height:14px;
    }

    .slider-popup-tick-label{
        font-size:8px;
    }

    .slider-popup-tick.major .slider-popup-tick-label{
        font-size:10px;
        font-weight:900;
    }
}

.slider-popup-actions{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
    margin-top:4px;
}

.slider-popup-actions.one-btn{
    grid-template-columns:1fr;
}

.slider-popup-actions button{
    height:58px;
    border:0;
    border-radius:16px;
    font-size:22px;
    font-weight:900;
    color:white;
}

.btn-cancel{background:#555}
.btn-ok{background:#00a84f}
.flag{
    width:72px;
    height:48px;
    border-radius:8px;
    overflow:hidden;
    background:rgba(255,255,255,.08);
    border:1px solid rgba(255,255,255,.22);
    display:flex;
    align-items:center;
    justify-content:center;
    flex-shrink:0;
}

.flag img{
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
}

.flag-text{
    font-size:18px;
    font-weight:900;
    color:white;
}

.noc{
    font-size:38px;
    font-weight:900;
}

.athlete{
    font-size:21px;
    font-weight:800;
}

.score-page{
    background:#030303;
}

.score-top{
    height:220px;
    grid-template-columns:34% 32% 34%;
}

.center-info{
    background:#030303;
    justify-content:center;
}

.score-bottom{
    grid-template-columns:30% 40% 30%;
}

.mid-score{
    border-radius:0;
}

.side-score{
    justify-content:center;
}

.pre-btn{
    background:#0d56a8;
    color:white;
    box-shadow:inset 0 -4px 0 rgba(0,0,0,.25);
}

.pre-btn.red{
    background:#b3131d;
}

.send{
    background:#00c853;
    color:white;
}

/* ===== MOBILE LANDSCAPE FIXED LAYOUT ===== */
@media screen and (orientation:landscape) and (max-height:600px){

    html, body, #appRoot{
        width:100dvw;
        height:100dvh;
        overflow:hidden!important;
    }

    .score-page{
        width:100dvw;
        height:100dvh;
        display:grid;
        grid-template-rows:33dvh 67dvh;
        overflow:hidden;
    }

    .score-top{
        height:33dvh!important;
        display:grid;
        grid-template-columns:34% 32% 34%;
    }

    .top-side{
        padding:clamp(5px,1.2vw,12px);
        gap:clamp(2px,.6vh,6px);
        overflow:hidden;
    }

    .flag{
        width:clamp(42px,8vw,72px);
        height:clamp(28px,5.3vw,48px);
    }

    .noc{
        font-size:clamp(22px,4vw,38px)!important;
        line-height:1;
    }

    .athlete{
        font-size:clamp(11px,2.2vw,21px)!important;
        line-height:1.15;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }

    .center-info{
        padding:clamp(3px,.8vw,8px);
        gap:clamp(1px,.5vh,4px);
        justify-content:center;
        overflow:hidden;
    }

    .division{
        font-size:clamp(10px,1.7vw,15px);
        padding:clamp(4px,.8vw,8px);
        border-radius:clamp(8px,1.8vw,16px);
    }

    .timer{
        font-size:clamp(28px,6vw,52px);
        line-height:1;
    }

    .small{
        font-size:clamp(9px,1.6vw,14px);
        line-height:1.05;
    }

    .poomsae{
        font-size:clamp(9px,1.5vw,13px);
        line-height:1.05;
    }

    .score-cards{
        display:none!important;
    }

    .score-bottom{
        min-height:67dvh!important;
        height:auto!important;
        display:grid;
        grid-template-columns:30% 40% 30%;
        overflow:hidden;
    }

    .side-score{
        padding:clamp(8px,2vw,24px);
        gap:clamp(4px,1vh,12px);
        justify-content:center;
        overflow:hidden;
    }

    .mid-score{
        padding:clamp(8px,1.8vw,22px);
        gap:clamp(6px,1.3vh,16px);
        overflow:hidden;
        justify-content:center;
    }

    .deduct{




        height:clamp(95px,28vh,180px);
        border-radius:clamp(14px,2.4vw,24px);
        font-size:clamp(32px,7vw,58px);

        color:white;
        font-weight:900;
        text-shadow:
            0 0 8px rgba(0,0,0,.65),
            0 2px 6px rgba(0,0,0,.7);
    }

    .count{
        font-size:clamp(16px,3vw,26px);
        line-height:1;
    }

    .score-summary{
        display:grid;
        grid-template-columns:1fr 1fr;
        gap:clamp(8px,1.4vw,18px);
    }

    .score-summary-card{
        background:#070707;
        border-radius:clamp(10px,1.8vw,18px);
        text-align:center;
        padding:clamp(5px,1vw,12px);
        font-weight:900;
    }

    .score-summary-num{
        font-size:clamp(20px,4vw,34px);
        line-height:1;
    }

    .score-summary-label{
        font-size:clamp(14px,2.5vw,28px);
        line-height:1.1;
    }

    .pre-row{
        height:clamp(38px,8vh,62px);
        grid-template-columns:clamp(58px,9vw,90px) 1fr clamp(58px,9vw,90px);
        gap:clamp(6px,1vw,12px);
    }

    .pre-btn{
        height:clamp(36px,7vh,58px);
        border-radius:clamp(10px,1.8vw,18px);
        font-size:clamp(16px,2.8vw,24px);
    }

    .pre-label{
        font-size:clamp(13px,2.4vw,22px);
        line-height:1;
        white-space:nowrap;
    }

    .send{
        height:clamp(55px,12vh,105px);
        border-radius:clamp(14px,2.2vw,24px);
        font-size:clamp(24px,5vw,44px);
    }

    .logout{
        top:clamp(4px,.8vw,10px);
        right:clamp(4px,.8vw,10px);
        height:clamp(26px,5vh,38px);
        font-size:clamp(10px,1.5vw,14px);
    }
}

/* =========================================================
   DEMONSTRATION HEADER SUMMARY
========================================================= */

.demo-header-summary{
    padding:clamp(8px,1.3vw,16px);

    display:flex;
    flex-direction:column;

    align-items:center;
    justify-content:center;

    gap:clamp(5px,.8vh,9px);

    background:#050505;
}


.demo-header-top{
    width:min(330px,90%);

    display:grid;

    grid-template-columns:1fr 1fr;

    gap:clamp(10px,1.5vw,18px);
}


.demo-header-score{
    text-align:center;
}


.demo-header-label{
    color:#cfd5de;

    font-size:clamp(11px,1.6vw,16px);

    font-weight:900;
}


.demo-header-number{
    margin-top:2px;

    color:white;

    font-size:clamp(22px,3vw,34px);

    font-weight:900;

    line-height:1;
}


.demo-header-label.deduct,
.demo-header-number.deduct{
    color:#ff6363;
}


.demo-header-total{
    width:min(250px,76%);

    text-align:center;

    padding-top:4px;

    border-top:1px solid rgba(255,255,255,.15);
}


.demo-header-total-label{
    color:#ffd447;

    font-size:clamp(13px,1.8vw,19px);

    font-weight:900;
}


.demo-header-total-number{
    margin-top:1px;

    color:#ffffff;

    font-size:clamp(27px,3.8vw,42px);

    font-weight:900;

    line-height:1;
}


.demo-header-judge{
    color:#00ff75;

    font-size:clamp(15px,2vw,22px);

    font-weight:900;
}

/* =========================================================
   DEMONSTRATION - TWO STEP LAYOUT
========================================================= */

.demo-scoring-layout{
    flex:1;

    padding:clamp(10px,1.5vw,20px);

    display:grid;
    grid-template-rows:auto 1fr;

    gap:clamp(8px,1.3vh,16px);

    background:#050505;

    overflow:hidden;
}


/* ===== TITLE + SEND ===== */

.demo-scoring-header{
    display:grid;

    grid-template-columns:1fr clamp(130px,18vw,220px);

    align-items:center;

    gap:clamp(10px,1.5vw,18px);
}


.demo-scoring-title{
    color:white;

    font-size:clamp(20px,3.5vw,38px);

    font-weight:900;
}


.demo-score-send{
    width:100%;
}


/* ===== SCORE PANEL ===== */

.demo-score-panel{
    min-height:0;

    display:grid;

    grid-template-rows:repeat(4,1fr);

    gap:clamp(5px,1vh,10px);

    padding:clamp(10px,1.5vw,18px);

    background:#111;

    border-radius:clamp(14px,2vw,24px);

    overflow:auto;
}


.demo-score-row{
    display:grid;

    grid-template-columns:
        clamp(150px,22vw,280px)
        clamp(60px,8vw,100px)
        minmax(0,1fr);

    align-items:center;

    gap:clamp(8px,1.3vw,16px);
}


.demo-score-title{
    color:white;

    font-size:clamp(14px,2.2vw,24px);

    font-weight:900;

    white-space:nowrap;
}


.demo-score-value{
    color:#ffd447;

    text-align:center;

    font-size:clamp(22px,3.4vw,38px);

    font-weight:900;
}


/* ===== SLIDER + RULER ===== */

.demo-slider-wrap{
    min-width:0;

    display:flex;

    flex-direction:column;

    gap:2px;
}


.demo-score-slider{
    width:100%;

    height:44px;

    margin:0;

    background:transparent;

    -webkit-appearance:none;
    appearance:none;

    cursor:pointer;
}


/* Chrome / Edge */

.demo-score-slider::-webkit-slider-runnable-track{
    height:12px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );

    border:1px solid rgba(255,255,255,.15);

    box-shadow:
        0 0 15px rgba(0,255,170,.30),
        inset 0 0 8px rgba(255,255,255,.14);
}


.demo-score-slider::-webkit-slider-thumb{
    -webkit-appearance:none;
    appearance:none;

    width:36px;
    height:36px;

    margin-top:-13px;

    border-radius:50%;

    background:
        radial-gradient(
            circle at 30% 30%,
            #ffffff,
            #9dffca 35%,
            #00e676 70%
        );

    border:4px solid #eafff4;

    box-shadow:
        0 0 20px rgba(0,255,140,.90),
        0 0 38px rgba(0,255,170,.46),
        0 5px 14px rgba(0,0,0,.42);
}


/* Firefox */

.demo-score-slider::-moz-range-track{
    height:12px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );
}


.demo-score-slider::-moz-range-thumb{
    width:36px;
    height:36px;

    border-radius:50%;

    background:#00e676;

    border:4px solid #eafff4;
}


/* ===== DEMO RULER ===== */

.demo-scale{
    position:relative;

    width:100%;

    height:28px;

    margin-top:-2px;
}


.demo-scale-line{
    position:absolute;

    /* Thu thước vào thêm để khớp tâm slider */
    left:28px;
    right:28px;

    top:2px;

    height:1px;

    background:rgba(120,180,210,.34);
}


.demo-scale-ticks{
    position:absolute;

    /* Phải giống hệt demo-scale-line */
    left:28px;
    right:28px;

    top:2px;

    height:26px;
}


.demo-scale-tick{
    position:absolute;

    top:0;

    transform:translateX(-50%);

    display:flex;

    flex-direction:column;

    align-items:center;
}


.demo-scale-mark{
    width:1px;
    height:7px;

    background:rgba(155,185,210,.58);
}


.demo-scale-label{
    margin-top:2px;

    color:rgba(190,205,220,.68);

    font-size:8px;

    font-weight:500;

    line-height:1;

    white-space:nowrap;
}


/* Mốc chính */

.demo-scale-tick.major .demo-scale-mark{
    width:3px;

    height:14px;

    background:#6fe8ff;

    box-shadow:
        0 0 5px rgba(80,220,255,.55),
        0 0 9px rgba(80,220,255,.18);
}


.demo-scale-tick.major .demo-scale-label{
    color:#ffffff;

    font-size:10px;

    font-weight:900;

    text-shadow:
        0 0 4px rgba(80,220,255,.42);
}


/* =========================================================
   DEMONSTRATION DEDUCTION
========================================================= */

.demo-deduction-layout{
    flex:1;

    display:flex;

    flex-direction:column;

    justify-content:center;

    gap:clamp(12px,2vh,24px);

    padding:clamp(12px,2vw,28px);

    background:#050505;

    overflow:auto;
}


.demo-deduction-controls{
    width:min(1180px,92vw);

    margin:0 auto;

    display:grid;

    grid-template-columns:repeat(3,1fr);

    gap:clamp(12px,2vw,26px);
}


.demo-deduction-card{
    padding:clamp(10px,1.6vw,20px);

    background:#151515;

    border:2px solid rgba(255,60,60,.30);

    border-radius:clamp(15px,2vw,24px);

    display:grid;

    grid-template-columns:1fr auto;

    align-items:center;

    gap:clamp(8px,1.5vw,18px);
}


.demo-deduction-main{
    width:100%;

    height:clamp(90px,18vh,160px);

    border:0;

    border-radius:clamp(14px,2vw,22px);

    background:
        linear-gradient(
            180deg,
            #ef4049 0%,
            #b81720 100%
        );

    color:white;

    font-size:clamp(34px,5vw,60px);

    font-weight:900;

    cursor:pointer;

    box-shadow:
        0 7px 18px rgba(0,0,0,.35),
        inset 0 2px 0 rgba(255,255,255,.15);
}


.demo-deduction-main.medium{
    background:
        linear-gradient(
            180deg,
            #db3038,
            #971116
        );
}


.demo-deduction-main.heavy{
    background:
        linear-gradient(
            180deg,
            #bd1a22,
            #68070b
        );
}


.demo-ded-count-box{
    min-width:clamp(75px,8vw,110px);

    display:flex;

    flex-direction:column;

    align-items:center;

    gap:6px;
}


.demo-ded-count-label{
    color:#9ba2ac;

    font-size:clamp(10px,1.4vw,15px);

    font-weight:900;
}


.demo-ded-count{
    color:#ffd447;

    font-size:clamp(32px,4.5vw,54px);

    font-weight:900;

    line-height:1;
}


.demo-ded-undo{
    width:clamp(58px,6.5vw,82px);

    height:clamp(30px,5vh,42px);

    border:0;

    border-radius:9px;

    background:#30343a;

    color:#d7dbe0;

    font-weight:900;

    cursor:pointer;
}


.demo-ded-undo:disabled{
    opacity:.25;

    cursor:default;
}


/* ===== FINAL SUMMARY ===== */

.demo-final-summary{
    width:min(650px,78vw);

    margin:0 auto;

    padding:clamp(10px,1.6vw,20px);

    display:grid;

    grid-template-columns:1fr auto;

    gap:5px 28px;

    border-radius:clamp(14px,2vw,22px);

    background:#111;

    border:1px solid rgba(255,255,255,.08);
}


.demo-summary-label,
.demo-summary-value{
    font-size:clamp(14px,2vw,22px);

    font-weight:900;
}


.demo-summary-value{
    text-align:right;
}


.demo-summary-ded{
    color:#ff6565;
}


.demo-summary-total{
    margin-top:5px;

    padding-top:7px;

    border-top:1px solid rgba(255,255,255,.14);

    color:#ffd447;

    font-size:clamp(21px,3vw,34px);

    font-weight:900;
}


.demo-summary-total.value{
    text-align:right;
}


.demo-final-send-wrap{
    display:flex;

    justify-content:center;
}


.demo-final-send{
    width:min(420px,55vw);

    height:clamp(58px,11vh,88px);

    border:0;

    border-radius:clamp(15px,2vw,22px);

    background:#00c853;

    color:white;

    font-size:clamp(24px,4vw,38px);

    font-weight:900;

    cursor:pointer;
}


/* ===== PHONE LANDSCAPE ===== */

@media screen and (orientation:landscape) and (max-height:600px){

    .demo-scoring-layout{
        min-height:67dvh;

        height:auto;

        padding:7px 14px 18px;

        overflow:auto;

        grid-template-rows:auto auto;
    }


    .demo-score-panel{
        display:flex;

        flex-direction:column;

        gap:3px;

        overflow:visible;
    }


    .demo-score-row{
        min-height:70px;

        grid-template-columns:
            clamp(125px,20vw,180px)
            55px
            minmax(0,1fr);

        gap:6px;
    }


    .demo-score-title{
        font-size:clamp(11px,1.7vw,15px);
    }


    .demo-score-value{
        font-size:clamp(17px,2.6vw,24px);
    }


    .demo-score-slider{
        height:32px;
    }


    .demo-score-slider::-webkit-slider-runnable-track{
        height:8px;
    }


    .demo-score-slider::-webkit-slider-thumb{
        width:27px;
        height:27px;

        margin-top:-10px;
    }


    .demo-scale{
        height:24px;
    }


    .demo-scale-label{
        font-size:7px;
    }


    .demo-scale-tick.major .demo-scale-label{
        font-size:9px;
    }


    .demo-deduction-layout{
        padding:8px 14px 14px;

        gap:8px;

        justify-content:flex-start;
    }


    .demo-deduction-controls{
        width:min(1000px,94vw);

        gap:10px;
    }


    .demo-deduction-card{
        padding:7px;

        gap:7px;
    }


    .demo-deduction-main{
        height:clamp(72px,16vh,105px);

        font-size:clamp(28px,4vw,42px);
    }


    .demo-ded-count{
        font-size:clamp(28px,4vw,42px);
    }


    .demo-final-summary{
        width:min(560px,70vw);

        padding:7px 12px;

        gap:2px 20px;
    }


    .demo-final-send{
        height:clamp(46px,9vh,64px);

        font-size:clamp(20px,3vw,30px);
    }

    .demo-scale-line,
    .demo-scale-ticks{
        left:20px;
        right:20px;
    }
}

.fs-mobile-layout{
    height:67dvh;
    padding:clamp(8px,1.5vw,18px);
    background:#050505;
    overflow:hidden;
    display:grid;
    grid-template-rows:auto 1fr;
    gap:clamp(8px,1.5vh,16px);
}

.fs-header-row{
    display:grid;
    grid-template-columns:1fr clamp(120px,20vw,220px);
    align-items:center;
    gap:clamp(8px,1.5vw,18px);
}

.fs-main-title{
    font-size:clamp(20px,3.5vw,38px);
    font-weight:900;
    color:white;
    line-height:1;
}

.fs-send{
    height:clamp(44px,9vh,76px);
    font-size:clamp(20px,3.5vw,34px);
    border-radius:clamp(14px,2vw,24px);
}

.fs-score-list{
    background:#111;
    border-radius:clamp(12px,2vw,24px);
    padding:clamp(8px,1.5vw,18px);
    display:grid;
    grid-template-rows:repeat(6,1fr);
    gap:clamp(5px,1vh,10px);
    overflow:hidden;
}

.fs-score-row{
    display:grid;
    grid-template-columns:clamp(145px,25vw,280px) 1fr;
    align-items:center;
    gap:clamp(6px,1vw,14px);
}

.fs-score-label{
    font-size:clamp(12px,2vw,22px);
    font-weight:900;
    color:white;
    line-height:1;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
}

.fs-button-grid{
    display:grid;
    grid-template-columns:repeat(10,1fr);
    gap:clamp(3px,.6vw,8px);
}

/* =========================================================
   FREESTYLE PRESENTATION SCORE BUTTONS
   Đồng bộ với Technical Proficiency
========================================================= */

.num-btn{
    width:100%;

    height:clamp(28px,6vh,52px);

    border:1px solid rgba(255,255,255,.05);

    border-radius:clamp(6px,1vw,12px);

    /* CHƯA CHẤM = màu cũ */
    background:#17263a;

    color:#ffffff;

    font-size:clamp(12px,2vw,20px);

    font-weight:900;

    cursor:pointer;

    opacity:1;

    box-shadow:
        inset 0 -3px 0 rgba(0,0,0,.20);

    transition:
        background .12s ease,
        color .12s ease,
        opacity .12s ease,
        transform .12s ease,
        box-shadow .12s ease;
}


/* Sau khi hàng Presentation đã chọn điểm */

.fs-button-grid.scored
.num-btn:not(.selected){

    background:#25282d;

    color:#8f969f;

    opacity:.78;

    box-shadow:
        inset 0 2px 5px rgba(0,0,0,.42);
}


/* Điểm được chọn */

.num-btn.selected{
    background:#00c853;

    color:#ffffff;

    opacity:1;

    border-color:#00e964;

    transform:translateY(-1px);

    box-shadow:
        0 0 14px rgba(0,200,83,.48),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.20);
}


/* Rê chuột vào điểm chưa chọn */
.num-btn:hover{
    opacity:1;

    background:#30343a;
    color:#d6d9dd;
}


/* Điểm đã chọn = xanh nổi */
.num-btn.selected{
    background:#00c853;
    color:#ffffff;

    opacity:1;

    border-color:#00e964;

    transform:translateY(-1px);

    box-shadow:
        0 0 14px rgba(0,200,83,.48),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.20);
}


/* Giữ nút đang chọn xanh khi rê chuột */
.num-btn.selected:hover{
    background:#00c853;
    color:#ffffff;

    opacity:1;

    box-shadow:
        0 0 16px rgba(0,200,83,.55),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.20);
}

/* =========================================================
   FREESTYLE TECHNICAL - NEW RULE
   Technical Proficiency 0-7
   Level of Difficulty +1 / +2 / +3
========================================================= */

.fs-tech-score-list{
    background:#111;
    border-radius:clamp(12px,2vw,24px);
    padding:clamp(8px,1.5vw,18px);

    display:flex;
    flex-direction:column;

    gap:clamp(5px,1vh,10px);

    overflow:auto;
}


/* ===== HEADER ===== */

.fs-tech-column-header{
    display:grid;

    grid-template-columns:
        clamp(145px,25vw,280px)
        minmax(0, 1fr)
        clamp(180px,25vw,330px);

    gap:clamp(6px,1vw,14px);

    align-items:end;
}


.fs-tech-header-left{
    min-width:0;
}


.fs-tech-header-box{
    text-align:center;

    font-size:clamp(12px,1.8vw,21px);

    font-weight:900;

    padding:clamp(5px,.8vh,9px);

    border-radius:clamp(7px,1vw,12px);

    line-height:1;
}


/* Technical Proficiency title */

.fs-tech-header-proficiency{
    color:#cfe6ff;

    background:#172230;

    border:1px solid rgba(80,160,255,.32);
}


/* Level of Difficulty title */

.fs-tech-header-difficulty{
    color:#ffb3b3;

    background:#2a2020;

    border:1px solid rgba(255,70,70,.42);
}


/* ===== EACH SKILL ROW ===== */

.fs-tech-skill-row{
    display:grid;

    grid-template-columns:
        clamp(145px,25vw,280px)
        minmax(0, 1fr)
        clamp(180px,25vw,330px);

    gap:clamp(6px,1vw,14px);

    align-items:center;
}


.fs-tech-skill-label{
    width:100%;

    display:flex;
    align-items:center;
    justify-content:space-between;

    gap:8px;

    font-size:clamp(12px,2vw,22px);
    font-weight:900;

    color:white;

    line-height:1.05;

    white-space:nowrap;

    overflow:visible;
}


/* ===== LEFT FRAME ===== */

.fs-proficiency-box{
    background:#131a23;

    border:2px solid rgba(56,119,190,.45);

    border-radius:clamp(8px,1.2vw,14px);

    padding:clamp(4px,.6vw,8px);
}


.fs-proficiency-grid{
    display:grid;

    grid-template-columns:repeat(8,1fr);

    gap:clamp(3px,.5vw,7px);
}


/* ===== RIGHT FRAME ===== */

.fs-difficulty-box{
    background:#261f1f;

    border:2px solid rgba(255,58,58,.55);

    border-radius:clamp(8px,1.2vw,14px);

    padding:clamp(4px,.6vw,8px);

    box-shadow:
        inset 0 0 14px rgba(255,0,0,.035);
}


.fs-difficulty-grid{
    display:grid;

    grid-template-columns:repeat(3,1fr);

    gap:clamp(4px,.6vw,8px);
}


/* ===== BUTTON ===== */

.fs-tech-btn{
    width:100%;
    min-width:0;

    height:clamp(28px,6vh,52px);

    border:1px solid rgba(255,255,255,.05);
    border-radius:clamp(6px,1vw,12px);

    /* CHƯA CHẤM = giao diện cũ */
    background:#17263a;
    color:#ffffff;

    font-size:clamp(12px,2vw,20px);
    font-weight:900;

    cursor:pointer;

    opacity:1;

    box-shadow:
        inset 0 -3px 0 rgba(0,0,0,.20);

    transition:
        background .12s ease,
        color .12s ease,
        opacity .12s ease,
        transform .12s ease,
        box-shadow .12s ease;
}


/* ========================================
   SAU KHI HÀNG ĐÃ ĐƯỢC CHẤM
   các nút KHÔNG được chọn mới chìm xuống
======================================== */

.fs-proficiency-box.scored
.fs-tech-btn:not(.proficiency-selected){

    background:#25282d;

    color:#8f969f;

    opacity:.78;

    box-shadow:
        inset 0 2px 5px rgba(0,0,0,.42);
}


/* Nút Technical được chọn */

.fs-tech-btn.proficiency-selected{
    background:#00c853;

    color:#ffffff;

    opacity:1;

    border-color:#00e964;

    transform:translateY(-1px);

    box-shadow:
        0 0 14px rgba(0,200,83,.48),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.20);
}


/* Khi rê chuột / chạm chuẩn bị sửa điểm */
.fs-tech-btn:hover{
    opacity:1;

    color:#d6d9dd;

    background:#30343a;
}


/* Technical selected = GREEN like current system */

.fs-tech-btn.proficiency-selected{
    background:#00c853;

    color:#ffffff;

    opacity:1;

    border-color:#00e964;

    transform:translateY(-1px);

    box-shadow:
        0 0 14px rgba(0,200,83,.48),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.20);
}


/* Difficulty normal */

.fs-tech-btn.difficulty{
    /* CHƯA CHỌN Difficulty = vẫn thấy rõ */
    background:#493a3a;

    color:#ffffff;

    opacity:1;

    border:1px solid rgba(255,90,90,.13);

    box-shadow:
        inset 0 -3px 0 rgba(0,0,0,.20);
}


/* Khi Difficulty đã chọn,
   những nút còn lại mới chìm */

.fs-difficulty-box.scored
.fs-tech-btn.difficulty:not(.difficulty-selected){

    background:#302c2c;

    color:#969090;

    opacity:.76;

    border-color:rgba(255,255,255,.05);

    box-shadow:
        inset 0 2px 5px rgba(0,0,0,.42);
}


/* Difficulty được chọn */

.fs-tech-btn.difficulty-selected{
    background:#d71920;

    color:#ffffff;

    opacity:1;

    border-color:#ff5555;

    transform:translateY(-1px);

    box-shadow:
        0 0 16px rgba(255,30,30,.52),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.22);
}

/* Difficulty selected = RED */

.fs-tech-btn.difficulty-selected{
    background:#d71920;

    color:#ffffff;

    opacity:1;

    border-color:#ff5555;

    transform:translateY(-1px);

    box-shadow:
        0 0 16px rgba(255,30,30,.52),
        0 4px 8px rgba(0,0,0,.38),
        inset 0 -3px 0 rgba(0,0,0,.22);
}


/* ===== BASIC MOVEMENT ===== */

.fs-basic-row{
    display:grid;

    grid-template-columns:
        clamp(145px,25vw,280px)
        minmax(0,1fr);

    gap:clamp(6px,1vw,14px);

    align-items:center;

    margin-top:clamp(2px,.5vh,6px);

    padding-top:clamp(5px,1vh,10px);

    border-top:1px solid rgba(255,255,255,.10);
}


.fs-basic-box{
    background:#131a23;

    border:1px solid rgba(255,255,255,.08);

    border-radius:clamp(8px,1.2vw,14px);

    padding:clamp(4px,.6vw,8px);
}


.fs-basic-grid{
    display:grid;

    grid-template-columns:repeat(10,1fr);

    gap:clamp(3px,.5vw,7px);
}


/* Small total displayed after skill name */

.fs-skill-total{
    display:flex;
    align-items:center;
    justify-content:center;

    flex:0 0 auto;

    min-width:clamp(25px,3.5vw,42px);
    height:clamp(22px,4vh,34px);

    margin-left:auto;
    margin-right:0;

    padding:0 6px;

    border-radius:999px;

    background:rgba(255,212,71,.13);

    border:1px solid rgba(255,212,71,.40);

    color:#ffd447;

    font-size:clamp(11px,1.6vw,17px);
    font-weight:900;
}


/* ===== PHONE LANDSCAPE ===== */

@media screen and (orientation:landscape) and (max-height:600px){

    .fs-tech-score-list{
        padding:6px 10px 14px!important;
        gap:4px!important;
        overflow:auto!important;
    }


    .fs-tech-column-header,
    .fs-tech-skill-row{

        grid-template-columns:
            clamp(130px,21vw,210px)
            minmax(0,1fr)
            clamp(145px,23vw,220px);

        gap:5px;
    }


    .fs-tech-skill-label{
        font-size:clamp(11px,1.7vw,16px);
    }


    .fs-tech-header-box{
        font-size:clamp(10px,1.5vw,14px);
        padding:4px;
    }


    .fs-proficiency-box,
    .fs-difficulty-box,
    .fs-basic-box{
        padding:3px;
        border-radius:7px;
    }


    .fs-proficiency-grid{
        gap:3px;
    }


    .fs-difficulty-grid{
        gap:4px;
    }


    .fs-tech-btn{
        min-height:26px!important;
        height:clamp(26px,5.8vh,38px)!important;

        font-size:clamp(11px,1.7vw,15px);
        border-radius:6px;
    }


    .fs-basic-row{
        grid-template-columns:
            clamp(130px,21vw,210px)
            minmax(0,1fr);

        gap:5px;
    }


    .fs-basic-grid{
        gap:3px;
    }
}

/* =========================================================
   FREESTYLE DEDUCTION
========================================================= */

.fs-deduction-layout{
    flex:1;

    padding:clamp(12px,2vw,28px);

    display:flex;
    flex-direction:column;

    justify-content:center;

    gap:clamp(12px,2vh,24px);

    background:#050505;
}


/* ===== TWO DEDUCTION CARDS ===== */

.fs-deduction-controls{
    width:min(980px,90vw);

    margin:0 auto;

    display:grid;
    grid-template-columns:1fr 1fr;

    gap:clamp(18px,4vw,50px);
}


.fs-deduction-card{
    min-height:clamp(130px,24vh,220px);

    display:grid;

    grid-template-columns:1fr auto;

    align-items:center;

    gap:clamp(12px,2vw,24px);

    padding:clamp(14px,2vw,26px);

    border-radius:clamp(16px,2.5vw,28px);

    background:#151515;

    border:2px solid rgba(255,60,60,.34);

    box-shadow:
        inset 0 0 20px rgba(255,0,0,.025),
        0 8px 25px rgba(0,0,0,.32);
}


/* Big -1 / -3 button */

.fs-deduction-main-btn{
    width:100%;
    height:clamp(95px,19vh,170px);

    border:2px solid rgba(255,255,255,.10);

    border-radius:clamp(16px,2vw,26px);

    color:#fff;

    font-size:clamp(40px,7vw,72px);

    font-weight:900;

    cursor:pointer;

    background:
        linear-gradient(
            180deg,
            #ef4049 0%,
            #b81720 100%
        );

    box-shadow:
        0 8px 18px rgba(0,0,0,.36),
        inset 0 2px 0 rgba(255,255,255,.16);

    transition:.12s;
}


.fs-deduction-main-btn.minus3{
    background:
        linear-gradient(
            180deg,
            #d81f29 0%,
            #7f080d 100%
        );
}


.fs-deduction-main-btn:active{
    transform:scale(.97);
}


/* ===== COUNT SIDE ===== */

.fs-deduction-count-box{
    min-width:clamp(90px,11vw,145px);

    display:flex;
    flex-direction:column;

    align-items:center;
    justify-content:center;

    gap:8px;
}


.fs-deduction-count-label{
    color:#aab0b7;

    font-size:clamp(11px,1.6vw,17px);

    font-weight:900;
}


.fs-deduction-count{
    min-width:clamp(65px,8vw,100px);

    text-align:center;

    color:#ffd447;

    font-size:clamp(38px,6vw,66px);

    line-height:1;

    font-weight:900;
}


/* Undo */

.fs-deduction-undo{
    width:clamp(58px,7vw,88px);

    height:clamp(30px,6vh,45px);

    border:0;

    border-radius:10px;

    background:#30343a;

    color:#d1d5da;

    font-size:clamp(12px,1.8vw,18px);

    font-weight:900;

    cursor:pointer;
}


.fs-deduction-undo:disabled{
    opacity:.25;

    cursor:default;
}


/* ===== SCORE SUMMARY ===== */

.fs-final-summary{
    width:min(700px,82vw);

    margin:0 auto;

    padding:clamp(12px,2vw,22px);

    border-radius:clamp(14px,2vw,22px);

    background:#111;

    border:1px solid rgba(255,255,255,.08);

    display:grid;

    grid-template-columns:1fr auto;

    row-gap:clamp(4px,.8vh,9px);

    column-gap:30px;
}


.fs-final-summary-label{
    color:#c7cbd1;

    font-size:clamp(13px,2vw,21px);

    font-weight:900;
}


.fs-final-summary-value{
    min-width:80px;

    text-align:right;

    color:white;

    font-size:clamp(16px,2.4vw,26px);

    font-weight:900;
}


.fs-final-summary-label.deduction,
.fs-final-summary-value.deduction{
    color:#ff6565;
}


.fs-final-total-label,
.fs-final-total-value{
    margin-top:5px;

    padding-top:8px;

    border-top:1px solid rgba(255,255,255,.15);

    font-size:clamp(20px,3vw,34px);

    font-weight:900;

    color:#ffd447;
}


.fs-final-total-value{
    text-align:right;
}


/* ===== FINAL SEND ===== */

.fs-final-send-wrap{
    display:flex;

    justify-content:center;
}


.fs-final-send{
    width:min(420px,55vw);

    height:clamp(58px,11vh,92px);

    border:0;

    border-radius:clamp(16px,2vw,24px);

    background:#00c853;

    color:white;

    font-size:clamp(24px,4vw,40px);

    font-weight:900;

    cursor:pointer;

    box-shadow:
        0 7px 20px rgba(0,0,0,.35),
        inset 0 -4px 0 rgba(0,0,0,.20);
}


/* ===== PHONE LANDSCAPE ===== */

@media screen and (orientation:landscape) and (max-height:600px){

    .fs-deduction-layout{
        padding:8px 16px 14px;

        gap:8px;

        justify-content:flex-start;

        overflow:auto;
    }


    .fs-deduction-controls{
        width:min(820px,88vw);

        gap:14px;
    }


    .fs-deduction-card{
        min-height:90px;

        padding:8px 12px;

        border-radius:14px;
    }


    .fs-deduction-main-btn{
        height:clamp(74px,17vh,110px);

        font-size:clamp(32px,6vw,52px);
    }


    .fs-deduction-count{
        font-size:clamp(30px,5vw,48px);
    }


    .fs-final-summary{
        width:min(620px,76vw);

        padding:8px 14px;

        row-gap:3px;
    }


    .fs-final-send{
        height:clamp(48px,10vh,68px);

        font-size:clamp(22px,4vw,32px);
    }
}

.fs-pre-list{
    grid-template-rows:repeat(4,1fr);
}

.fs-pre-row{
    grid-template-columns:clamp(190px,30vw,360px) 1fr;
}

.ready-page{
    width:100dvw;
    height:100dvh;
    overflow:hidden;
    background:radial-gradient(circle at 50% 0%,#102340 0,#05070d 48%,#020308 100%);
    display:grid;
    grid-template-rows:clamp(135px, 23vh, 185px) 1fr;
    padding:clamp(8px,1.5vw,18px);
    gap:clamp(8px,1.5vh,16px);
}

.ready-title{
    text-align:center;
    font-size:clamp(16px,3.2vw,28px)!important;
    line-height:1!important;
    font-weight:900;
    color:#2fd8ff;
}

.ready-info{
    justify-self:center;
    text-align:center;
    width:fit-content;
    max-width:min(420px,52vw);
    margin-left:auto!important;
    margin-right:auto!important;
    text-align:center;

    background:rgba(10,10,10,.82);
    border-radius:clamp(10px,1.5vw,18px);
    padding:clamp(6px,1vh,10px) clamp(14px,2.2vw,28px);
    font-size:clamp(12px,1.8vw,21px);
    font-weight:900;
    line-height:1.08;
    color:white;
    margin-bottom:0!important;
    position:relative;
    z-index:90;
    border:1px solid rgba(255,255,255,.10);
    box-shadow:0 0 18px rgba(0,0,0,.35);
}

.ready-poomsae-pill{
    display:inline-block;
    margin-top:5px;
    padding:4px 14px;
    border-radius:999px;
    background:rgba(255,212,71,.14);
    border:1px solid rgba(255,212,71,.65);
    color:#fff200;
    text-shadow:0 0 10px rgba(255,242,0,.65);
    font-size:clamp(13px,2vw,24px);
}

.ready-body{
    display:grid;
    align-items:center;
    gap:clamp(10px,2vw,24px);
}

.ready-body.single{
    grid-template-columns:1fr 1fr;
}

.ready-body.cutoff{
    grid-template-columns:1fr;
}

.ready-team{
    height:100%;
    min-height:0;
    border-radius:clamp(18px,3vw,34px);
    padding:clamp(14px,2.5vw,34px);
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    gap:clamp(8px,1.5vh,18px);
    overflow:hidden;
    border:3px solid rgba(255,255,255,.15);
}

.ready-team.blue{
    background:linear-gradient(145deg,#06264f,#071426);
    border-color:#1e88ff;
}

.ready-team.red{
    background:linear-gradient(145deg,#520609,#160305);
    border-color:#ff303a;
}

.ready-team.cutoff-one{
    width:min(620px,70vw);
    justify-self:center;
    background:linear-gradient(145deg,#111,#050505);
    border-color:#ffd447;
}

@media screen and (orientation:landscape) and (max-height:600px){
    .ready-page{
        grid-template-rows:clamp(92px, 20vh, 128px) 1fr!important;
    }

    .ready-info{
        position:absolute!important;
        left:50%!important;
        top:clamp(38px,10vh,58px)!important;
        transform:translateX(-50%)!important;

        width:max-content!important;
        max-width:50vw!important;

        padding:5px 14px!important;
        font-size:clamp(9px,1.65vw,15px)!important;
        line-height:1.05!important;
        z-index:120!important;
    }

    .ready-poomsae-pill{
        margin-top:3px!important;
        padding:2px 10px!important;
        font-size:clamp(10px,1.9vw,17px)!important;
    }

    .ready-team.cutoff-one{
        width:min(460px,58vw)!important;
        min-height:0!important;
        padding:clamp(10px,2vw,22px)!important;
    }

    .waiting-glass{
        top:64%!important;
        width:min(420px,54vw)!important;
        padding:clamp(12px,2vw,24px) clamp(18px,3vw,34px)!important;
        font-size:clamp(18px,3.4vw,34px)!important;
    }
}

.ready-team-top{
    display:flex;
    align-items:center;
    justify-content:center;
    gap:clamp(10px,1.8vw,22px);
}

.ready-team .flag{
    width:clamp(64px,12vw,150px);
    height:clamp(42px,8vw,96px);
}

.ready-noc{
    font-size:clamp(38px,8vw,96px);
    font-weight:900;
    line-height:1;
}

.ready-athlete{
    max-width:100%;
    font-size:clamp(18px,4vw,48px);
    font-weight:900;
    text-align:center;
    line-height:1.1;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
}

.send-row .send{
    height:clamp(44px,8vh,70px)!important;
    font-size:clamp(12px,2.2vw,24px)!important;
    line-height:1.05!important;
    padding:2px 4px!important;
    border-radius:clamp(10px,1.8vw,18px)!important;
}

.locked-control{
    opacity:.28!important;
    filter:grayscale(1)!important;
    pointer-events:none!important;
}

@media screen and (orientation:landscape) and (max-height:600px){
    .page.center{
        height:100dvh;
        width:100dvw;
        padding:clamp(6px,1vh,12px);
        justify-content:center;
        overflow:hidden;
    }

    .login-card{
        width:min(520px,74vw);
        max-height:96dvh;
        padding:clamp(10px,2vh,18px) clamp(18px,3vw,32px);
        display:flex;
        flex-direction:column;
        justify-content:center;
        gap:clamp(5px,1vh,10px);
    }

    .logo{
        font-size:clamp(24px,6vh,42px);
        line-height:1.05;
    }

    .sub{
        font-size:clamp(13px,3vh,22px);
        margin:0 0 clamp(4px,1vh,10px);
    }

    .field{
        margin:clamp(4px,1vh,8px) 0;
    }

    .field label{
        font-size:clamp(11px,2.5vh,16px);
        margin-bottom:clamp(3px,.7vh,6px);
    }

    select,input{
        height:clamp(34px,8vh,54px);
        font-size:clamp(17px,4vh,28px);
        border-radius:clamp(8px,1.5vw,14px);
    }

    .btn-main{
        height:clamp(40px,9vh,62px);
        font-size:clamp(18px,4vh,30px);
        margin-top:clamp(6px,1vh,12px);
        border-radius:clamp(12px,2vw,22px);
    }
}

.ready-page{
    background:
        radial-gradient(circle at 18% 20%, rgba(0,220,255,.22), transparent 28%),
        radial-gradient(circle at 82% 35%, rgba(0,255,130,.13), transparent 30%),
        linear-gradient(135deg,#02050c 0%,#071629 45%,#020308 100%)!important;
    position:relative;
}

.ready-page:before{
    content:"";
    position:absolute;
    inset:0;
    pointer-events:none;
    opacity:.22;
    background-image:
        linear-gradient(rgba(0,255,255,.18) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,255,255,.18) 1px, transparent 1px);
    background-size:42px 42px;
}

.ready-page:after{
    content:"";
    position:absolute;
    inset:0;
    pointer-events:none;
    background:radial-gradient(circle at 50% 50%, transparent 0%, rgba(0,0,0,.45) 72%);
}

.waiting-glass{
    position:fixed;
    left:50%;
    top:62%;
    transform:translate(-50%,-50%);
    z-index:70;
    padding:clamp(18px,3vw,34px) clamp(28px,5vw,56px);
    border-radius:clamp(18px,3vw,32px);
    background:rgba(8,18,32,.12);
    backdrop-filter:blur(3px);
    -webkit-backdrop-filter:blur(3px);
    border:1px solid rgba(80,220,255,.35);
    opacity:.88;
    box-shadow:
        0 0 28px rgba(0,220,255,.28),
        inset 0 0 20px rgba(255,255,255,.06);
    text-align:center;
    color:white;
    font-weight:900;
    font-size:clamp(22px,4vw,46px);
    letter-spacing:1px;
}

.waiting-glass span{
    display:block;
    margin-top:8px;
    color:#ffd447;
    font-size:clamp(13px,2vw,22px);
}

.match-line{
    font-size:clamp(10px,1.8vw,16px);
    font-weight:900;
    color:white;
    line-height:1;
}

.poomsae-box{
    margin-top:4px;
    padding:6px 12px;
    border:2px solid #00e5ff;
    border-radius:10px;
    color:#fff200;
    background:rgba(0,229,255,.12);
    font-size:clamp(12px,2vw,20px);
    font-weight:900;
    line-height:1.05;
    white-space:nowrap;
    text-shadow:0 0 10px rgba(255,242,0,.7);
    box-shadow:0 0 18px rgba(0,229,255,.35);
}

.top-side .athlete{
    font-size:clamp(18px,5vw,48px)!important;
    font-weight:900!important;
}


.toast{
    position:fixed;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
    z-index:10000;
    display:none;

    min-width:min(260px,58vw);

    padding:clamp(12px,2vw,20px)
            clamp(22px,4vw,38px);

    border-radius:clamp(16px,2vw,24px);

    background:rgba(187,247,208,.38);

    backdrop-filter:blur(6px);
    -webkit-backdrop-filter:blur(6px);

    border:1.5px solid rgba(34,197,94,.38);

    box-shadow:
        0 0 22px rgba(34,197,94,.18),
        inset 0 0 10px rgba(255,255,255,.05);

    color:#ecfff4;

    font-size:clamp(20px,3.2vw,38px);

    font-weight:900;
    text-align:center;
    letter-spacing:.3px;
}

.login-top-error{
    position:fixed;
    top:126px;
    left:50%;
    transform:translateX(-50%);
    z-index:10000;

    width:min(370px,72vw);
    height:68px;

    display:flex;
    align-items:center;
    justify-content:center;

    border-radius:4px;

    background:rgba(12,32,58,.72);
    border:3px solid red;

    color:#ff9a9a;
    font-size:20px;
    font-weight:900;
    letter-spacing:.5px;

    backdrop-filter:blur(4px);
    -webkit-backdrop-filter:blur(4px);

    box-shadow:0 0 22px rgba(255,0,0,.28);

    opacity:1;
    transition:all .6s ease;
}

.toast.warn{
    background:rgba(254,249,195,.88);
    border-color:rgba(234,179,8,.75);
    color:#713f12;
    box-shadow:0 0 35px rgba(234,179,8,.38);
}

.toast.ok{
    background:rgba(120,255,170,.22);
    border-color:rgba(120,255,170,.35);
    color:#f4fff8;
}

/* ===== FIX FREESTYLE TECH/PRE ON IPHONE XS MAX LANDSCAPE ===== */
@media screen and (orientation:landscape) and (max-height:600px){

    .fs-mobile-layout{
        min-height:67dvh!important;
        height:auto!important;
        overflow:auto!important;
        grid-template-rows:auto auto!important;
        padding-bottom:24px!important;
    }

    .fs-score-list{
        min-height:max-content!important;
        height:auto!important;
        overflow:visible!important;
        grid-template-rows:none!important;
        display:flex!important;
        flex-direction:column!important;
        gap:clamp(4px,.8vh,8px)!important;
        padding-bottom:20px!important;
    }

    .fs-score-row{
        min-height:clamp(30px,7vh,46px)!important;
        height:auto!important;
    }

    .fs-button-grid{
        min-height:clamp(28px,6vh,42px)!important;
    }

    .num-btn{
        min-height:clamp(26px,5.8vh,40px)!important;
        height:auto!important;
    }
}

/* Basic Movement:
   sau khi chọn điểm mới làm các nút còn lại chìm */

.fs-basic-box.scored
.fs-tech-btn:not(.proficiency-selected){

    background:#25282d;

    color:#8f969f;

    opacity:.78;

    box-shadow:
        inset 0 2px 5px rgba(0,0,0,.42);
}


/* =========================================================
   SPECIAL POOMSAE EVENTS
   FB / VSK / JMK
========================================================= */

.special-layout{
    flex:1;
    min-height:0;

    padding:clamp(10px,1.5vw,20px);

    background:#050505;

    overflow-y:auto;
    overflow-x:hidden;
}


.special-panel{
    width:100%;

    display:flex;
    flex-direction:column;

    gap:clamp(10px,1.5vh,18px);

    padding-bottom:24px;
}


.special-play{
    width:100%;

    padding:clamp(10px,1.4vw,18px);

    background:#111;

    border:1px solid rgba(255,255,255,.08);

    border-radius:clamp(12px,1.8vw,22px);
}


.special-play-title{
    margin-bottom:clamp(8px,1.2vh,14px);

    color:#ffffff;

    font-size:clamp(17px,2.4vw,28px);

    font-weight:900;
}


.special-score-row{
    width:100%;

    display:grid;

    grid-template-columns:
        clamp(125px,15vw,210px)
        minmax(0,1fr);

    align-items:center;

    gap:clamp(6px,1vw,14px);

    margin:6px 0;
}


.special-score-name{
    font-size:clamp(12px,1.8vw,20px);

    font-weight:900;

    white-space:nowrap;
}

/* =========================================================
   VSK / JMK DUAL SLIDER - FIX ALIGN
========================================================= */

.vsk-play-title{
    font-size:clamp(24px,3vw,42px)!important;
    font-weight:900!important;
    margin-bottom:16px!important;
    line-height:1!important;
}

.vsk-dual-row{
    display:grid;
    grid-template-columns:180px minmax(0,1fr);
    gap:22px;
    align-items:center;
    width:100%;
}

.vsk-dual-labels{
    display:flex;
    flex-direction:column;
    justify-content:center;
    gap:74px;
    min-width:0;
}

.vsk-dual-label{
    font-size:clamp(20px,1.7vw,30px);
    font-weight:900;
    color:#ffffff;
    line-height:1.05;
    white-space:nowrap;
}

.vsk-dual-main{
    min-width:0;
    width:100%;
    display:flex;
    flex-direction:column;
    gap:10px;
}

.vsk-slider-shell{
    position:relative;
    width:100%;
    min-width:0;
}

.vsk-slider-shell.top{
    padding-top:42px;
}

.vsk-slider-shell.bottom{
    padding-bottom:42px;
}

.vsk-score-bubble{
    position:absolute;
    transform:translateX(-50%);
    font-size:clamp(26px,2.4vw,44px);
    font-weight:900;
    line-height:1;
    white-space:nowrap;
    pointer-events:none;
    text-shadow:0 0 10px rgba(255,255,255,.12);
    z-index:4;
}

.vsk-score-bubble.acc{
    top:0;
    color:#ffd54a;
}

.vsk-score-bubble.pre{
    bottom:0;
    color:#6ec1ff;
}

.vsk-slider{
    width:100%;
    height:26px;
    display:block;
    background:transparent;
    -webkit-appearance:none;
    appearance:none;
    cursor:pointer;
    margin:0;
    padding:0;
}

.vsk-slider::-webkit-slider-runnable-track{
    height:12px;
    border-radius:999px;
    background:linear-gradient(90deg,#18ff91 0%,#2be7ff 100%);
    border:1px solid rgba(255,255,255,.14);
    box-shadow:
        0 0 12px rgba(0,255,160,.30),
        inset 0 0 10px rgba(255,255,255,.08);
}

.vsk-slider::-moz-range-track{
    height:12px;
    border-radius:999px;
    background:linear-gradient(90deg,#18ff91 0%,#2be7ff 100%);
    border:1px solid rgba(255,255,255,.14);
    box-shadow:
        0 0 12px rgba(0,255,160,.30),
        inset 0 0 10px rgba(255,255,255,.08);
}

.vsk-slider::-webkit-slider-thumb{
    -webkit-appearance:none;
    appearance:none;
    width:30px;
    height:30px;
    border-radius:50%;
    margin-top:-10px;
    border:3px solid #eafff4;
    background:radial-gradient(circle at 30% 30%, #ffffff 0%, #caffdf 35%, #16ef7f 70%, #08cb67 100%);
    box-shadow:
        0 0 18px rgba(0,255,150,.75),
        0 0 34px rgba(0,255,180,.28),
        0 4px 10px rgba(0,0,0,.35);
}

.vsk-slider::-moz-range-thumb{
    width:30px;
    height:30px;
    border-radius:50%;
    border:3px solid #eafff4;
    background:radial-gradient(circle at 30% 30%, #ffffff 0%, #caffdf 35%, #16ef7f 70%, #08cb67 100%);
    box-shadow:
        0 0 18px rgba(0,255,150,.75),
        0 0 34px rgba(0,255,180,.28),
        0 4px 10px rgba(0,0,0,.35);
}

.vsk-shared-ruler{
    position:relative;
    width:100%;
    min-width:0;
    margin:0;
}

.vsk-shared-ruler::before{
    content:"";
    position:absolute;
    left:0;
    right:0;
    top:20px;
    height:1px;
    background:rgba(180,230,255,.35);
    border-radius:999px;
}

.vsk-ruler-grid{
    width:100%;
    display:grid;
    grid-template-columns:repeat(51, minmax(0,1fr));
    align-items:start;
}

.vsk-ruler-tick{
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:flex-start;
    min-width:0;
}

.vsk-ruler-line.top{
    width:1px;
    height:8px;
    background:rgba(170,200,220,.55);
    margin-bottom:2px;
}

.vsk-ruler-line.bottom{
    width:1px;
    height:8px;
    background:rgba(170,200,220,.55);
    margin-top:2px;
}

.vsk-ruler-label{
    min-height:12px;
    font-size:8px;
    line-height:1;
    color:rgba(190,205,220,.72);
    font-weight:500;
    white-space:nowrap;
}

.vsk-ruler-tick.major .vsk-ruler-line.top,
.vsk-ruler-tick.major .vsk-ruler-line.bottom{
    width:3px;
    height:14px;
    background:#72ebff;
    box-shadow:
        0 0 4px rgba(80,220,255,.4),
        0 0 9px rgba(80,220,255,.18);
}

.vsk-ruler-tick.major .vsk-ruler-label{
    font-size:10px;
    font-weight:900;
    color:#ffffff;
    text-shadow:0 0 4px rgba(80,220,255,.3);
}

/* =========================
   SMALLER DESKTOP / HALF WINDOW
========================= */
@media screen and (max-width: 1400px){

    .vsk-dual-row{
        grid-template-columns:150px minmax(0,1fr);
        gap:16px;
    }

    .vsk-dual-labels{
        gap:66px;
    }

    .vsk-dual-label{
        font-size:clamp(18px,1.6vw,26px);
    }

    .vsk-slider-shell.top{
        padding-top:34px;
    }

    .vsk-slider-shell.bottom{
        padding-bottom:34px;
    }

    .vsk-score-bubble{
        font-size:clamp(22px,2vw,34px);
    }
}

/* =========================
   LANDSCAPE MOBILE / LOW HEIGHT
========================= */
@media screen and (orientation:landscape) and (max-height: 700px){

    .vsk-play-title{
        font-size:clamp(20px,2vw,26px)!important;
        margin-bottom:10px!important;
    }

    .vsk-dual-row{
        grid-template-columns:110px minmax(0,1fr);
        gap:10px;
    }

    .vsk-dual-labels{
        gap:48px;
    }

    .vsk-dual-label{
        font-size:clamp(12px,1.45vw,18px);
    }

    .vsk-slider-shell.top{
        padding-top:24px;
    }

    .vsk-slider-shell.bottom{
        padding-bottom:24px;
    }

    .vsk-score-bubble{
        font-size:clamp(12px,1.5vw,20px);
    }

    .vsk-slider{
        height:18px;
    }

    .vsk-slider::-webkit-slider-runnable-track,
    .vsk-slider::-moz-range-track{
        height:8px;
    }

    .vsk-slider::-webkit-slider-thumb{
        width:22px;
        height:22px;
        margin-top:-7px;
        border-width:2px;
    }

    .vsk-slider::-moz-range-thumb{
        width:22px;
        height:22px;
        border-width:2px;
    }

    .vsk-shared-ruler::before{
        top:14px;
    }

    .vsk-ruler-line.top,
    .vsk-ruler-line.bottom{
        height:5px;
    }

    .vsk-ruler-tick.major .vsk-ruler-line.top,
    .vsk-ruler-tick.major .vsk-ruler-line.bottom{
        width:2px;
        height:10px;
    }

    .vsk-ruler-label{
        font-size:6px;
    }

    .vsk-ruler-tick.major .vsk-ruler-label{
        font-size:7px;
    }
}

/* =========================
   HALF WINDOW / TABLET
========================= */
@media screen and (max-width: 1200px){

    .vsk-play-title{
        font-size:clamp(24px,2.6vw,34px)!important;
    }

    .vsk-dual-row{
        grid-template-columns:145px minmax(0,1fr);
        gap:12px;
    }

    .vsk-dual-labels{
        gap:80px;
    }

    .vsk-dual-label{
        font-size:clamp(20px,2vw,28px);
    }

    .vsk-slider-shell.top{
        padding-top:38px;
    }

    .vsk-slider-shell.bottom{
        padding-bottom:38px;
    }

    .vsk-score-bubble{
        font-size:clamp(22px,2.1vw,32px);
    }
}

/* =========================
   MOBILE / LANDSCAPE LOW HEIGHT
========================= */
@media screen and (orientation:landscape) and (max-height: 700px){

    .vsk-play-title{
        font-size:clamp(22px,2vw,28px)!important;
        margin-bottom:8px!important;
    }

    .vsk-dual-row{
        grid-template-columns:115px minmax(0,1fr);
        gap:10px;
    }

    .vsk-dual-labels{
        gap:62px;
    }

    .vsk-dual-label{
        font-size:clamp(14px,1.55vw,20px);
    }

    .vsk-slider-shell.top{
        padding-top:26px;
    }

    .vsk-slider-shell.bottom{
        padding-bottom:26px;
    }

    .vsk-score-bubble{
        font-size:clamp(14px,1.6vw,22px);
    }

    .vsk-slider{
        height:20px;
    }

    .vsk-slider::-webkit-slider-runnable-track,
    .vsk-slider::-moz-range-track{
        height:8px;
    }

    .vsk-slider::-webkit-slider-thumb{
        width:22px;
        height:22px;
        margin-top:-7px;
        border-width:2px;
    }

    .vsk-slider::-moz-range-thumb{
        width:22px;
        height:22px;
        border-width:2px;
    }

    .vsk-shared-ruler::before{
        top:15px;
    }

    .vsk-ruler-line.top,
    .vsk-ruler-line.bottom{
        height:5px;
    }

    .vsk-ruler-tick.major .vsk-ruler-line.top,
    .vsk-ruler-tick.major .vsk-ruler-line.bottom{
        width:2px;
        height:10px;
    }

    .vsk-ruler-label{
        font-size:6px;
    }

    .vsk-ruler-tick.major .vsk-ruler-label{
        font-size:7px;
    }
}

/* =========================
   VSK / JMK DUAL SLIDER
========================= */
.special-dual-score{
    display:grid;
    grid-template-columns:170px minmax(0,1fr);
    gap:18px;
    align-items:center;
    width:100%;
}

.special-dual-labels{
    display:flex;
    flex-direction:column;
    justify-content:center;
    gap:86px;
}

.special-dual-label{
    font-size:clamp(18px,2vw,34px);
    font-weight:900;
    color:#ffffff;
    line-height:1.1;
}

.special-dual-main{
    min-width:0;
    display:flex;
    flex-direction:column;
    gap:10px;
}

.special-dual-line{
    display:flex;
    flex-direction:column;
    min-width:0;
}

.special-dual-value{
    font-size:clamp(28px,2.6vw,44px);
    font-weight:900;
    line-height:1;
    margin-bottom:8px;
    text-shadow:0 0 10px rgba(255,255,255,.12);
}

.special-dual-value.accuracy{
    color:#ffd54a;
}

.special-dual-value.presentation{
    color:#6ec1ff;
    margin-top:8px;
    margin-bottom:0;
}

.special-dual-slider{
    width:100%;
    height:34px;
    background:transparent;
    -webkit-appearance:none;
    appearance:none;
    cursor:pointer;
}

.special-dual-slider::-webkit-slider-runnable-track{
    height:12px;
    border-radius:999px;
    background:linear-gradient(90deg, #19f58d 0%, #25e7ff 100%);
    border:1px solid rgba(255,255,255,.14);
    box-shadow:
        0 0 14px rgba(0,255,170,.35),
        inset 0 0 10px rgba(255,255,255,.08);
}

.special-dual-slider::-moz-range-track{
    height:12px;
    border-radius:999px;
    background:linear-gradient(90deg, #19f58d 0%, #25e7ff 100%);
    border:1px solid rgba(255,255,255,.14);
    box-shadow:
        0 0 14px rgba(0,255,170,.35),
        inset 0 0 10px rgba(255,255,255,.08);
}

.special-dual-slider::-webkit-slider-thumb{
    -webkit-appearance:none;
    appearance:none;
    width:30px;
    height:30px;
    border-radius:50%;
    margin-top:-10px;
    border:3px solid #ecfff4;
    background:
        radial-gradient(circle at 30% 30%, #ffffff 0%, #bfffe0 35%, #10e676 68%, #02c85e 100%);
    box-shadow:
        0 0 18px rgba(0,255,140,.8),
        0 0 34px rgba(0,255,170,.35),
        0 4px 10px rgba(0,0,0,.35);
}

.special-dual-slider::-moz-range-thumb{
    width:30px;
    height:30px;
    border-radius:50%;
    border:3px solid #ecfff4;
    background:
        radial-gradient(circle at 30% 30%, #ffffff 0%, #bfffe0 35%, #10e676 68%, #02c85e 100%);
    box-shadow:
        0 0 18px rgba(0,255,140,.8),
        0 0 34px rgba(0,255,170,.35),
        0 4px 10px rgba(0,0,0,.35);
}

.special-dual-ruler{
    position:relative;
    padding:4px 8px 8px;
    min-width:0;
}

.special-dual-ruler::before{
    content:"";
    position:absolute;
    left:8px;
    right:8px;
    top:18px;
    height:1px;
    background:rgba(160,210,235,.35);
    border-radius:999px;
}

.special-dual-ruler-grid{
    display:grid;
    grid-template-columns:repeat(51, minmax(0,1fr));
    align-items:start;
}

.special-dual-tick{
    display:flex;
    flex-direction:column;
    align-items:center;
    min-width:0;
}

.special-dual-tick-top,
.special-dual-tick-bottom{
    width:1px;
    background:rgba(170,200,220,.55);
    border-radius:999px;
}

.special-dual-tick-top{
    height:7px;
    margin-bottom:2px;
}

.special-dual-tick-bottom{
    height:7px;
    margin-top:2px;
}

.special-dual-tick-label{
    margin-top:2px;
    font-size:8px;
    line-height:1;
    color:rgba(190,205,220,.72);
    font-weight:500;
    white-space:nowrap;
}

.special-dual-tick.major .special-dual-tick-top,
.special-dual-tick.major .special-dual-tick-bottom{
    width:3px;
    height:13px;
    background:#73eaff;
    box-shadow:
        0 0 4px rgba(80,220,255,.5),
        0 0 8px rgba(80,220,255,.18);
}

.special-dual-tick.major .special-dual-tick-label{
    font-size:10px;
    font-weight:900;
    color:#ffffff;
    text-shadow:0 0 4px rgba(80,220,255,.35);
}

/* mobile / nửa cửa sổ */
@media screen and (max-width: 1100px){
    .special-dual-score{
        grid-template-columns:130px minmax(0,1fr);
        gap:12px;
    }

    .special-dual-labels{
        gap:72px;
    }

    .special-dual-label{
        font-size:clamp(15px,1.7vw,24px);
    }

    .special-dual-value{
        font-size:clamp(22px,2vw,32px);
    }

    .special-dual-slider{
        height:28px;
    }

    .special-dual-slider::-webkit-slider-runnable-track,
    .special-dual-slider::-moz-range-track{
        height:10px;
    }

    .special-dual-slider::-webkit-slider-thumb{
        width:24px;
        height:24px;
        margin-top:-7px;
        border-width:2px;
    }

    .special-dual-slider::-moz-range-thumb{
        width:24px;
        height:24px;
        border-width:2px;
    }

    .special-dual-ruler{
        padding:2px 6px 6px;
    }

    .special-dual-ruler::before{
        left:6px;
        right:6px;
        top:14px;
    }

    .special-dual-tick-top,
    .special-dual-tick-bottom{
        height:5px;
    }

    .special-dual-tick.major .special-dual-tick-top,
    .special-dual-tick.major .special-dual-tick-bottom{
        width:2px;
        height:10px;
    }

    .special-dual-tick-label{
        font-size:7px;
    }

    .special-dual-tick.major .special-dual-tick-label{
        font-size:8px;
    }
}

/* =========================================
   VSK / JMK SLIDER MODE
========================================= */

.special-score-row.slider-mode{
    align-items:start;
}

.special-slider-box{
    min-width:0;

    display:flex;
    flex-direction:column;

    gap:6px;
}

.special-slider-head{
    display:flex;
    align-items:center;
    justify-content:flex-start;
}

.special-slider-current{
    min-width:56px;

    font-size:clamp(18px,2.3vw,32px);
    font-weight:900;
    line-height:1;

    text-align:left;
}

.special-slider-current.accuracy{
    color:#ffd54a;
}

.special-slider-current.presentation{
    color:#6ec1ff;
}

.special-inline-range{
    width:100%;
    height:34px;

    background:transparent;

    -webkit-appearance:none;
    appearance:none;

    cursor:pointer;
}

/* TRACK */
.special-inline-range::-webkit-slider-runnable-track{
    height:12px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );

    box-shadow:
        0 0 14px rgba(0,255,170,.28),
        inset 0 0 8px rgba(255,255,255,.12);

    border:1px solid rgba(255,255,255,.12);
}

.special-inline-range::-moz-range-track{
    height:12px;

    border-radius:999px;

    background:
        linear-gradient(
            90deg,
            rgba(0,255,140,.95) 0%,
            rgba(0,220,255,.95) 100%
        );

    box-shadow:
        0 0 14px rgba(0,255,170,.28),
        inset 0 0 8px rgba(255,255,255,.12);

    border:1px solid rgba(255,255,255,.12);
}

/* THUMB */
.special-inline-range::-webkit-slider-thumb{
    -webkit-appearance:none;
    appearance:none;

    width:30px;
    height:30px;

    border-radius:50%;

    background:
        radial-gradient(
            circle at 30% 30%,
            #ffffff,
            #a7ffd2 35%,
            #00e676 70%
        );

    border:3px solid #edfff5;

    margin-top:-10px;

    box-shadow:
        0 0 18px rgba(0,255,140,.9),
        0 0 36px rgba(0,255,170,.4),
        0 4px 10px rgba(0,0,0,.4);
}

.special-inline-range::-moz-range-thumb{
    width:30px;
    height:30px;

    border-radius:50%;

    background:
        radial-gradient(
            circle at 30% 30%,
            #ffffff,
            #a7ffd2 35%,
            #00e676 70%
        );

    border:3px solid #edfff5;

    box-shadow:
        0 0 18px rgba(0,255,140,.9),
        0 0 36px rgba(0,255,170,.4),
        0 4px 10px rgba(0,0,0,.4);
}

/* SCALE */
.special-inline-scale{
    position:relative;

    margin-top:1px;
    padding:6px 8px 0;
}

.special-inline-scale::before{
    content:"";

    position:absolute;

    left:8px;
    right:8px;
    top:6px;

    height:1px;

    border-radius:999px;

    background:rgba(120,180,210,.35);
}

.special-inline-scale-inner{
    display:grid;
    grid-template-columns:repeat(51, minmax(0,1fr));
    align-items:start;
}

.special-inline-tick{
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:flex-start;
    gap:3px;
    min-width:0;
}

.special-inline-tick-line{
    width:1px;
    height:7px;

    border-radius:999px;

    background:rgba(150,180,205,.58);
}

.special-inline-tick-label{
    margin-top:1px;

    font-size:8px;
    line-height:1;

    font-weight:500;

    color:rgba(190,205,220,.72);

    white-space:nowrap;
}

.special-inline-tick.major .special-inline-tick-line{
    width:3px;
    height:14px;

    background:#6fe8ff;

    box-shadow:
        0 0 4px rgba(80,220,255,.55),
        0 0 8px rgba(80,220,255,.18);
}

.special-inline-tick.major .special-inline-tick-label{
    font-size:10px;
    font-weight:900;

    color:#ffffff;

    text-shadow:
        0 0 4px rgba(80,220,255,.45);
}


/* =========================
   BUTTON GRID
========================= */

.special-btn-grid{
    min-width:0;

    display:grid;

    gap:clamp(3px,.55vw,8px);
}


.special-btn-grid.six{
    grid-template-columns:repeat(6,minmax(0,1fr));
}


.special-btn-grid.eleven{
    grid-template-columns:repeat(11,minmax(0,1fr));
}


/* =========================
   COMMON BUTTON
========================= */

.special-score-btn{
    min-width:0;

    height:clamp(32px,6vh,54px);

    border-radius:clamp(6px,1vw,11px);

    border:1px solid rgba(255,255,255,.10);

    font-size:clamp(11px,1.7vw,18px);

    font-weight:900;

    color:white;

    cursor:pointer;

    transition:.12s;
}


/* ACCURACY = YELLOW */

.special-score-btn.accuracy{
    background:#9a7611;

    border-color:rgba(255,212,71,.38);

    color:#fff5c5;
}


/* PRESENTATION = BLUE */

.special-score-btn.presentation{
    background:#164d89;

    border-color:rgba(80,165,255,.38);
}


/* LEVEL = LIGHT PURPLE */

.special-score-btn.level{
    background:#725a91;

    border-color:rgba(205,170,255,.42);

    color:#f5eaff;
}


/* =========================================
   SAU KHI HÀNG ĐÃ CHỌN
========================================= */

.special-btn-grid.scored
.special-score-btn:not(.selected){

    background:#25282d;

    border-color:rgba(255,255,255,.04);

    color:#858c95;

    opacity:.75;

    box-shadow:
        inset 0 2px 5px rgba(0,0,0,.42);
}


/* Accuracy selected */

.special-score-btn.accuracy.selected{
    background:#f4c21f;

    color:#161100;

    opacity:1;

    border-color:#ffe269;

    box-shadow:
        0 0 14px rgba(255,210,50,.45);
}


/* Presentation selected */

.special-score-btn.presentation.selected{
    background:#1684e8;

    color:white;

    opacity:1;

    border-color:#58afff;

    box-shadow:
        0 0 14px rgba(30,145,255,.45);
}


/* Level selected */

.special-score-btn.level.selected{
    background:#ae7ce6;

    color:white;

    opacity:1;

    border-color:#d4b2ff;

    box-shadow:
        0 0 14px rgba(190,135,255,.45);
}


/* =========================================
   DEDUCTION -3 / -5
========================================= */

.special-deduction{
    width:min(900px,100%);

    margin:8px auto;

    display:grid;

    grid-template-columns:1fr 1fr;

    gap:clamp(12px,2vw,26px);
}


.special-ded-card{
    background:#151515;

    border:2px solid rgba(255,60,60,.30);

    border-radius:18px;

    padding:10px;

    display:grid;

    grid-template-columns:1fr auto;

    gap:10px;

    align-items:center;
}


.special-ded-main{
    height:clamp(66px,12vh,110px);

    border:0;

    border-radius:15px;

    background:
        linear-gradient(
            180deg,
            #e53b45,
            #a9141c
        );

    color:white;

    font-size:clamp(28px,4vw,48px);

    font-weight:900;

    cursor:pointer;
}


.special-ded-main.minus5{
    background:
        linear-gradient(
            180deg,
            #bb1820,
            #65070b
        );
}


.special-ded-side{
    min-width:70px;

    display:flex;

    flex-direction:column;

    align-items:center;

    gap:5px;
}


.special-ded-label{
    color:#aeb4bd;

    font-size:11px;

    font-weight:900;
}


.special-ded-count{
    color:#ffd447;

    font-size:34px;

    font-weight:900;
}


.special-ded-undo{
    border:0;

    border-radius:8px;

    padding:7px 10px;

    background:#30343a;

    color:white;

    font-weight:900;
}


.special-send-wrap{
    display:flex;

    justify-content:center;

    padding:5px 0 20px;
}


.special-send{
    width:min(420px,55vw);

    height:clamp(56px,10vh,86px);
}


/* PHONE LANDSCAPE */

@media screen and (orientation:landscape) and (max-height:600px){

    .special-layout{
        min-height:67dvh;

        padding:7px 10px 22px;

        overflow-y:auto!important;
    }


    .special-play{
        padding:7px 9px;
    }


    .special-score-row{
        grid-template-columns:
            clamp(100px,16vw,145px)
            minmax(0,1fr);

        gap:5px;
    }

    .special-slider-current{
        min-width:44px;
        font-size:clamp(14px,1.8vw,20px);
    }

    .special-inline-range{
        height:26px;
    }

    .special-inline-range::-webkit-slider-runnable-track,
    .special-inline-range::-moz-range-track{
        height:10px;
    }

    .special-inline-range::-webkit-slider-thumb{
        width:24px;
        height:24px;
        margin-top:-7px;
        border-width:2px;
    }

    .special-inline-range::-moz-range-thumb{
        width:24px;
        height:24px;
        border-width:2px;
    }

    .special-inline-scale{
        padding:5px 6px 0;
    }

    .special-inline-scale::before{
        left:6px;
        right:6px;
        top:5px;
    }

    .special-inline-tick-line{
        height:6px;
    }

    .special-inline-tick.major .special-inline-tick-line{
        width:2px;
        height:11px;
    }

    .special-inline-tick-label{
        font-size:7px;
    }

    .special-inline-tick.major .special-inline-tick-label{
        font-size:9px;
    }


    .special-score-name{
        font-size:clamp(10px,1.5vw,13px);
    }


    .special-score-btn{
        height:clamp(27px,5.6vh,37px);

        font-size:clamp(9px,1.4vw,13px);
    }


    .special-btn-grid{
        gap:3px;
    }


    .special-deduction{
        width:min(760px,90vw);
    }
}

/* =========================================================
   SPECIAL EVENT HEADER SUMMARY
========================================================= */

.special-header-summary{
    padding:clamp(7px,1.1vw,14px);

    display:flex;
    flex-direction:column;

    align-items:center;
    justify-content:center;

    gap:clamp(6px,.8vh,10px);

    background:#050505;
}


.special-header-grid{
    width:min(360px,94%);

    display:grid;
    grid-template-columns:1fr 1fr;

    gap:8px 18px;
}


.special-header-cell{
    text-align:center;
}


.special-header-label{
    font-size:clamp(10px,1.45vw,15px);
    font-weight:900;
    line-height:1.1;
}


.special-header-number{
    margin-top:2px;

    font-size:clamp(21px,2.8vw,32px);
    font-weight:900;
    line-height:1;
}


.special-header-label.accuracy,
.special-header-number.accuracy{
    color:#ffd447;
}


.special-header-label.presentation,
.special-header-number.presentation{
    color:#5ab5ff;
}


.special-header-label.deduct,
.special-header-number.deduct{
    color:#ff6a6a;
}


.special-header-label.total,
.special-header-number.total{
    color:#ffffff;
}


.special-header-judge{
    padding-top:4px;

    color:#00ff75;
    font-size:clamp(15px,2vw,22px);
    font-weight:900;
}


/* =========================================================
   SPECIAL HEADER - HÀNG TRÊN
========================================================= */

.special-header-top{
    width:min(360px,94%);

    display:grid;

    grid-template-columns:
        repeat(2, minmax(0,1fr));

    align-items:center;

    gap:
        clamp(4px,.7vh,8px)
        clamp(10px,1.5vw,20px);
}


/* Freestyle Breaking:
   Accuracy | Presentation | Lv. Skill */

.special-header-top.breaking{
    width:min(440px,96%);

    grid-template-columns:
        repeat(3, minmax(0,1fr));

    gap:clamp(6px,1vw,14px);
}

.special-header-score{
    min-width:0;

    display:flex;
    flex-direction:column;

    align-items:center;
    justify-content:center;

    text-align:center;
}

.special-header-top.breaking
.special-header-label{
    font-size:clamp(9px,1.15vw,13px);
}

.special-header-top.breaking
.special-header-number{
    font-size:clamp(18px,2.3vw,28px);
}

/* Dòng dưới:
   Deduction / Total */

.special-header-bottom{
    width:min(300px,82%);

    display:grid;

    grid-template-columns:
        1fr 1fr;

    gap:clamp(14px,2vw,28px);

    padding-top:4px;

    border-top:
        1px solid rgba(
            255,
            255,
            255,
            .13
        );
}

.special-header-label.level,
.special-header-number.level{
    color:#c591ff;
}

/* ===== TOTAL ===== */

.special-header-total{
    width:min(250px,74%);

    text-align:center;

    margin-top:2px;

    padding-top:5px;

    border-top:1px solid rgba(255,255,255,.14);
}


.special-header-total-label{
    color:#cfd5de;

    font-size:clamp(11px,1.6vw,17px);

    font-weight:900;
}


.special-header-total-number{
    color:white;

    margin-top:1px;

    font-size:clamp(27px,3.7vw,40px);

    font-weight:900;

    line-height:1;
}


/* ===== JUDGE ===== */

.special-header-judge{
    color:#00ff75;

    font-size:clamp(15px,2vw,22px);

    font-weight:900;
}

/* =========================================================
   SPECIAL EVENT NAME
   Freestyle Breaking / Vertical Spinning Kicks /
   Jumping Multiple Kicks
========================================================= */

.special-event-box{
    width:max-content;
    max-width:94%;

    margin:4px auto 0;

    padding:
        clamp(5px,.7vh,8px)
        clamp(12px,1.5vw,22px);

    border:2px solid #00e5ff;

    border-radius:
        clamp(8px,1vw,12px);

    background:
        rgba(0,55,70,.58);

    color:#fff200;

    font-size:
        clamp(14px,1.65vw,20px);

    font-weight:900;

    line-height:1.15;

    text-align:center;

    white-space:normal;

    box-shadow:
        0 0 8px rgba(0,229,255,.75),
        0 0 18px rgba(0,229,255,.30);

    box-sizing:border-box;
}


/* Trung tâm header phải được phép co lại */
.score-top .center-info{
    min-width:0;
}


/* =========================================================
   NỬA CỬA SỔ / MÀN HÌNH HẸP
========================================================= */

@media screen and (max-width:900px){

    .special-event-box{
        max-width:96%;

        margin-top:3px;

        padding:4px 8px;

        border-width:1.5px;

        border-radius:7px;

        font-size:
            clamp(10px,1.55vw,14px);

        line-height:1.05;
    }

}


/* =========================================================
   ĐIỆN THOẠI XOAY NGANG
========================================================= */

@media screen and (orientation:landscape) and (max-height:600px){

    .special-event-box{
        width:auto;
        max-width:96%;

        margin:2px auto 0;

        padding:3px 8px;

        border-width:1.5px;

        border-radius:7px;

        font-size:
            clamp(9px,1.55vw,13px);

        line-height:1.05;

        white-space:normal;
    }

}


/* =========================================================
   SPECIAL EVENT - FINAL RESPONSIVE
   Freestyle Breaking / VSK / JMK
   HALF WINDOW + PHONE LANDSCAPE
========================================================= */

@media screen and (orientation:landscape) and (max-height:600px){

    /* =====================================================
       PAGE
    ===================================================== */

    .special-score-page{
        width:100dvw!important;
        height:100dvh!important;

        display:grid!important;

        grid-template-rows:
            34dvh
            66dvh!important;

        overflow:hidden!important;
    }


    /* =====================================================
       HEADER
    ===================================================== */

    .special-score-page > .special-score-top{
        width:100%!important;
        height:34dvh!important;

        min-width:0!important;
        min-height:0!important;

        display:grid!important;

        grid-template-columns:
            34%
            32%
            34%!important;

        overflow:hidden!important;
    }


    /* =====================================================
       LEFT
    ===================================================== */

    .special-score-top .special-header-athlete{
        min-width:0!important;
        min-height:0!important;

        padding:6px 8px!important;

        display:flex!important;
        flex-direction:column!important;

        justify-content:center!important;
        align-items:flex-start!important;

        gap:4px!important;

        overflow:hidden!important;
    }


    .special-score-top
    .special-header-athlete
    > div:first-child{
        width:100%!important;

        display:flex!important;

        align-items:center!important;

        gap:6px!important;
    }


    .special-score-top
    .special-header-athlete
    .flag{
        width:clamp(34px,5.8vw,50px)!important;
        height:clamp(23px,3.9vw,34px)!important;

        flex:0 0 auto!important;
    }


    .special-score-top
    .special-header-athlete
    .noc{
        min-width:0!important;

        font-size:
            clamp(16px,2.6vw,23px)!important;

        line-height:1!important;
    }


    .special-score-top
    .special-header-athlete
    .athlete{
        width:100%!important;

        font-size:
            clamp(11px,1.85vw,17px)!important;

        line-height:1.08!important;

        white-space:normal!important;

        overflow:hidden!important;

        text-overflow:clip!important;

        display:block!important;
    }


    /* =====================================================
       CENTER
    ===================================================== */

    .special-score-top .center-info{
        min-width:0!important;
        min-height:0!important;

        padding:4px 5px!important;

        display:flex!important;
        flex-direction:column!important;

        align-items:center!important;
        justify-content:center!important;

        gap:2px!important;

        overflow:hidden!important;
    }


    .special-score-top .division{
        width:100%!important;

        padding:3px 5px!important;

        flex:0 0 auto!important;

        font-size:
            clamp(8px,1.15vw,11px)!important;

        line-height:1!important;

        border-radius:7px!important;
    }


    .special-score-top .timer{
        margin:0!important;

        flex:0 0 auto!important;

        font-size:
            clamp(23px,3.8vw,33px)!important;

        line-height:1!important;
    }


    .special-score-top .match-line{
        flex:0 0 auto!important;

        font-size:
            clamp(8px,1.1vw,10px)!important;

        line-height:1!important;

        white-space:nowrap!important;
    }


    .special-score-top .special-event-box{
        width:auto!important;
        max-width:96%!important;

        margin:5px auto 0!important;

        padding:2px 6px!important;

        flex:0 0 auto!important;

        border-width:1px!important;
        border-radius:6px!important;

        font-size:
            clamp(8px,1.05vw,10px)!important;

        line-height:1!important;

        white-space:normal!important;
    }


    /* ===============================
       RIGHT COLUMN
    =============================== */

    .special-score-page
    .special-score-top
    .special-header-summary{

        width:100%!important;
        height:100%!important;

        min-width:0!important;
        min-height:0!important;

        padding:5px 8px!important;

        display:flex!important;
        flex-direction:column!important;

        align-items:center!important;
        justify-content:center!important;

        gap:3px!important;

        background:#050505!important;

        overflow:visible!important;
    }


    /* ===============================
       ROW 1
       Accuracy / Presentation
    =============================== */

    .special-score-page
    .special-header-top{

        width:94%!important;
        height:auto!important;

        margin:0 auto!important;
        padding:0!important;

        display:grid!important;

        grid-template-columns:
            repeat(2,minmax(0,1fr))!important;

        gap:4px!important;

        flex:0 0 auto!important;
    }


    /* Freestyle Breaking = 3 cột */

    .special-score-page
    .special-header-top.breaking{

        width:98%!important;

        grid-template-columns:
            repeat(3,minmax(0,1fr))!important;

        gap:3px!important;
    }


    /* ===============================
       SCORE CELL
    =============================== */

    .special-score-page
    .special-header-score{

        width:auto!important;
        height:auto!important;

        min-width:0!important;
        min-height:0!important;

        margin:0!important;
        padding:0!important;

        display:flex!important;
        flex-direction:column!important;

        align-items:center!important;
        justify-content:flex-start!important;

        flex:none!important;

        text-align:center!important;
    }


    /* ===============================
       LABEL
    =============================== */

    .special-score-page
    .special-header-label{

        margin:0!important;
        padding:0!important;

        font-size:
            clamp(6px,.8vw,8px)!important;

        line-height:1!important;

        font-weight:900!important;

        white-space:nowrap!important;
    }


    /* ===============================
       NUMBER
    =============================== */

    .special-score-page
    .special-header-number{

        margin:
            2px
            0
            0!important;

        padding:0!important;

        font-size:
            clamp(14px,1.8vw,19px)!important;

        line-height:1!important;

        font-weight:900!important;
    }


    /* =====================================================
       DEDUCTION + TOTAL
       FIX KHÔNG CHO Ô BỊ KÉO CAO
    ===================================================== */

    .special-score-page
    .special-header-bottom{

        width:68%!important;

        height:auto!important;
        min-height:0!important;
        max-height:none!important;

        margin:1px auto 0!important;
        padding:3px 0 0!important;

        display:flex!important;
        flex-direction:row!important;

        align-items:flex-start!important;
        justify-content:center!important;

        gap:10px!important;

        flex-grow:0!important;
        flex-shrink:0!important;
        flex-basis:auto!important;

        align-self:auto!important;

        border-top:
            1px solid rgba(255,255,255,.16)!important;

        overflow:visible!important;
    }


    /* Hai ô DEDUCTION / TOTAL */

    .special-score-page
    .special-header-bottom
    .special-header-score{

        width:auto!important;
        height:auto!important;

        min-width:0!important;
        min-height:0!important;

        max-height:none!important;

        margin:0!important;
        padding:0!important;

        display:flex!important;
        flex-direction:column!important;

        align-items:center!important;
        justify-content:flex-start!important;

        flex-grow:0!important;
        flex-shrink:0!important;
        flex-basis:calc(50% - 5px)!important;

        align-self:flex-start!important;

        overflow:visible!important;
    }


    /* Tiêu đề */

    .special-score-page
    .special-header-bottom
    .special-header-label{

        position:static!important;

        display:block!important;

        width:auto!important;
        height:auto!important;

        margin:0!important;
        padding:0!important;

        flex:none!important;

        font-size:
            clamp(6px,.8vw,8px)!important;

        font-weight:900!important;

        line-height:1!important;

        visibility:visible!important;
        opacity:1!important;
    }


    /* Con số */

    .special-score-page
    .special-header-bottom
    .special-header-number{

        position:static!important;

        display:block!important;

        width:auto!important;
        height:auto!important;

        margin:2px 0 0!important;
        padding:0!important;

        flex:none!important;

        font-size:
            clamp(14px,1.8vw,18px)!important;

        font-weight:900!important;

        line-height:1!important;

        visibility:visible!important;
        opacity:1!important;
    }


    /* Màu */

    .special-score-page
    .special-header-bottom
    .special-header-label.deduct,
    .special-score-page
    .special-header-bottom
    .special-header-number.deduct{
        color:#ff6262!important;
    }


    .special-score-page
    .special-header-bottom
    .special-header-label.total,
    .special-score-page
    .special-header-bottom
    .special-header-number.total{
        color:#ffffff!important;
    }


    /* ===============================
       ROW 3
       Judge
    =============================== */

    .special-score-page
    .special-header-judge{

        width:100%!important;
        height:auto!important;

        margin:0!important;
        padding:0!important;

        text-align:center!important;

        color:#00ff75!important;

        font-size:
            clamp(9px,1.3vw,13px)!important;

        line-height:1!important;

        font-weight:900!important;
    }
}




/* JUDGE RESPONSIVE V2: Only narrow/short windows. Full-size laptop is unchanged.
   Fix: score/pill elements used fixed min-height and oversized viewport-based fonts.
   All sizing stays in the viewport without a horizontal scrollbar. */
@media screen and (max-width: 1199px), screen and (max-height: 740px) {
  html, body, #appRoot {
    width: 100% !important;
    height: 100% !important;
    max-width: 100% !important;
    overflow: hidden !important;
  }
  #appRoot .score-page {
    width: 100% !important;
    height: 100dvh !important;
    min-width: 0 !important;
    min-height: 0 !important;
    display: grid !important;
    grid-template-rows: clamp(92px, 23dvh, 195px) minmax(0, 1fr) !important;
    overflow: hidden !important;
  }
  #appRoot .score-top {
    height: 100% !important;
    min-height: 0 !important;
    grid-template-columns: minmax(0, 34%) minmax(0, 32%) minmax(0, 34%) !important;
    overflow: hidden !important;
  }
  #appRoot .score-bottom {
    width: 100% !important;
    height: 100% !important;
    min-width: 0 !important;
    min-height: 0 !important;
    display: grid !important;
    grid-template-columns: minmax(0, 30%) minmax(0, 40%) minmax(0, 30%) !important;
    overflow: hidden !important;
  }
  #appRoot .score-top > *, #appRoot .score-bottom > * {
    min-width: 0 !important;
    min-height: 0 !important;
    box-sizing: border-box !important;
  }
  #appRoot .top-side {
    padding: clamp(3px, .8vw, 9px) !important;
    gap: clamp(2px, .5vh, 6px) !important;
    justify-content: flex-start !important;
    overflow: hidden !important;
  }
  #appRoot .top-side > div:first-child { min-width: 0 !important; gap: clamp(4px, .8vw, 10px) !important; }
  #appRoot .top-side .flag {
    flex: 0 0 auto !important;
    width: clamp(28px, 6vw, 58px) !important;
    height: clamp(20px, 4vw, 40px) !important;
  }
  #appRoot .top-side .noc {
    min-width: 0 !important;
    font-size: clamp(16px, 3vw, 32px) !important;
    line-height: 1.05 !important;
  }
  #appRoot .top-side .athlete {
    font-size: clamp(12px, 5vw, 32px) !important;
    line-height: 1.08 !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    display: -webkit-box !important;
    -webkit-box-orient: vertical !important;
    -webkit-line-clamp: 2 !important;
    overflow: hidden !important;
  }
  #appRoot .center-info {
    min-width: 0 !important;
    min-height: 0 !important;
    padding: clamp(2px, .7vw, 8px) !important;
    gap: clamp(1px, .3vh, 5px) !important;
    justify-content: center !important;
    overflow: hidden !important;
  }
  #appRoot .center-info .division {
    max-width: 100% !important;
    padding: 4px 6px !important;
    font-size: clamp(9px, 2vw, 16px) !important;
    line-height: 1.05 !important;
    border-radius: 10px !important;
    overflow-wrap: anywhere !important;
  }
  #appRoot .center-info .timer {
    font-size: clamp(25px, 4.8vw, 48px) !important;
    line-height: 1 !important;
  }
  #appRoot .center-info .match-line {
    font-size: clamp(9px, 1.3vw, 14px) !important;
    line-height: 1 !important;
  }
  #appRoot .center-info .poomsae-box, #appRoot .center-info .ready-poomsae-pill {
    min-width: 0 !important;
    max-width: 100% !important;
    margin-top: 1px !important;
    padding: 3px 5px !important;
    font-size: clamp(10px, 1.35vw, 17px) !important;
    line-height: 1.05 !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
  }
  #appRoot .side-score {
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    gap: clamp(5px, 1.5dvh, 13px) !important;
    padding: clamp(4px, 1.1vw, 14px) !important;
    overflow: hidden !important;
  }
  #appRoot .deduct-pill {
    box-sizing: border-box !important;
    flex: 0 1 clamp(66px, 27dvh, 185px) !important;
    width: 100% !important;
    height: clamp(66px, 27dvh, 185px) !important;
    max-height: 46% !important;
    min-height: 0 !important;
    min-width: 0 !important;
    padding: 4px clamp(5px, 1.1vw, 15px) !important;
    gap: clamp(2px, .8vw, 10px) !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    border-radius: 999px !important;
    overflow: hidden !important;
  }
  #appRoot .deduct-value {
    min-width: 0 !important;
    font-size: clamp(23px, 5.2vw, 62px) !important;
    line-height: 1 !important;
  }
  #appRoot .deduct-badge {
    flex: 0 0 auto !important;
    width: clamp(30px, 6vw, 76px) !important;
    height: clamp(30px, 6vw, 76px) !important;
    font-size: clamp(15px, 2.9vw, 30px) !important;
  }
  #appRoot .mid-score {
    display: flex !important;
    flex-direction: column !important;
    min-width: 0 !important;
    min-height: 0 !important;
    justify-content: center !important;
    gap: clamp(3px, 1dvh, 11px) !important;
    padding: clamp(3px, .85vw, 12px) !important;
    overflow: hidden !important;
  }
  #appRoot .tech-total-row {
    flex: 0 0 auto !important;
    min-width: 0 !important;
    height: clamp(30px, 6dvh, 56px) !important;
    grid-template-columns: minmax(0, 22%) minmax(0, 56%) minmax(0, 22%) !important;
    gap: clamp(3px, .6vw, 9px) !important;
    margin: 0 !important;
  }
  #appRoot .tech-total {
    min-width: 0 !important;
    height: 100% !important;
    font-size: clamp(15px, 2.4vw, 24px) !important;
    border-radius: 9px !important;
  }
  #appRoot .tech-total-title {
    min-width: 0 !important;
    font-size: clamp(11px, 2.1vw, 23px) !important;
    line-height: 1 !important;
    overflow: hidden !important;
  }
  #appRoot .pre-row {
    flex: 0 0 auto !important;
    min-width: 0 !important;
    width: 100% !important;
    height: clamp(29px, 6.4dvh, 54px) !important;
    grid-template-columns: minmax(0, 22%) minmax(0, 56%) minmax(0, 22%) !important;
    gap: clamp(3px, .6vw, 9px) !important;
  }
  #appRoot .pre-row.cutoff { grid-template-columns: minmax(0, 25%) minmax(0, 75%) !important; }
  #appRoot .pre-btn {
    min-width: 0 !important;
    width: 100% !important;
    height: 100% !important;
    padding: 0 2px !important;
    font-size: clamp(13px, 2.1vw, 22px) !important;
    border-radius: 9px !important;
  }
  #appRoot .pre-label {
    min-width: 0 !important;
    font-size: clamp(10px, 1.8vw, 20px) !important;
    line-height: 1.05 !important;
    overflow-wrap: anywhere !important;
  }
  #appRoot .send, #appRoot .send-row .send {
    flex: 0 0 auto !important;
    width: 100% !important;
    height: clamp(38px, 9dvh, 76px) !important;
    min-height: 0 !important;
    padding: 2px !important;
    font-size: clamp(20px, 3.7vw, 39px) !important;
    line-height: 1 !important;
    border-radius: 13px !important;
  }
  #appRoot .send-row {
    flex: 0 0 auto !important;
    min-height: 0 !important;
    gap: 5px !important;
  }
  #appRoot .mid-score > div:last-child { font-size: clamp(10px, 1.5vw, 15px) !important; line-height: 1.05 !important; }
  #appRoot .mid-score .score-card { min-height: 0 !important; height: clamp(38px, 8dvh, 90px) !important; font-size: clamp(12px, 2.2vw, 24px) !important; }
  #appRoot .mid-score .score-summary { gap: 5px !important; }
}
/* On short phones, height is scarcer than width: make the scoring controls compact. */
@media screen and (orientation: landscape) and (max-height: 520px) {
  #appRoot .score-page { grid-template-rows: clamp(84px, 24dvh, 116px) minmax(0, 1fr) !important; }
  #appRoot .score-top .center-info .timer { font-size: clamp(23px, 6.8dvh, 34px) !important; }
  #appRoot .score-top .center-info .division { font-size: clamp(9px, 5dvh, 14px) !important; }
  #appRoot .score-top .center-info .poomsae-box { font-size: clamp(9px, 2.6dvh, 12px) !important; }
  #appRoot .score-top .top-side .noc { font-size: clamp(16px, 5dvh, 23px) !important; }
  #appRoot .score-top .top-side .athlete { font-size: clamp(11px, 6dvh, 32px) !important; }
  #appRoot .side-score { gap: clamp(4px, 1dvh, 7px) !important; padding: clamp(3px, .5vw, 6px) !important; }
  #appRoot .deduct-pill { flex-basis: clamp(64px, 27dvh, 105px) !important; height: clamp(64px, 27dvh, 105px) !important; padding: 3px 6px !important; }
  #appRoot .deduct-value { font-size: clamp(22px, 10dvh, 42px) !important; }
  #appRoot .deduct-badge { width: clamp(29px, 20dvh, 85px) !important; height: clamp(29px, 20dvh, 85px) !important; font-size: clamp(15px, 6dvh, 24px) !important; }
  #appRoot .mid-score { gap: clamp(3px, .9dvh, 6px) !important; padding: 3px 6px !important; }
  #appRoot .mid-score .pre-row { height: clamp(28px, 8dvh, 37px) !important; }
  #appRoot .mid-score .tech-total-row { height: clamp(29px, 8dvh, 36px) !important; }
  #appRoot .mid-score .send { height: clamp(35px, 10dvh, 46px) !important; }
  #appRoot .pre-label { font-size: clamp(10px, 5dvh, 22px) !important; }
  #appRoot .tech-total-title { font-size: clamp(11px, 5dvh, 22px) !important; }
  #appRoot .send { font-size: clamp(20px, 7dvh, 30px) !important; }
}

</style>
</head>
<body>
{% if error or event_error %}
<div id="loginTopError" class="login-top-error">
    {{ event_error if event_error else 'INVALID EVENT OR PASSWORD!' }}
</div>

<script>
setTimeout(()=>{
    const e = document.getElementById('loginTopError');
    if(e){
        e.style.opacity = '0';
        e.style.transform = 'translateX(-50%) translateY(-20px)';
        setTimeout(()=>e.remove(),600);
    }
},3000);
</script>
{% endif %}
<div class="rotate-lock">
  <div class="rotate-lock-box">
    <div class="rotate-lock-icon">📱↔️</div>
    <div class="rotate-lock-title">PLEASE ROTATE PHONE</div>
    <div class="rotate-lock-text">
      Màn hình chấm thi chỉ dùng chế độ ngang.<br>
      Vui lòng xoay điện thoại sang ngang để tiếp tục.
    </div>
  </div>
</div>
{% if not judge_id %}
<div class="page center">
  <form class="login-card" method="post" action="{{ request.script_root }}/login">
    <div class="logo">POOMSAE SCORING</div>
    <div class="sub">JUDGE CONTROL PANEL</div>
    <div class="field">
        <label>SELECT EVENT</label>
        <select name="event_code" required>
            <option value="" disabled {% if not chosen_event %}selected{% endif %}>Select competition</option>
            {% for evt in events %}
            <option value="{{evt.code}}" {% if chosen_event == evt.code %}selected{% endif %}>{{evt.date + ' · ' if evt.date else ''}}{{evt.name}} ({{evt.code}})</option>
            {% endfor %}
        </select>
    </div>
    <div class="field">
        <label>SELECT JUDGE</label>
        <select name="judge_id">
            {% for i in range(1,8) %}
            <option>Judge {{i}}</option>
            {% endfor %}
        </select>
    </div>

    <div class="field">
        <label>SELECT COURT</label>
        <select name="court_id">
            {% for i in range(0,7) %}
            <option>Court {{i}}</option>
            {% endfor %}
        </select>
    </div>

    <div class="field">
        <label>PASSWORD</label>
        <input
            type="password"
            name="password"
            placeholder="Enter Password"
            autocomplete="off">
    </div>

    <button class="btn btn-main" type="submit">
        ACCEPT
    </button>
  </form>
</div>
{% else %}
<button class="logout" onclick="openLogoutPopup()">Logout</button>
<div id="appRoot"></div>
<div id="toast" class="toast"></div>
<div id="logoutConfirm" style="
    position:fixed;
    inset:0;
    background:rgba(0,0,0,.55);

    backdrop-filter:blur(4px);
    -webkit-backdrop-filter:blur(4px);

    display:none;
    align-items:center;
    justify-content:center;

    z-index:99999;
">

    <div style="
        width:min(420px,86vw);
        border-radius:26px;

        background:rgba(8,12,20,.96);

        border:2px solid rgba(255,255,255,.08);

        padding:28px 24px;

        text-align:center;

        box-shadow:
            0 0 40px rgba(0,0,0,.55),
            0 0 24px rgba(0,255,180,.08);
    ">

        <div style="
            font-size:clamp(22px,4vw,38px);
            font-weight:900;
            color:#ffd54a;
            margin-bottom:14px;
        ">
            LOG OUT
        </div>

        <div style="
            font-size:clamp(14px,2vw,22px);
            color:#d7dde7;
            margin-bottom:28px;
        ">
            Do you want to log out?
        </div>

        <div style="
            display:flex;
            gap:16px;
            justify-content:center;
        ">

            <button onclick="confirmLogout()" style="
                border:none;
                border-radius:16px;
                padding:14px 34px;
                font-size:20px;
                font-weight:900;
                cursor:pointer;

                background:#00d26a;
                color:white;
            ">
                YES
            </button>

            <button onclick="closeLogoutPopup()" style="
                border:none;
                border-radius:16px;
                padding:14px 34px;
                font-size:20px;
                font-weight:900;
                cursor:pointer;

                background:#2b3444;
                color:white;
            ">
                NO
            </button>

        </div>
    </div>
</div>

<div id="sliderPopup" class="slider-popup">
  <div class="slider-popup-box">
    <div id="sliderPopupTitle" class="slider-popup-title">Score</div>
    <div id="sliderPopupValue" class="slider-popup-value">2.0</div>
    <input id="sliderPopupRange" class="popup-range" type="range" min="0.5" max="2" step="0.01" value="2">

    <div id="sliderPopupScale" class="slider-popup-scale"></div>

    <div class="slider-popup-actions one-btn">
    </div>
  </div>
</div>
<script>
const STATE = {
    judge_id: {{judge_id|tojson}},
    court_id: {{court_id|tojson}},
    screen:'wait',
    match:null,
    ready:{slot:'',poomsae:''},
    freestyleTech:null,
    freestylePre:null,

    fsDeduction:{
        minus1:0,
        minus3:0
    },
    specialScore:null,

    specialDeduction:{
        minus3:0,
        minus5:0
    },

    scores:{},
    timerText:'00:00',
    timerColor:'#ffef00',
    lastGoodTime:{match:'', minutes:0, seconds:0, control:''},
    noActiveCount:0,
    localLockScreen:'',
    readyRenderKey:''  // Tránh dựng lại ảnh cờ khi poll không đổi trạng thái.
};
async function lockLandscapeAndFullscreen(){
    try{
        if(document.documentElement.requestFullscreen && !document.fullscreenElement){
            await document.documentElement.requestFullscreen();
        }
    }catch(e){}

    try{
        if(screen.orientation && screen.orientation.lock){
            await screen.orientation.lock('landscape');
        }
    }catch(e){}

    setTimeout(()=>window.scrollTo(0,1),80);
}

window.addEventListener('load',()=>setTimeout(()=>window.scrollTo(0,1),120));
window.addEventListener('orientationchange',()=>setTimeout(()=>window.scrollTo(0,1),250));
document.addEventListener('click',lockLandscapeAndFullscreen,{once:true});
document.addEventListener('touchend',lockLandscapeAndFullscreen,{once:true});

const SLIDER_SCALE_VALUES = [
    0.5, 0.6, 0.7, 0.8, 0.9,
    1.0, 1.1, 1.2, 1.3, 1.4,
    1.5, 1.6, 1.7, 1.8, 1.9,
    2.0
];

function buildSliderPopupScale(){

    const host = document.getElementById('sliderPopupScale');

    if(!host) return;

    if(host.dataset.ready === '1') return;

    host.dataset.ready = '1';

    host.innerHTML = `
        <div class="slider-popup-scale-inner">
            ${
                SLIDER_SCALE_VALUES.map((value, index)=>`

                    <div class="slider-popup-tick ${index % 5 === 0 || index === 15 ? 'major' : ''}">
                        <div class="slider-popup-tick-line"></div>
                        <div class="slider-popup-tick-label">${value.toFixed(1)}</div>
                    </div>

                `).join('')
            }
        </div>
    `;
}

const JUDGE_SESSION = new URLSearchParams(window.location.search).get('judge_session') || '';
function api(url, options={}){
    const headers = {'Content-Type':'application/json', ...options.headers};
    if(JUDGE_SESSION) headers['X-Judge-Session'] = JUDGE_SESSION;
    return fetch({{ request.script_root|tojson }}+url,{...options, headers, cache:'no-store'}).then(r=>r.json());
}
function openLogoutPopup(){
    document.getElementById('logoutConfirm').style.display='flex';
}

function closeLogoutPopup(){
    document.getElementById('logoutConfirm').style.display='none';
}

function confirmLogout(){

    // Do not clear shared localStorage: other Judge tabs may be scoring.
    // This tab is invalidated by leaving its signed URL.

    // logout current tab
    window.location.href={{ (request.script_root ~ '/logout')|tojson }};
}
function fmtName(s){return s||''}
function toast(msg, type='ok', after){
    const t = document.getElementById('toast');

    t.className = 'toast ' + type;
    t.innerText = msg;
    t.style.display = 'block';

    const delay = type === 'ok' ? 1200 : 1000;

    setTimeout(()=>{
        t.style.display = 'none';
        if(after) after();
    }, delay);
}function scoreInit(){return {b03:0,b01:0,r03:0,r01:0,bt:4,rt:4,bs:2,br:2,bk:2,rs:2,rr:2,rk:2,c03:0,c01:0,ct:4,cs:2,cr:2,ck:2,demo:{Yeonmu:2,'Self-Defense':2,Breaking:5,Composition:1},ded:{'-0.1':0,'-0.3':0,'-0.5':0}}}
function resetScores(){STATE.scores=scoreInit()}
function scoreDraftKey(){
    const match = STATE.match?.Match || 'no_match';
    const slot = STATE.ready?.slot || 'no_slot';
    const judge = STATE.judge_id || 'no_judge';

    let screenGroup = STATE.screen;
    if(
        STATE.screen === 'freestyle_tech' ||
        STATE.screen === 'freestyle_pre' ||
        STATE.screen === 'freestyle_deduction'
    ){
        screenGroup = 'freestyle';
    }

    if(
        STATE.screen === 'demonstration' ||
        STATE.screen === 'demonstration_deduction'
    ){
        screenGroup = 'demonstration';
    }

    if(
        STATE.screen === 'freestyle_breaking' ||
        STATE.screen === 'vertical_spinning_kicks' ||
        STATE.screen === 'jumping_multiple_kicks'
    ){
        screenGroup =
            STATE.screen;
    }
    return `poomsae_draft_${judge}_${match}_${slot}_${screenGroup}`;
}

function saveScoreDraft(){
    try{
        localStorage.setItem(scoreDraftKey(), JSON.stringify({
            scores: STATE.scores,

            fsTech: STATE.fsTech || null,
            fsPre: STATE.fsPre || null,

            freestyleTech: STATE.freestyleTech || null,
            freestylePre: STATE.freestylePre || null,

            fsDeduction: STATE.fsDeduction || {
                minus1:0,
                minus3:0
            }
        }));
    }catch(e){}
}

function loadScoreDraft(){
    try{
        const raw = localStorage.getItem(scoreDraftKey());
        if(!raw) return false;

        const data = JSON.parse(raw);
        if(data.scores) STATE.scores = data.scores;
        if(data.fsTech){
            STATE.fsTech = data.fsTech;
        }

        if(data.fsPre){
            STATE.fsPre = data.fsPre;
        }

        if(data.freestyleTech){
            STATE.freestyleTech = data.freestyleTech;
        }

        if(data.freestylePre){
            STATE.freestylePre = data.freestylePre;
        }

        if(data.fsDeduction){
            STATE.fsDeduction = {
                minus1:Number(data.fsDeduction.minus1 || 0),
                minus3:Number(data.fsDeduction.minus3 || 0)
            };
        }

        if(data.specialScore){

            STATE.specialScore =
                data.specialScore;
        }


        if(data.specialDeduction){

            STATE.specialDeduction = {

                minus3:Number(
                    data.specialDeduction.minus3 || 0
                ),

                minus5:Number(
                    data.specialDeduction.minus5 || 0
                )
            };
        }

        return true;
    }catch(e){
        return false;
    }
}

function clearScoreDraft(){
    try{
        const match = STATE.match?.Match || '';
        const judge = STATE.judge_id || '';

        const keysToDelete = [];

        for(let i = 0; i < localStorage.length; i++){
            const key = localStorage.key(i);

            if(!key) continue;

            // Xóa toàn bộ draft của đúng Judge + đúng Match
            if(
                key.startsWith(`poomsae_draft_${judge}_${match}_`) ||
                key.startsWith(`poomsae_draft_${judge}_no_match_`)
            ){
                keysToDelete.push(key);
            }
        }

        keysToDelete.forEach(k => localStorage.removeItem(k));

    }catch(e){}
}

function pageWait(title='WAITING START MATCH',sub='System is waiting for controller action...'){
    document.getElementById('appRoot').innerHTML=`
    <div class="ready-page">
        <div class="ready-title">POOMSAE SCORING SYSTEM</div>

        <div style="
            flex:1;
            display:flex;
            align-items:flex-start;
            justify-content:center;
            padding:clamp(4px,1vh,10px) 10px 10px 10px;
            overflow:hidden;
        ">
            <div style="
                width:min(780px,88vw);

                min-height:clamp(260px,42vh,420px);

                max-height:82dvh;

                padding:
                    clamp(24px,4vh,44px)
                    clamp(28px,5vw,58px);

                overflow:hidden;

                display:flex;
                flex-direction:column;
                justify-content:center;
                border-radius:clamp(22px,4vw,40px);
                background:rgba(8,18,32,.42);
                border:1px solid rgba(80,220,255,.35);
                box-shadow:0 0 40px rgba(0,220,255,.22);
                text-align:center;
                backdrop-filter:blur(8px);
                -webkit-backdrop-filter:blur(8px);
            ">
                <div style="
                    font-size:clamp(34px,6vw,72px);
                    color:var(--gold);
                    font-weight:900;
                    line-height:1;
                    margin-bottom:clamp(10px,2vh,20px);
                ">${title}</div>

                <div style="
                    font-size:clamp(16px,2.8vw,28px);
                    color:#dcecff;
                    font-weight:900;
                    margin-bottom:clamp(16px,3vh,30px);
                ">${sub}</div>

                <div style="
                    display:inline-block;
                    padding:10px 22px;
                    border-radius:999px;
                    background:rgba(0,255,117,.12);
                    border:1px solid rgba(0,255,117,.35);
                    color:#51ff8c;
                    font-size:clamp(16px,2.8vw,26px);
                    font-weight:900;
                ">${STATE.judge_id} | ${STATE.court_id}</div>
            </div>
        </div>
    </div>`;
}
function flag(noc){
    const code = (noc||'').split(' ')[0].toUpperCase();

    return `
    <div class="flag">
        <img src="flags/${code}.png"
             onerror="this.style.display='none'; this.parentElement.innerHTML='<span class=&quot;flag-text&quot;>${code}</span>'">
    </div>`;
}
function pageReady(row,readyText){
    let method = (row.Method || '').toLowerCase();
    let division = (row.Division || '').toLowerCase();

    let isCut = method.includes('cut');
    let isRecognizedCutoff = isCut && String(row['Poomsae Type'] || '').toLowerCase() === 'recognized';
    let isObo = division.includes('pair') || division.includes('team');
    let poomsaeType = String(row['Poomsae Type'] || '').toLowerCase();
    let isOneSide = [
        'freestyle',
        'demonstration',
        'freestyle breaking',
        'vertical spinning kicks',
        'jumping multiple kicks'
    ].includes(poomsaeType);

    let judgeKey = String(STATE.judge_id || '').trim();   // ví dụ: Judge 1

    let judgeStatus = String(
        row[judgeKey] || STATE.judge_status || ''
    ).trim().toLowerCase();

    console.log("READY DEBUG:", judgeKey, judgeStatus);

    let poomsaeName = STATE.ready?.poomsae || '';
    let hasPoomsae = !!poomsaeName;

    let info = `
        <div class="ready-info">
            ${row.Match || ''} | ${row.Phase || ''}<br>
            ${row.Division || ''}
            ${hasPoomsae && !poomsaeName.toLowerCase().includes('waiting') ? `<br><span class="ready-poomsae-pill">${poomsaeName}</span>` : ``}
        </div>
    `;

    let body = '';

    function dimStyle(active){
        if(active){
            return '';
        }

        return `
            filter:grayscale(1);
            opacity:.34;
            border-color:rgba(255,255,255,.12)!important;
        `;
    }

    function textDimStyle(active, color){
        if(active){
            return `color:${color}`;
        }

        return `
            color:rgba(255,255,255,.42);
            text-shadow:none;
        `;
    }

    // =========================
    // RECOGNIZED ONE BY ONE
    // Pair / Team: Blue trước, Red sau
    // =========================
    let blueActive = true;
    let redActive = true;

    if(isObo && !isCut){
        if(judgeStatus.startsWith('red')){
            blueActive = false;
            redActive = true;
        }else if(judgeStatus.startsWith('blue')){
            blueActive = true;
            redActive = false;
        }
    }

    if(isCut || isOneSide){
        body = `
        <div class="ready-body cutoff">
            <div class="ready-team cutoff-one">
                <div class="ready-team-top">
                    ${flag(row['NOC Blue'])}
                    <div class="ready-noc" style="color:#ffd447">${row['NOC Blue'] || ''}</div>
                </div>
                <div class="ready-athlete">${row['Athlete Blue'] || ''}</div>
            </div>
        </div>`;
    }else{
        body = `
        <div class="ready-body single">
            <div class="ready-team blue" style="${dimStyle(blueActive)}">
                <div class="ready-team-top">
                    ${flag(row['NOC Blue'])}
                    <div class="ready-noc" style="${textDimStyle(blueActive, '#5ab5ff')}">
                        ${row['NOC Blue'] || ''}
                    </div>
                </div>

                <div class="ready-athlete" style="${textDimStyle(blueActive, '#5ab5ff')}">
                    ${row['Athlete Blue'] || ''}
                </div>
            </div>

            <div class="ready-team red" style="${dimStyle(redActive)}">
                <div class="ready-team-top">
                    <div class="ready-noc" style="${textDimStyle(redActive, '#ff7878')}">
                        ${row['NOC Red'] || ''}
                    </div>
                    ${flag(row['NOC Red'])}
                </div>

                <div class="ready-athlete" style="${textDimStyle(redActive, '#ff7878')}">
                    ${row['Athlete Red'] || ''}
                </div>
            </div>
        </div>`;
    }

    document.getElementById('appRoot').innerHTML = `
    <div class="ready-page">
        <div>
            <div class="ready-title" style="
                margin-bottom:clamp(8px,2vh,18px);
            ">
                POOMSAE SCORING SYSTEM
            </div>
            ${info}
        </div>
        ${body}
        ${hasPoomsae && !poomsaeName.toLowerCase().includes('waiting') && readyText.includes('Waiting Start Time') ? `
            <div class="waiting-glass">
                WAITING TIME...
                <span>Please wait for Start</span>
            </div>
        ` : ``}
    </div>`;
}


function header(row,title,total=''){

    return `
    <div class="score-top">

        <!-- LEFT -->
        <div class="top-side blue-bg">

            <div style="
                display:flex;
                gap:12px;
                align-items:center;
            ">

                ${flag(row['NOC Blue'])}

                <div
                    class="noc"
                    style="
                        font-size:36px;
                        color:#5ab5ff;
                    "
                >
                    ${row['NOC Blue'] || ''}
                </div>

            </div>


            <div
                class="athlete"
                style="
                    text-align:left;
                    color:#5ab5ff;
                "
            >
                ${row['Athlete Blue'] || ''}
            </div>

        </div>


        <!-- CENTER -->
        <div class="center-info">

            <div class="division">
                ${title || row.Division || ''}
            </div>


            <div
                class="timer"
                id="timer"
                style="
                    color:${STATE.timerColor}
                "
            >
                ${STATE.timerText}
            </div>


            <div class="match-line">
                ${row.Match || ''} - ${row.Phase || ''}
            </div>


            <div class="poomsae-box">
                Poomsae&nbsp;
                [ ${STATE.ready.poomsae || ''} ]
            </div>

        </div>


        <!-- RIGHT -->
        <div
            class="top-side"
            style="
                align-items:center;
                justify-content:center;
            "
        >

            <div style="
                font-size:36px;
                font-weight:900;
            ">
                ${total}
            </div>


            <div style="
                font-size:24px;
                color:#00ff75;
                font-weight:900;
            ">
                ${STATE.judge_id}
            </div>

        </div>

    </div>
    `;
}


/* =========================================================
   DEMONSTRATION HEADER
========================================================= */

function demoHeader(row,title){

    const technical =
        getDemoScoreTotal();


    const deduct =
        getDemoDeductionTotal();


    const total =
        getDemoFinalTotal();


    return `

    <div class="score-top">


        <!-- LEFT -->
        <div class="top-side blue-bg">

            <div style="
                display:flex;
                gap:12px;
                align-items:center;
            ">

                ${flag(row['NOC Blue'])}


                <div
                    class="noc"
                    style="
                        font-size:36px;
                        color:#5ab5ff;
                    "
                >
                    ${row['NOC Blue'] || ''}
                </div>

            </div>


            <div
                class="athlete"
                style="
                    text-align:left;
                    color:#5ab5ff;
                "
            >
                ${row['Athlete Blue'] || ''}
            </div>

        </div>


        <!-- CENTER -->
        <div class="center-info">

            <div class="division">
                ${title}
            </div>


            <div
                class="timer"
                id="timer"
                style="
                    color:${STATE.timerColor}
                "
            >
                ${STATE.timerText}
            </div>


            <div class="match-line">
                ${row.Match || ''} - ${row.Phase || ''}
            </div>


            <div class="poomsae-box">
                Poomsae&nbsp;
                [ ${STATE.ready.poomsae || ''} ]
            </div>

        </div>


        <!-- RIGHT -->
        <div class="demo-header-summary">


            <div class="demo-header-top">


                <div class="demo-header-score">

                    <div class="demo-header-label">
                        TECHNICAL
                    </div>

                    <div class="demo-header-number">
                        ${technical.toFixed(1)}
                    </div>

                </div>


                <div class="demo-header-score">

                    <div class="
                        demo-header-label
                        deduct
                    ">
                        DEDUCT
                    </div>

                    <div class="
                        demo-header-number
                        deduct
                    ">
                        ${deduct.toFixed(1)}
                    </div>

                </div>


            </div>


            <div class="demo-header-total">

                <div class="demo-header-total-label">
                    TOTAL
                </div>

                <div class="demo-header-total-number">
                    ${total.toFixed(1)}
                </div>

            </div>


            <div class="demo-header-judge">
                ${STATE.judge_id}
            </div>


        </div>


    </div>
    `;
}

function getSpecialHeaderSummary(){

    let accuracySum = 0;
    let presentationSum = 0;
    let levelSkillSum = 0;


    const type = String(
        STATE.match?.['Poomsae Type'] || ''
    )
    .trim()
    .toLowerCase();


    const isBreaking =
        type === 'freestyle breaking';


    if(!STATE.specialScore){

        return {
            accuracy:0,
            presentation:0,
            level_skill:0,
            deduct:0,
            total:0,
            isBreaking:isBreaking
        };
    }


    Object.values(
        STATE.specialScore
    ).forEach(play=>{

        accuracySum += Number(
            play?.accuracy || 0
        );


        presentationSum += Number(
            play?.presentation || 0
        );


        if(isBreaking){

            levelSkillSum += Number(
                play?.level || 0
            );
        }

    });


    let accuracy = 0;

    let presentation = 0;

    let level_skill = 0;


    /* =====================================
       FREESTYLE BREAKING
       CỘNG THẲNG 4 PLAY
    ===================================== */

    if(isBreaking){

        accuracy =
            accuracySum;

        presentation =
            presentationSum;

        level_skill =
            levelSkillSum;

    }


    /* =====================================
       VSK / JMK
       TRUNG BÌNH 3 LƯỢT
    ===================================== */

    else{

        accuracy =
            accuracySum / 3;

        presentation =
            presentationSum / 3;

        level_skill =
            0;

    }


    /* =====================================
       DEDUCTION

       -3 = 0.3
       -5 = 0.5
    ===================================== */

    const deduct =
        (
            Number(
                STATE.specialDeduction?.minus3 || 0
            ) * 0.3

            +

            Number(
                STATE.specialDeduction?.minus5 || 0
            ) * 0.5
        );


    /* =====================================
       TOTAL
    ===================================== */

    const total =
        isBreaking

            ? (
                accuracy
                +
                presentation
                +
                level_skill
                -
                deduct
            )

            : (
                accuracy
                +
                presentation
                -
                deduct
            );


    return {
        accuracy,
        presentation,
        level_skill,
        deduct,
        total,
        isBreaking
    };
}

function specialHeader(row,title){

    const summary =
        getSpecialHeaderSummary();


    const breakingTop = `

        <div class="special-header-top breaking">


            <div class="special-header-score">

                <div
                    class="
                        special-header-label
                        accuracy
                    "
                >
                    ACCURACY
                </div>

                <div
                    class="
                        special-header-number
                        accuracy
                    "
                >
                    ${summary.accuracy.toFixed(1)}
                </div>

            </div>


            <div class="special-header-score">

                <div
                    class="
                        special-header-label
                        presentation
                    "
                >
                    PRESENTATION
                </div>

                <div
                    class="
                        special-header-number
                        presentation
                    "
                >
                    ${summary.presentation.toFixed(1)}
                </div>

            </div>


            <div class="special-header-score">

                <div
                    class="
                        special-header-label
                        level
                    "
                >
                    LV. SKILL
                </div>

                <div
                    class="
                        special-header-number
                        level
                    "
                >
                    ${summary.level_skill.toFixed(1)}
                </div>

            </div>


        </div>
    `;


    const normalTop = `

        <div class="special-header-top">


            <div class="special-header-score">

                <div
                    class="
                        special-header-label
                        accuracy
                    "
                >
                    ACCURACY
                </div>

                <div
                    class="
                        special-header-number
                        accuracy
                    "
                >
                    ${summary.accuracy.toFixed(1)}
                </div>

            </div>


            <div class="special-header-score">

                <div
                    class="
                        special-header-label
                        presentation
                    "
                >
                    PRESENTATION
                </div>

                <div
                    class="
                        special-header-number
                        presentation
                    "
                >
                    ${summary.presentation.toFixed(1)}
                </div>

            </div>


        </div>
    `;


    return `

    <div class="score-top special-score-top">


        <!-- LEFT -->

        <div class="top-side blue-bg special-header-athlete">

            <div style="
                display:flex;
                gap:12px;
                align-items:center;
            ">

                ${flag(row['NOC Blue'])}


                <div
                    class="noc"
                    style="
                        font-size:36px;
                        color:#5ab5ff;
                    "
                >
                    ${row['NOC Blue'] || ''}
                </div>

            </div>


            <div
                class="athlete"
                style="
                    text-align:left;
                    color:#5ab5ff;
                "
            >
                ${row['Athlete Blue'] || ''}
            </div>

        </div>


        <!-- CENTER -->

        <div class="center-info">

            <div class="division">
                ${title}
            </div>


            <div
                class="timer"
                id="timer"
                style="
                    color:${STATE.timerColor}
                "
            >
                ${STATE.timerText}
            </div>


            <div class="match-line">

                ${row.Match || ''}
                -
                ${row.Phase || ''}

            </div>


            <div class="special-event-box">
                ${row['Poomsae Type'] || title || ''}
            </div>

        </div>


        <!-- RIGHT -->

        <div class="special-header-summary">


            ${
                summary.isBreaking
                    ? breakingTop
                    : normalTop
            }


            <div class="special-header-bottom">


                <div class="special-header-score">

                    <div
                        class="
                            special-header-label
                            deduct
                        "
                    >
                        DEDUCTION
                    </div>

                    <div
                        class="
                            special-header-number
                            deduct
                        "
                    >
                        ${summary.deduct.toFixed(1)}
                    </div>

                </div>


                <div class="special-header-score">

                    <div
                        class="
                            special-header-label
                            total
                        "
                    >
                        TOTAL
                    </div>

                    <div
                        class="
                            special-header-number
                            total
                        "
                    >
                        ${summary.total.toFixed(1)}
                    </div>

                </div>


            </div>


            <div class="special-header-judge">
                ${STATE.judge_id}
            </div>


        </div>


    </div>
    `;
}


function s(){return STATE.scores}
let CURRENT_SLIDER_KEY = null;
let CURRENT_SLIDER_RENDER = null;

function openScoreSlider(title,key,renderName){
    CURRENT_SLIDER_KEY = key;
    CURRENT_SLIDER_RENDER = renderName;

    const popup = document.getElementById('sliderPopup');
    const titleEl = document.getElementById('sliderPopupTitle');
    const valueEl = document.getElementById('sliderPopupValue');
    const rangeEl = document.getElementById('sliderPopupRange');

    titleEl.innerText = title;
    rangeEl.value = s()[key];
    valueEl.innerText = Number(rangeEl.value).toFixed(1);

    rangeEl.oninput = function(){
        valueEl.innerText = Number(this.value).toFixed(1);
    };

    rangeEl.onchange = function(){
        saveScoreSlider();
    };

    rangeEl.onpointerup = function(){
        saveScoreSlider();
    };

    rangeEl.ontouchend = function(){
        saveScoreSlider();
    };

    popup.style.display = 'flex';
}

function closeScoreSlider(){
    document.getElementById('sliderPopup').style.display = 'none';
    CURRENT_SLIDER_KEY = null;
    CURRENT_SLIDER_RENDER = null;
}

function saveScoreSlider(){
    if(!CURRENT_SLIDER_KEY) return;

    const rangeEl = document.getElementById('sliderPopupRange');
    s()[CURRENT_SLIDER_KEY] = Math.round(Number(rangeEl.value) * 10) / 10;
    saveScoreDraft();

    closeScoreSlider();

    if(CURRENT_SLIDER_RENDER === 'cutoff'){
        renderCutoff(false);
    }else{
        renderScoring(false);
    }
}
function setVal(k,v){s()[k]=Math.max(0,Math.round(v*10)/10); renderScoring(false)}
function inc(k){s()[k]++; if(k==='b03')s().bt=Math.max(0,Math.round((s().bt-.3)*10)/10); if(k==='b01')s().bt=Math.max(0,Math.round((s().bt-.1)*10)/10); if(k==='r03')s().rt=Math.max(0,Math.round((s().rt-.3)*10)/10); if(k==='r01')s().rt=Math.max(0,Math.round((s().rt-.1)*10)/10); if(k==='c03')s().ct=Math.max(0,Math.round((s().ct-.3)*10)/10); if(k==='c01')s().ct=Math.max(0,Math.round((s().ct-.1)*10)/10);saveScoreDraft(); renderScoring(false)}
function preSlider(title,bkey,rkey){
    let obo = (STATE.screen === 'recognized_obo');

    return `
    <div class="pre-row">
        <button class="pre-btn blue-side-control"
            onclick="${obo ? "lockSide('blue'); " : ""}openScoreSlider('${title} BLUE','${bkey}','recognized')">
            ${s()[bkey].toFixed(1)}
        </button>

        <div class="pre-label">${title}</div>

        <button class="pre-btn red red-side-control"
            onclick="${obo ? "lockSide('red'); " : ""}openScoreSlider('${title} RED','${rkey}','recognized')">
            ${s()[rkey].toFixed(1)}
        </button>
    </div>`;
}
function lockSide(side){
    if(side === 'blue'){
        document.querySelectorAll('.red-side-control').forEach(b=>{
            b.disabled = true;
            b.classList.add('locked-control');
        });
        document.querySelectorAll('.blue-side-control').forEach(b=>{
            b.disabled = false;
            b.classList.remove('locked-control');
        });
    }

    if(side === 'red'){
        document.querySelectorAll('.blue-side-control').forEach(b=>{
            b.disabled = true;
            b.classList.add('locked-control');
        });
        document.querySelectorAll('.red-side-control').forEach(b=>{
            b.disabled = false;
            b.classList.remove('locked-control');
        });
    }
}

function askVal(k,min,max){let v=prompt('Input score '+min+' - '+max, s()[k]); if(v===null)return; v=parseFloat(v); if(isNaN(v))return; setVal(k,Math.min(max,Math.max(min,v)))}

function createVskJmkScore(){

    return {

        play1:{
            accuracy:null,
            presentation:null
        },

        play2:{
            accuracy:null,
            presentation:null
        },

        play3:{
            accuracy:null,
            presentation:null
        }
    };
}


function createBreakingScore(){

    return {

        play1:{
            accuracy:null,
            presentation:null,
            level:null
        },

        play2:{
            accuracy:null,
            presentation:null,
            level:null
        },

        play3:{
            accuracy:null,
            presentation:null,
            level:null
        },

        play4:{
            accuracy:null,
            presentation:null,
            level:null
        }
    };
}

function specialButtons(
    playKey,
    field,
    maxValue,
    type
){

    const current =
        STATE.specialScore?.[playKey]?.[field];


    let html = '';


    for(
        let value = 0;
        value <= maxValue;
        value++
    ){

        const label =
            maxValue === 5 &&
            type !== 'level'
                ? value.toFixed(1)
                : String(value);


        html += `

            <button
                class="
                    special-score-btn
                    ${type}

                    ${
                        current === value
                            ? 'selected'
                            : ''
                    }
                "

                onclick="
                    setSpecialScore(
                        '${playKey}',
                        '${field}',
                        ${value}
                    )
                "
            >
                ${label}
            </button>

        `;
    }


    return html;
}


function buildSpecialInlineScale(){

    let html = '';

    for(let i = 0; i <= 50; i++){

        const value = (i / 10);
        const isMajor = (i % 5 === 0);

        html += `
            <div class="special-inline-tick ${isMajor ? 'major' : ''}">
                <div class="special-inline-tick-line"></div>
                <div class="special-inline-tick-label">
                    ${isMajor ? value.toFixed(1) : ''}
                </div>
            </div>
        `;
    }

    return `
        <div class="special-inline-scale">
            <div class="special-inline-scale-inner">
                ${html}
            </div>
        </div>
    `;
}


function previewSpecialSlider(el){

    const box = el.closest('.special-slider-box');
    if(!box) return;

    const valueEl =
        box.querySelector('.special-slider-current');

    if(valueEl){
        valueEl.innerText =
            Number(el.value).toFixed(1);
    }
}


function commitSpecialSliderScore(
    el,
    playKey,
    field
){

    if(!STATE.specialScore?.[playKey]){
        return;
    }

    const scrollPosition =
        getSpecialScrollPosition();

    STATE.specialScore[playKey][field] =
        Math.round(Number(el.value) * 10) / 10;

    saveScoreDraft();

    renderSpecialScoring(false);

    restoreSpecialScrollPosition(
        scrollPosition
    );
}


function specialSliderRow(
    label,
    playKey,
    field,
    type
){

    const rawValue =
        STATE.specialScore?.[playKey]?.[field];

    const current =
        rawValue === null || rawValue === undefined
            ? 0
            : Number(rawValue);

    return `

        <div class="special-score-row slider-mode">

            <div class="special-score-name">
                ${label}
            </div>

            <div class="special-slider-box">

                <div class="special-slider-head">
                    <div class="special-slider-current ${type}">
                        ${current.toFixed(1)}
                    </div>
                </div>

                <input
                    type="range"
                    min="0"
                    max="5"
                    step="0.1"
                    value="${current.toFixed(1)}"
                    class="special-inline-range ${type}"
                    oninput="previewSpecialSlider(this)"
                    onchange="commitSpecialSliderScore(this, '${playKey}', '${field}')"
                >

                ${buildSpecialInlineScale()}

            </div>

        </div>
    `;
}

function buildDualSharedRuler(){

    let html = '';

    for(let i = 0; i <= 50; i++){

        const value = (i / 10).toFixed(1);
        const isMajor = (i % 5 === 0);

        html += `
            <div class="special-dual-tick ${isMajor ? 'major' : ''}">
                <div class="special-dual-tick-top"></div>
                <div class="special-dual-tick-label">
                    ${isMajor ? value : ''}
                </div>
                <div class="special-dual-tick-bottom"></div>
            </div>
        `;
    }

    return `
        <div class="special-dual-ruler">
            <div class="special-dual-ruler-grid">
                ${html}
            </div>
        </div>
    `;
}


function previewDualSlider(el){

    const wrap = el.closest('.special-dual-line');
    if(!wrap) return;

    const valueEl = wrap.querySelector('.special-dual-value');
    if(valueEl){
        valueEl.innerText = Number(el.value).toFixed(1);
    }
}


function commitDualSlider(el, playKey, field){

    if(!STATE.specialScore?.[playKey]){
        return;
    }

    const scrollPosition = getSpecialScrollPosition();

    STATE.specialScore[playKey][field] =
        Math.round(Number(el.value) * 10) / 10;

    saveScoreDraft();

    renderSpecialScoring(false);

    restoreSpecialScrollPosition(scrollPosition);
}


function specialDualSliderRow(playKey){

    const acc =
        Number(STATE.specialScore?.[playKey]?.accuracy ?? 0);

    const pre =
        Number(STATE.specialScore?.[playKey]?.presentation ?? 0);

    return `
        <div class="special-dual-score">

            <div class="special-dual-labels">
                <div class="special-dual-label">
                    Accuracy
                </div>

                <div class="special-dual-label">
                    Presentation
                </div>
            </div>

            <div class="special-dual-main">

                <div class="special-dual-line top">
                    <div class="special-dual-value accuracy">
                        ${acc.toFixed(1)}
                    </div>

                    <input
                        type="range"
                        min="0"
                        max="5"
                        step="0.1"
                        value="${acc.toFixed(1)}"
                        class="special-dual-slider"
                        oninput="previewDualSlider(this)"
                        onchange="commitDualSlider(this, '${playKey}', 'accuracy')"
                    >
                </div>

                ${buildDualSharedRuler()}

                <div class="special-dual-line bottom">
                    <input
                        type="range"
                        min="0"
                        max="5"
                        step="0.1"
                        value="${pre.toFixed(1)}"
                        class="special-dual-slider"
                        oninput="previewDualSlider(this)"
                        onchange="commitDualSlider(this, '${playKey}', 'presentation')"
                    >

                    <div class="special-dual-value presentation">
                        ${pre.toFixed(1)}
                    </div>
                </div>

            </div>

        </div>
    `;
}

function specialScoreRow(
    label,
    playKey,
    field,
    maxValue,
    type
){

    const selected =
        STATE.specialScore?.[playKey]?.[field];


    const columns =
        maxValue === 10
            ? 'eleven'
            : 'six';


    return `

        <div class="special-score-row">


            <div class="special-score-name">
                ${label}
            </div>


            <div
                class="
                    special-btn-grid
                    ${columns}

                    ${
                        selected !== null &&
                        selected !== undefined
                            ? 'scored'
                            : ''
                    }
                "
            >

                ${
                    specialButtons(
                        playKey,
                        field,
                        maxValue,
                        type
                    )
                }

            </div>

        </div>

    `;
}

/* =========================================================
   KEEP SPECIAL SCORING SCROLL POSITION
========================================================= */

function getSpecialScrollPosition(){

    const layout =
        document.querySelector('.special-layout');

    const page =
        document.querySelector('.score-page');

    return {
        layout:
            layout ? layout.scrollTop : 0,

        page:
            page ? page.scrollTop : 0
    };
}


function restoreSpecialScrollPosition(position){

    requestAnimationFrame(()=>{

        const layout =
            document.querySelector('.special-layout');

        const page =
            document.querySelector('.score-page');


        if(layout){
            layout.scrollTop =
                position?.layout || 0;
        }


        if(page){
            page.scrollTop =
                position?.page || 0;
        }

    });
}

function setSpecialScore(
    playKey,
    field,
    value
){

    if(!STATE.specialScore?.[playKey]){
        return;
    }


    /* Lưu vị trí đang cuộn */
    const scrollPosition =
        getSpecialScrollPosition();


    STATE.specialScore[playKey][field] =
        Number(value);


    saveScoreDraft();


    renderSpecialScoring(false);


    /* Trả lại đúng vị trí cũ */
    restoreSpecialScrollPosition(
        scrollPosition
    );
}

function addSpecialDeduction(type){

    const scrollPosition =
        getSpecialScrollPosition();


    if(type === 'minus3'){
        STATE.specialDeduction.minus3++;
    }

    if(type === 'minus5'){
        STATE.specialDeduction.minus5++;
    }


    saveScoreDraft();


    renderSpecialScoring(false);


    restoreSpecialScrollPosition(
        scrollPosition
    );
}


function removeSpecialDeduction(type){

    const scrollPosition =
        getSpecialScrollPosition();


    if(type === 'minus3'){

        STATE.specialDeduction.minus3 =
            Math.max(
                0,
                STATE.specialDeduction.minus3 - 1
            );
    }


    if(type === 'minus5'){

        STATE.specialDeduction.minus5 =
            Math.max(
                0,
                STATE.specialDeduction.minus5 - 1
            );
    }


    saveScoreDraft();


    renderSpecialScoring(false);


    restoreSpecialScrollPosition(
        scrollPosition
    );
}


function specialDeductionHtml(){

    const minus3 =
        Number(
            STATE.specialDeduction?.minus3 || 0
        );


    const minus5 =
        Number(
            STATE.specialDeduction?.minus5 || 0
        );


    return `

        <div class="special-deduction">


            <div class="special-ded-card">

                <button
                    class="special-ded-main"
                    onclick="
                        addSpecialDeduction(
                            'minus3'
                        )
                    "
                >
                    -3
                </button>


                <div class="special-ded-side">

                    <div class="special-ded-label">
                        COUNT
                    </div>

                    <div class="special-ded-count">
                        ${minus3}
                    </div>

                    <button
                        class="special-ded-undo"
                        onclick="
                            removeSpecialDeduction(
                                'minus3'
                            )
                        "
                    >
                        UNDO
                    </button>

                </div>

            </div>


            <div class="special-ded-card">

                <button
                    class="
                        special-ded-main
                        minus5
                    "
                    onclick="
                        addSpecialDeduction(
                            'minus5'
                        )
                    "
                >
                    -5
                </button>


                <div class="special-ded-side">

                    <div class="special-ded-label">
                        COUNT
                    </div>

                    <div class="special-ded-count">
                        ${minus5}
                    </div>

                    <button
                        class="special-ded-undo"
                        onclick="
                            removeSpecialDeduction(
                                'minus5'
                            )
                        "
                    >
                        UNDO
                    </button>

                </div>

            </div>


        </div>
    `;
}

function renderSpecialScoring(reset=true){

    const type =
        String(
            STATE.match?.['Poomsae Type'] || ''
        )
        .trim()
        .toLowerCase();


    if(reset){

        if(type === 'freestyle breaking'){

            STATE.specialScore =
                createBreakingScore();

        }else{

            STATE.specialScore =
                createVskJmkScore();
        }


        STATE.specialDeduction = {
            minus3:0,
            minus5:0
        };


        loadScoreDraft();
    }


    if(type === 'freestyle breaking'){

        return renderFreestyleBreaking();

    }


    return renderVskJmk();
}

function buildVskSharedRuler(){

    let html = '';

    for(let i = 0; i <= 50; i++){

        const value = (i / 10).toFixed(1);
        const isMajor = (i % 5 === 0);

        html += `
            <div class="vsk-ruler-tick ${isMajor ? 'major' : ''}">
                <div class="vsk-ruler-line top"></div>
                <div class="vsk-ruler-label">
                    ${isMajor ? value : ''}
                </div>
                <div class="vsk-ruler-line bottom"></div>
            </div>
        `;
    }

    return `
        <div class="vsk-shared-ruler">
            <div class="vsk-ruler-grid">
                ${html}
            </div>
        </div>
    `;
}

function vskBubbleLeft(value, maxValue=5){

    const percent =
        (Number(value || 0) / Number(maxValue)) * 100;

    return `clamp(16px, ${percent}%, calc(100% - 16px))`;
}

function moveVskBubble(input){

    const wrap = input.closest('.vsk-slider-shell');
    if(!wrap) return;

    const bubble = wrap.querySelector('.vsk-score-bubble');
    if(!bubble) return;

    const value =
        Number(input.value || 0);

    bubble.innerText = value.toFixed(1);
    bubble.style.left = vskBubbleLeft(value, Number(input.max || 5));
}

function commitVskSlider(input, playKey, field){

    const scrollPos =
        getSpecialScrollPosition();

    if(!STATE.specialScore?.[playKey]){
        return;
    }

    const value =
        Math.round(Number(input.value) * 10) / 10;

    STATE.specialScore[playKey][field] = value;

    saveScoreDraft();

    renderSpecialScoring(false);

    restoreSpecialScrollPosition(scrollPos);
}

function vskDualSliderBlock(playKey){

    const accuracy =
        Number(
            STATE.specialScore?.[playKey]?.accuracy ?? 0
        );

    const presentation =
        Number(
            STATE.specialScore?.[playKey]?.presentation ?? 0
        );

    return `
        <div class="vsk-dual-row">

            <div class="vsk-dual-labels">
                <div class="vsk-dual-label">
                    Accuracy
                </div>

                <div class="vsk-dual-label">
                    Presentation
                </div>
            </div>

            <div class="vsk-dual-main">

                <div class="vsk-slider-shell top">
                    <div
                        class="vsk-score-bubble acc"
                        style="left:${vskBubbleLeft(accuracy,5)}"
                    >
                        ${accuracy.toFixed(1)}
                    </div>

                    <input
                        class="vsk-slider"
                        type="range"
                        min="0"
                        max="5"
                        step="0.1"
                        value="${accuracy.toFixed(1)}"
                        oninput="moveVskBubble(this)"
                        onchange="commitVskSlider(this, '${playKey}', 'accuracy')"
                    >
                </div>

                ${buildVskSharedRuler()}

                <div class="vsk-slider-shell bottom">
                    <input
                        class="vsk-slider"
                        type="range"
                        min="0"
                        max="5"
                        step="0.1"
                        value="${presentation.toFixed(1)}"
                        oninput="moveVskBubble(this)"
                        onchange="commitVskSlider(this, '${playKey}', 'presentation')"
                    >

                    <div
                        class="vsk-score-bubble pre"
                        style="left:${vskBubbleLeft(presentation,5)}"
                    >
                        ${presentation.toFixed(1)}
                    </div>
                </div>

            </div>

        </div>
    `;
}

function renderVskJmk(){

    const row = STATE.match;

    const title =
        row['Poomsae Type'] || '';


    let plays = '';


    for(let i = 1; i <= 3; i++){

        const key =
            `play${i}`;


        const playName =
            i === 1
                ? '1st'
                : i === 2
                    ? '2nd'
                    : '3rd';


        plays += `

            <div class="special-play">

                <div class="special-play-title vsk-play-title">
                    ${playName}
                </div>

                ${
                    vskDualSliderBlock(key)
                }

            </div>
        `;
    }


    document.getElementById(
        'appRoot'
    ).innerHTML = `

        <div class="score-page special-score-page">

            ${
                specialHeader(
                    row,
                    title.toUpperCase()
                )
            }


            <div class="special-layout">

                <div class="special-panel">

                    ${plays}

                    ${specialDeductionHtml()}


                    <div class="special-send-wrap">

                        <button
                            class="
                                send
                                special-send
                            "

                            onclick="
                                sendSpecialScore()
                            "
                        >
                            SEND
                        </button>

                    </div>

                </div>

            </div>

        </div>
    `;


    pollTime();
}

function renderFreestyleBreaking(){

    const row = STATE.match;


    let plays = '';


    for(let i = 1; i <= 4; i++){

        const key =
            `play${i}`;


        const names = [
            '',
            '1st Play',
            '2nd Play',
            '3rd Play',
            '4th Play'
        ];


        plays += `

            <div class="special-play">

                <div class="special-play-title">
                    ${names[i]}
                </div>


                ${
                    specialScoreRow(
                        'Accuracy',
                        key,
                        'accuracy',
                        10,
                        'accuracy'
                    )
                }


                ${
                    specialScoreRow(
                        'Presentation',
                        key,
                        'presentation',
                        10,
                        'presentation'
                    )
                }


                ${
                    specialScoreRow(
                        'Lv of Perform Skill',
                        key,
                        'level',
                        5,
                        'level'
                    )
                }

            </div>
        `;
    }


    document.getElementById(
        'appRoot'
    ).innerHTML = `

        <div class="score-page special-score-page">
            ${
                specialHeader(
                    row,
                    'FREESTYLE BREAKING'
                )
            }


            <div class="special-layout">

                <div class="special-panel">

                    ${plays}

                    ${specialDeductionHtml()}


                    <div class="special-send-wrap">

                        <button
                            class="
                                send
                                special-send
                            "

                            onclick="
                                sendSpecialScore()
                            "
                        >
                            SEND
                        </button>

                    </div>

                </div>

            </div>

        </div>
    `;


    pollTime();
}

async function sendSpecialScore(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    if(!STATE.specialScore){

        return toast(
            'Score missing!',
            'warn'
        );
    }


    /* =====================================
       KIỂM TRA TẤT CẢ Ô ĐÃ CHẤM
    ===================================== */

    for(
        const [playKey, play]
        of Object.entries(STATE.specialScore)
    ){

        for(
            const [field, value]
            of Object.entries(play)
        ){

            if(
                value === null ||
                value === undefined
            ){

                return toast(
                    `Please score ${playKey} ${field}!`,
                    'warn'
                );
            }
        }
    }


    /* =====================================
       TÍNH HEADER SUMMARY
    ===================================== */

    const summary =
        getSpecialHeaderSummary();


    const body = {

        ready:
            STATE.ready,

        poomsae_type:
            STATE.match?.['Poomsae Type'] || '',

        scores:
            STATE.specialScore,

        deduction:{
            minus3:Number(
                STATE.specialDeduction?.minus3 || 0
            ),

            minus5:Number(
                STATE.specialDeduction?.minus5 || 0
            )
        },

        summary:{
            accuracy:
                Number(summary.accuracy || 0),

            presentation:
                Number(summary.presentation || 0),

            level_skill:
                Number(summary.level_skill || 0),

            deduction:
                Number(summary.deduct || 0),

            total:
                Number(summary.total || 0)
        }

    };


    const r = await api(
        '/api/send_special',
        {
            method:'POST',
            body:JSON.stringify(body)
        }
    );


    if(r.ok){

        toast(
            'Sent Success!',
            'ok',
            ()=>{

                clearScoreDraft();


                STATE.specialScore = null;


                STATE.specialDeduction = {
                    minus3:0,
                    minus5:0
                };


                STATE.localLockScreen = '';


                STATE.screen = 'wait';


                pageWait();

            }
        );

    }else{

        toast(
            r.error || 'Send error',
            'warn'
        );
    }
}

function renderScoring(reset=true){
    if(reset){
        resetScores();
        loadScoreDraft();
    }

    const row=STATE.match;
    const screen=STATE.screen;

    if(
        screen === 'vertical_spinning_kicks' ||
        screen === 'jumping_multiple_kicks' ||
        screen === 'freestyle_breaking'
    ){
        return renderSpecialScoring(reset);
    }

    if(screen==='recognized_cutoff')return renderCutoff(reset);
    if(screen==='freestyle_tech'){
        return renderFreestyleTech(reset);
    }

    if(screen==='freestyle_pre'){
        return renderFreestylePre(reset);
    }

    if(screen==='freestyle_deduction'){
        return renderFreestyleDeduction(false);
    }

    if(screen==='demonstration'){
        return renderDemo(reset);
    }

    if(screen==='demonstration_deduction'){
        return renderDemoDeduction();
    }

    let bluePre=s().bs+s().br+s().bk;
    let redPre=s().rs+s().rr+s().rk;
    let hideSummary = (
        screen === 'recognized_obo' ||
        screen === 'recognized_sbs'
    );

    let obo = (screen === 'recognized_obo');

    document.getElementById('appRoot').innerHTML=`
    <div class="score-page">

        <div class="score-top">
            <div class="top-side blue-bg">
                <div style="display:flex;gap:12px;align-items:center">
                    ${flag(row['NOC Blue'])}
                    <div class="noc" style="color:#5ab5ff">${row['NOC Blue']||''}</div>
                </div>
                <div class="athlete" style="color:#5ab5ff;text-align:center">${row['Athlete Blue']||''}</div>
            </div>

            <div class="center-info">
                <div class="division">${row.Division||''}</div>
                <div class="timer" id="timer" style="color:${STATE.timerColor}">${STATE.timerText}</div>
                <div class="match-line">${row.Match||''} - ${row.Phase||''}</div>
                <div class="poomsae-box">Poomsae&nbsp; [ ${STATE.ready.poomsae||''} ]</div>
            </div>

            <div class="top-side red-bg">
                <div style="display:flex;gap:12px;align-items:center;justify-content:flex-end">
                    <div class="noc" style="color:#ff7878">${row['NOC Red']||''}</div>
                    ${flag(row['NOC Red'])}
                </div>
                <div class="athlete" style="color:#ff7878;text-align:center">${row['Athlete Red']||''}</div>
            </div>
        </div>

        <div class="score-bottom">
            <div class="side-score blue-bg">

                <button
                    class="deduct-pill blue-side-control"
                    onclick="lockSide('blue'); inc('b03')"
                >
                    <span class="deduct-value">0.3</span>
                    <span class="deduct-badge">${s().b03}</span>
                </button>

                <button
                    class="deduct-pill blue-side-control"
                    onclick="lockSide('blue'); inc('b01')"
                >
                    <span class="deduct-value">0.1</span>
                    <span class="deduct-badge">${s().b01}</span>
                </button>

            </div>

            <div class="mid-score">
                ${!hideSummary ? `
                <div class="score-summary">
                    <div class="score-summary-card">
                        <div class="score-summary-num">${s().bt.toFixed(1)}</div>
                        <div class="score-summary-label">TECHNICAL</div>
                    </div>

                    <div class="score-summary-card">
                        <div class="score-summary-num">${bluePre.toFixed(1)}</div>
                        <div class="score-summary-label">PRESENTATION</div>
                    </div>
                </div>
                ` : ``}

                <div class="tech-total-row">
                    <div class="tech-total blue-tech">
                        ${s().bt.toFixed(1)}
                    </div>

                    <div class="tech-total-title">
                        TECHNICAL
                    </div>

                    <div class="tech-total red-tech">
                        ${s().rt.toFixed(1)}
                    </div>
                </div>

                ${preSlider('Speed&Power','bs','rs')}
                ${preSlider('Rhythm','br','rr')}
                ${preSlider('Ki','bk','rk')}

                ${obo
                    ? `<div class="send-row">
                            <button class="send send-blue blue-side-control" onclick="sendRecognized('blue')">SEND<br>BLUE</button>
                            <button class="send send-red red-side-control" onclick="sendRecognized('red')">SEND<br>RED</button>
                       </div>`
                    : `<button class="send" onclick="sendRecognized('both')">SEND</button>`
                }

                <div style="text-align:center;color:#00ff75;font-weight:900">${STATE.judge_id}</div>
            </div>

            <div class="side-score red-bg">

                <button
                    class="deduct-pill red red-side-control"
                    onclick="lockSide('red'); inc('r03')"
                >
                    <span class="deduct-value">0.3</span>
                    <span class="deduct-badge">${s().r03}</span>
                </button>

                <button
                    class="deduct-pill red red-side-control"
                    onclick="lockSide('red'); inc('r01')"
                >
                    <span class="deduct-value">0.1</span>
                    <span class="deduct-badge">${s().r01}</span>
                </button>

            </div>
        </div>
    </div>`;
    if(obo){
        const st = (STATE.judge_status || '').toLowerCase();

        if(st.includes('red 1 ready') || st.includes('red 2 ready')){
            lockSide('red');
        }else{
            lockSide('blue');
        }
    }
    pollTime();
}
function cutoffSlider(title,key){
    return `
    <div class="pre-row cutoff">
        <button class="pre-btn" onclick="openScoreSlider('${title}','${key}','cutoff')">${s()[key].toFixed(1)}</button>
        <div class="pre-label">${title}</div>
    </div>`;
}
function renderCutoff(reset=true){
    if(reset){
        resetScores();
        loadScoreDraft();
    }

    const row=STATE.match;
    let pre=s().cs+s().cr+s().ck;

    document.getElementById('appRoot').innerHTML=`
    <div class="score-page">
        ${header(row,row.Division||'')}

        <div class="score-bottom">
            <div class="side-score">
                <button
                    class="deduct-pill"
                    onclick="inc('c03')"
                >
                    <span class="deduct-value">0.3</span>
                    <span class="deduct-badge">${s().c03}</span>
                </button>
            </div>

            <div class="mid-score">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">
                    <div class="score-card" style="background:#111">${s().ct.toFixed(1)}\nTECHNICAL</div>
                    <div class="score-card" style="background:#111">${pre.toFixed(1)}\nPRESENTATION</div>
                </div>

                ${cutoffSlider('Speed&Power','cs')}
                ${cutoffSlider('Rhythm','cr')}
                ${cutoffSlider('Ki','ck')}

                <button class="send" onclick="sendCutoff()">SEND</button>
                <div style="text-align:center;color:#00ff75;font-weight:900">${STATE.judge_id}</div>
            </div>

            <div class="side-score">
                <button
                    class="deduct-pill"
                    onclick="inc('c01')"
                >
                    <span class="deduct-value">0.1</span>
                    <span class="deduct-badge">${s().c01}</span>
                </button>
            </div>
        </div>
    </div>`;

    pollTime();
}
function scoreButtons(group,item){

    let html = '';

    for(let i = 1; i <= 10; i++){

        html += `
            <button
                class="
                    num-btn
                    ${group[item] === i
                        ? 'selected'
                        : ''
                    }
                "

                onclick="
                    setFsPre(
                        '${item}',
                        ${i}
                    )
                "
            >
                ${i}
            </button>
        `;
    }

    return html;
}

// =========================================================
// FREESTYLE TECHNICAL - NEW RULE
// =========================================================

const FS_TECH_SKILLS = [
    'Jumping Side Kick',
    'Multiple Kick',
    'Spining Kick',
    'Consecutive Kick',
    'Acrobatic Kick'
];


function createFsTechState(){

    return {

        'Jumping Side Kick': {
            proficiency:null,
            difficulty:null
        },

        'Multiple Kick': {
            proficiency:null,
            difficulty:null
        },

        'Spining Kick': {
            proficiency:null,
            difficulty:null
        },

        'Consecutive Kick': {
            proficiency:null,
            difficulty:null
        },

        'Acrobatic Kick': {
            proficiency:null,
            difficulty:null
        },

        'Basic Movement': {
            score:null
        }

    };
}


function getFsTechSkillTotal(skill){

    const item = STATE.fsTech?.[skill];

    if(!item){
        return 0;
    }


    if(skill === 'Basic Movement'){

        return item.score === null
            ? 0
            : Number(item.score);
    }


    const proficiency =
        item.proficiency === null
            ? 0
            : Number(item.proficiency);


    const difficulty =
        item.difficulty === null
            ? 0
            : Number(item.difficulty);


    return proficiency + difficulty;
}


function getFsTechTotal(){

    let total = 0;


    FS_TECH_SKILLS.forEach(skill=>{

        total += getFsTechSkillTotal(skill);

    });


    total += getFsTechSkillTotal('Basic Movement');


    return total;
}


/* ========================================
   TECHNICAL PROFICIENCY BUTTONS 0 - 7
======================================== */

function freestyleProficiencyButtons(skill){

    let html = '';

    const selected =
        STATE.fsTech?.[skill]?.proficiency;


    for(let i = 0; i <= 7; i++){

        html += `
            <button
                class="
                    fs-tech-btn
                    ${selected === i
                        ? 'proficiency-selected'
                        : ''}
                "

                onclick="
                    setFsTechProficiency(
                        '${skill}',
                        ${i}
                    )
                "
            >
                ${i}
            </button>
        `;
    }


    return html;
}


/* ========================================
   LEVEL OF DIFFICULTY +1 +2 +3
======================================== */

function freestyleDifficultyButtons(skill){

    let html = '';

    const selected =
        STATE.fsTech?.[skill]?.difficulty;


    for(let i = 1; i <= 3; i++){

        html += `
            <button
                class="
                    fs-tech-btn
                    difficulty
                    ${selected === i
                        ? 'difficulty-selected'
                        : ''}
                "

                onclick="
                    setFsTechDifficulty(
                        '${skill}',
                        ${i}
                    )
                "
            >
                +${i}
            </button>
        `;
    }


    return html;
}


/* ========================================
   BASIC MOVEMENT 1 - 10
======================================== */

function freestyleBasicButtons(){

    let html = '';

    const selected =
        STATE.fsTech?.['Basic Movement']?.score;


    for(let i = 1; i <= 10; i++){

        html += `
            <button
                class="
                    fs-tech-btn
                    ${selected === i
                        ? 'proficiency-selected'
                        : ''}
                "

                onclick="
                    setFsTechBasic(${i})
                "
            >
                ${i}
            </button>
        `;
    }


    return html;
}


/* ========================================
   RENDER
======================================== */

function renderFreestyleTech(reset=true){

    if(reset){

        STATE.fsTech = createFsTechState();

        loadScoreDraft();


        /*
        Backward protection:
        Nếu localStorage còn draft kiểu cũ,
        tự reset để tránh lỗi giao diện.
        */

        if(
            !STATE.fsTech ||
            typeof STATE.fsTech['Jumping Side Kick'] !== 'object'
        ){
            STATE.fsTech = createFsTechState();
        }
    }


    const row = STATE.match;

    const total = getFsTechTotal();


    const rows = FS_TECH_SKILLS.map(skill=>{

        const skillTotal =
            getFsTechSkillTotal(skill);


        return `
            <div class="fs-tech-skill-row">

                <div class="fs-tech-skill-label">
                    ${skill}

                    <span class="fs-skill-total">
                        ${skillTotal}
                    </span>
                </div>


                <div class="
                    fs-proficiency-box
                    ${STATE.fsTech?.[skill]?.proficiency !== null &&
                      STATE.fsTech?.[skill]?.proficiency !== undefined
                        ? 'scored'
                        : ''
                    }
                ">

                    <div class="fs-proficiency-grid">

                        ${freestyleProficiencyButtons(skill)}

                    </div>

                </div>


                <div class="
                    fs-difficulty-box
                    ${STATE.fsTech?.[skill]?.difficulty !== null &&
                      STATE.fsTech?.[skill]?.difficulty !== undefined
                        ? 'scored'
                        : ''
                    }
                ">

                    <div class="fs-difficulty-grid">

                        ${freestyleDifficultyButtons(skill)}

                    </div>

                </div>

            </div>
        `;

    }).join('');


    document.getElementById('appRoot').innerHTML = `

    <div class="score-page">

        ${header(
            row,
            'FREESTYLE TECHNICAL',
            'TOTAL: ' + total
        )}


        <div class="fs-mobile-layout">


            <div class="fs-header-row">

                <div class="fs-main-title">
                    TECHNICAL SCORING
                </div>


                <button
                    class="send fs-send"
                    onclick="sendFsTech()"
                >
                    SEND
                </button>

            </div>


            <div class="fs-tech-score-list">


                <div class="fs-tech-column-header">

                    <div class="fs-tech-header-left"></div>


                    <div
                        class="
                            fs-tech-header-box
                            fs-tech-header-proficiency
                        "
                    >
                        TECHNICAL PROFICIENCY
                    </div>


                    <div
                        class="
                            fs-tech-header-box
                            fs-tech-header-difficulty
                        "
                    >
                        LEVEL OF DIFFICULTY
                    </div>

                </div>


                ${rows}


                <div class="fs-basic-row">

                    <div class="fs-tech-skill-label">

                        Basic Movement

                        <span class="fs-skill-total">

                            ${getFsTechSkillTotal(
                                'Basic Movement'
                            )}

                        </span>

                    </div>


                    <div class="
                        fs-basic-box
                        ${STATE.fsTech?.['Basic Movement']?.score !== null &&
                          STATE.fsTech?.['Basic Movement']?.score !== undefined
                            ? 'scored'
                            : ''
                        }
                    ">

                        <div class="fs-basic-grid">

                            ${freestyleBasicButtons()}

                        </div>

                    </div>

                </div>


            </div>

        </div>

    </div>`;


    pollTime();
}


/* ========================================
   SELECT TECHNICAL PROFICIENCY
======================================== */

function setFsTechProficiency(skill,value){

    if(!STATE.fsTech?.[skill]){
        return;
    }


    STATE.fsTech[skill].proficiency = value;


    saveScoreDraft();

    renderFreestyleTech(false);
}


/* ========================================
   SELECT LEVEL OF DIFFICULTY

   Bấm lại đúng nút đang chọn
   => bỏ Level of Difficulty
======================================== */

function setFsTechDifficulty(skill,value){

    if(!STATE.fsTech?.[skill]){
        return;
    }


    if(
        STATE.fsTech[skill].difficulty === value
    ){

        STATE.fsTech[skill].difficulty = null;

    }else{

        STATE.fsTech[skill].difficulty = value;

    }


    saveScoreDraft();

    renderFreestyleTech(false);
}


/* ========================================
   BASIC MOVEMENT
======================================== */

function setFsTechBasic(value){

    STATE.fsTech['Basic Movement'].score = value;


    saveScoreDraft();

    renderFreestyleTech(false);
}

function renderFreestylePre(reset=true){
    if(reset){
        STATE.fsPre={
            'Creativeness':0,
            'Harmony':0,
            'Expression Of Energy':0,
            'Music & Choreography':0
        };
        loadScoreDraft();
    }

    let total=Object.values(STATE.fsPre).reduce((a,b)=>a+b,0);
    const row=STATE.match;

    let rows=Object.keys(STATE.fsPre).map(k=>`
        <div class="fs-score-row fs-pre-row">
            <div class="fs-score-label">${k}</div>
            <div class="
                fs-button-grid
                ${STATE.fsPre[k] !== 0
                    ? 'scored'
                    : ''
                }
            ">

                ${scoreButtons(STATE.fsPre,k)}

            </div>
        </div>
    `).join('');

    document.getElementById('appRoot').innerHTML=`
    <div class="score-page">
        ${header(row,'FREESTYLE PRESENTATION','TOTAL: '+total)}

        <div class="fs-mobile-layout">
            <div class="fs-header-row">
                <div class="fs-main-title">PRESENTATION SCORING</div>
                <button class="send fs-send" onclick="sendFsPre()">SEND</button>
            </div>

            <div class="fs-score-list fs-pre-list">
                ${rows}
            </div>
        </div>
    </div>`;

    pollTime();
}
function setFsPre(k,v){
    STATE.fsPre[k]=v;
    saveScoreDraft();
    renderFreestylePre(false);
}

// =========================================================
// FREESTYLE DEDUCTION
// =========================================================

function getFreestylePreTotal(){

    if(STATE.freestylePre){

        return Object.values(STATE.freestylePre)
            .reduce(
                (sum,value)=>sum + Number(value || 0),
                0
            );
    }


    if(STATE.fsPre){

        return Object.values(STATE.fsPre)
            .reduce(
                (sum,value)=>sum + Number(value || 0),
                0
            );
    }


    return 0;
}


function getFreestyleTechnicalTotal(){

    return Number(
        STATE.freestyleTech?.Tech || 0
    );
}


function getFreestyleDeductionTotal(){

    const minus1 =
        Number(
            STATE.fsDeduction?.minus1 || 0
        );


    const minus3 =
        Number(
            STATE.fsDeduction?.minus3 || 0
        );


    return (
        minus1 * 1 +
        minus3 * 3
    );
}


function getFreestyleFinalTotal(){

    return (
        getFreestyleTechnicalTotal() +
        getFreestylePreTotal() -
        getFreestyleDeductionTotal()
    );
}


function renderFreestyleDeduction(reset=false){

    /*
    reset=false:
    Không reset counter mỗi lần render.
    Chỉ đảm bảo STATE tồn tại.
    */

    if(
        !STATE.fsDeduction ||
        typeof STATE.fsDeduction !== 'object'
    ){

        STATE.fsDeduction = {
            minus1:0,
            minus3:0
        };
    }


    loadScoreDraft();


    const row = STATE.match;


    const technical =
        getFreestyleTechnicalTotal();


    const presentation =
        getFreestylePreTotal();


    const deduction =
        getFreestyleDeductionTotal();


    const finalTotal =
        getFreestyleFinalTotal();


    const minus1 =
        Number(
            STATE.fsDeduction.minus1 || 0
        );


    const minus3 =
        Number(
            STATE.fsDeduction.minus3 || 0
        );


    document.getElementById('appRoot').innerHTML = `

    <div class="score-page">

        ${header(
            row,
            'FREESTYLE DEDUCTION',
            'TOTAL: ' + finalTotal
        )}


        <div class="fs-deduction-layout">


            <div class="fs-deduction-controls">


                <!-- =========================
                     -1
                ========================== -->

                <div class="fs-deduction-card">

                    <button
                        class="fs-deduction-main-btn"

                        onclick="
                            addFsDeduction('minus1')
                        "
                    >
                        -1
                    </button>


                    <div class="fs-deduction-count-box">

                        <div class="fs-deduction-count-label">
                            COUNT
                        </div>


                        <div class="fs-deduction-count">

                            ${minus1}

                        </div>


                        <button
                            class="fs-deduction-undo"

                            ${minus1 <= 0
                                ? 'disabled'
                                : ''
                            }

                            onclick="
                                removeFsDeduction('minus1')
                            "
                        >
                            UNDO
                        </button>

                    </div>

                </div>


                <!-- =========================
                     -3
                ========================== -->

                <div class="fs-deduction-card">

                    <button
                        class="
                            fs-deduction-main-btn
                            minus3
                        "

                        onclick="
                            addFsDeduction('minus3')
                        "
                    >
                        -3
                    </button>


                    <div class="fs-deduction-count-box">

                        <div class="fs-deduction-count-label">
                            COUNT
                        </div>


                        <div class="fs-deduction-count">

                            ${minus3}

                        </div>


                        <button
                            class="fs-deduction-undo"

                            ${minus3 <= 0
                                ? 'disabled'
                                : ''
                            }

                            onclick="
                                removeFsDeduction('minus3')
                            "
                        >
                            UNDO
                        </button>

                    </div>

                </div>

            </div>


            <!-- =========================
                 FINAL SUMMARY
            ========================== -->

            <div class="fs-final-summary">


                <div class="fs-final-summary-label">
                    TECHNICAL
                </div>

                <div class="fs-final-summary-value">
                    ${technical}
                </div>


                <div class="fs-final-summary-label">
                    PRESENTATION
                </div>

                <div class="fs-final-summary-value">
                    ${presentation}
                </div>


                <div class="
                    fs-final-summary-label
                    deduction
                ">
                    DEDUCTION
                </div>

                <div class="
                    fs-final-summary-value
                    deduction
                ">
                    -${deduction}
                </div>


                <div class="fs-final-total-label">
                    TOTAL
                </div>

                <div class="fs-final-total-value">
                    ${finalTotal}
                </div>


            </div>


            <!-- =========================
                 SEND CHÍNH THỨC
            ========================== -->

            <div class="fs-final-send-wrap">

                <button
                    class="fs-final-send"
                    onclick="sendFsFinal()"
                >
                    SEND
                </button>

            </div>


        </div>

    </div>`;


    pollTime();
}


function addFsDeduction(type){

    if(type === 'minus1'){

        STATE.fsDeduction.minus1++;

    }else if(type === 'minus3'){

        STATE.fsDeduction.minus3++;

    }


    saveScoreDraft();

    renderFreestyleDeduction(false);
}


function removeFsDeduction(type){

    if(type === 'minus1'){

        STATE.fsDeduction.minus1 =
            Math.max(
                0,
                STATE.fsDeduction.minus1 - 1
            );

    }else if(type === 'minus3'){

        STATE.fsDeduction.minus3 =
            Math.max(
                0,
                STATE.fsDeduction.minus3 - 1
            );
    }


    saveScoreDraft();

    renderFreestyleDeduction(false);
}


// =========================================================
// DEMONSTRATION SCORING
// =========================================================

function buildDemoScale(maxScore){

    const step = 0.1;

    const count =
        Math.round(maxScore / step);


    let html = `
        <div class="demo-scale">
            <div class="demo-scale-line"></div>
            <div class="demo-scale-ticks">
    `;


    for(let i = 0; i <= count; i++){

        const value =
            Math.round(i * step * 10) / 10;


        const percent =
            (value / maxScore) * 100;


        /*
        Major:
        0 / 0.5 / 1.0 / 1.5...
        */

        const major =
            Math.abs(
                (value * 10) % 5
            ) < 0.001;


        /*
        Với mốc phụ:
        vẫn có vạch,
        nhưng chỉ hiện số xen kẽ để tránh quá dày
        ở Breaking 0 - 5.
        */

        let showLabel = major;


        if(maxScore <= 2){

            showLabel = true;

        }else if(maxScore <= 1){

            showLabel = true;
        }


        html += `
            <div
                class="
                    demo-scale-tick
                    ${major ? 'major' : ''}
                "

                style="
                    left:${percent}%;
                "
            >

                <div class="demo-scale-mark"></div>


                ${
                    showLabel
                    ? `
                        <div class="demo-scale-label">
                            ${value.toFixed(1)}
                        </div>
                      `
                    : ``
                }

            </div>
        `;
    }


    html += `
            </div>
        </div>
    `;


    return html;
}


function renderDemo(reset=true){

    if(reset){

        resetScores();

        loadScoreDraft();
    }


    const row = STATE.match;


    const totalDemo =
        Object.values(s().demo)
            .reduce(
                (sum,value)=>
                    sum + Number(value || 0),
                0
            );


    function demoSliderRow(
        title,
        key,
        maxScore
    ){

        const valueId =
            `demo_value_${
                key.replace(
                    /[^a-zA-Z0-9]/g,
                    '_'
                )
            }`;


        return `

        <div class="demo-score-row">


            <div class="demo-score-title">
                ${title}
            </div>


            <div
                id="${valueId}"
                class="demo-score-value"
            >
                ${Number(
                    s().demo[key]
                ).toFixed(1)}
            </div>


            <div class="demo-slider-wrap">


                <input
                    class="demo-score-slider"

                    type="range"

                    min="0"

                    max="${maxScore}"

                    step="0.01"

                    value="${s().demo[key]}"


                    oninput="
                        document
                            .getElementById(
                                '${valueId}'
                            )
                            .innerText =
                                Number(
                                    this.value
                                ).toFixed(1);
                    "


                    onchange="
                        setDemoScore(
                            '${key}',
                            this.value
                        );
                    "


                    onpointerup="
                        setDemoScore(
                            '${key}',
                            this.value
                        );
                    "


                    ontouchend="
                        setDemoScore(
                            '${key}',
                            this.value
                        );
                    "
                >


                ${buildDemoScale(maxScore)}


            </div>

        </div>
        `;
    }


    document.getElementById(
        'appRoot'
    ).innerHTML = `

    <div class="score-page">


        ${demoHeader(
            row,
            'DEMONSTRATION SCORING'
        )}


        <div class="demo-scoring-layout">


            <div class="demo-scoring-header">

                <div class="demo-scoring-title">
                    DEMONSTRATION SCORING
                </div>


                <button
                    class="send demo-score-send"
                    onclick="sendDemoScoring()"
                >
                    SEND
                </button>

            </div>


            <div class="demo-score-panel">


                ${demoSliderRow(
                    'Yeonmu',
                    'Yeonmu',
                    2
                )}


                ${demoSliderRow(
                    'Self-Defense',
                    'Self-Defense',
                    2
                )}


                ${demoSliderRow(
                    'Breaking',
                    'Breaking',
                    5
                )}


                ${demoSliderRow(
                    'Composition',
                    'Composition',
                    1
                )}


            </div>

        </div>

    </div>
    `;


    pollTime();
}


function setDemoScore(
    key,
    rawValue
){

    const value =
        Math.round(
            Number(rawValue) * 10
        ) / 10;


    s().demo[key] = value;


    saveScoreDraft();


    renderDemo(false);
}

// =========================================================
// DEMONSTRATION DEDUCTION
// =========================================================

function getDemoScoreTotal(){

    return Object.values(
        s().demo || {}
    ).reduce(
        (sum,value)=>
            sum + Number(value || 0),
        0
    );
}


function getDemoDeductionTotal(){

    const d01 =
        Number(
            s().ded?.['-0.1'] || 0
        );


    const d03 =
        Number(
            s().ded?.['-0.3'] || 0
        );


    const d05 =
        Number(
            s().ded?.['-0.5'] || 0
        );


    return Math.round(
        (
            d01 * 0.1 +
            d03 * 0.3 +
            d05 * 0.5
        ) * 10
    ) / 10;
}


function getDemoFinalTotal(){

    return Math.max(
        0,
        Math.round(
            (
                getDemoScoreTotal()
                -
                getDemoDeductionTotal()
            ) * 10
        ) / 10
    );
}


function renderDemoDeduction(){

    loadScoreDraft();


    const row = STATE.match;


    const scoreTotal =
        getDemoScoreTotal();


    const deduction =
        getDemoDeductionTotal();


    const finalTotal =
        getDemoFinalTotal();


    const d01 =
        Number(
            s().ded['-0.1'] || 0
        );


    const d03 =
        Number(
            s().ded['-0.3'] || 0
        );


    const d05 =
        Number(
            s().ded['-0.5'] || 0
        );


    document.getElementById(
        'appRoot'
    ).innerHTML = `

    <div class="score-page">


        ${demoHeader(
            row,
            'DEMONSTRATION DEDUCTION'
        )}


        <div class="demo-deduction-layout">


            <div class="demo-deduction-controls">


                <!-- -0.1 -->

                <div class="demo-deduction-card">

                    <button
                        class="demo-deduction-main"

                        onclick="
                            addDemoDeduction(
                                '-0.1'
                            )
                        "
                    >
                        -0.1
                    </button>


                    <div class="demo-ded-count-box">

                        <div class="demo-ded-count-label">
                            COUNT
                        </div>


                        <div class="demo-ded-count">
                            ${d01}
                        </div>


                        <button
                            class="demo-ded-undo"

                            ${d01 <= 0
                                ? 'disabled'
                                : ''
                            }

                            onclick="
                                removeDemoDeduction(
                                    '-0.1'
                                )
                            "
                        >
                            UNDO
                        </button>

                    </div>

                </div>


                <!-- -0.3 -->

                <div class="demo-deduction-card">

                    <button
                        class="
                            demo-deduction-main
                            medium
                        "

                        onclick="
                            addDemoDeduction(
                                '-0.3'
                            )
                        "
                    >
                        -0.3
                    </button>


                    <div class="demo-ded-count-box">

                        <div class="demo-ded-count-label">
                            COUNT
                        </div>


                        <div class="demo-ded-count">
                            ${d03}
                        </div>


                        <button
                            class="demo-ded-undo"

                            ${d03 <= 0
                                ? 'disabled'
                                : ''
                            }

                            onclick="
                                removeDemoDeduction(
                                    '-0.3'
                                )
                            "
                        >
                            UNDO
                        </button>

                    </div>

                </div>


                <!-- -0.5 -->

                <div class="demo-deduction-card">

                    <button
                        class="
                            demo-deduction-main
                            heavy
                        "

                        onclick="
                            addDemoDeduction(
                                '-0.5'
                            )
                        "
                    >
                        -0.5
                    </button>


                    <div class="demo-ded-count-box">

                        <div class="demo-ded-count-label">
                            COUNT
                        </div>


                        <div class="demo-ded-count">
                            ${d05}
                        </div>


                        <button
                            class="demo-ded-undo"

                            ${d05 <= 0
                                ? 'disabled'
                                : ''
                            }

                            onclick="
                                removeDemoDeduction(
                                    '-0.5'
                                )
                            "
                        >
                            UNDO
                        </button>

                    </div>

                </div>


            </div>


            <!-- SUMMARY -->

            <div class="demo-final-summary">


                <div class="demo-summary-label">
                    SCORE
                </div>

                <div class="demo-summary-value">
                    ${scoreTotal.toFixed(1)}
                </div>


                <div class="
                    demo-summary-label
                    demo-summary-ded
                ">
                    DEDUCTION
                </div>

                <div class="
                    demo-summary-value
                    demo-summary-ded
                ">
                    -${deduction.toFixed(1)}
                </div>


                <div class="demo-summary-total">
                    TOTAL
                </div>

                <div class="
                    demo-summary-total
                    value
                ">
                    ${finalTotal.toFixed(1)}
                </div>


            </div>


            <!-- FINAL SEND -->

            <div class="demo-final-send-wrap">

                <button
                    class="demo-final-send"

                    onclick="sendDemo()"
                >
                    SEND
                </button>

            </div>


        </div>

    </div>
    `;


    pollTime();
}


function addDemoDeduction(key){

    s().ded[key] =
        Number(
            s().ded[key] || 0
        ) + 1;


    saveScoreDraft();


    renderDemoDeduction();
}


function removeDemoDeduction(key){

    s().ded[key] =
        Math.max(
            0,
            Number(
                s().ded[key] || 0
            ) - 1
        );


    saveScoreDraft();


    renderDemoDeduction();
}

async function canSend(){let r=await api('/api/time'); return r.control==='stop'}
async function sendRecognized(side){
    if(!(await canSend())) return toast('The match not finish!','warn');

    let body = {
        side,
        screen: STATE.screen,
        ready: STATE.ready,
        scores: s()
    };

    let r = await api('/api/send_recognized',{
        method:'POST',
        body:JSON.stringify(body)
    });

    if(r.ok){
        toast('Sent Success!','ok',()=>{
            clearScoreDraft();
            STATE.screen='wait';
            pageWait();
        });
    }else{
        toast(r.error || 'Send error','warn');
    }
}

async function sendCutoff(){
    if(!(await canSend())) return toast('The match not finish!','warn');

    let r = await api('/api/send_cutoff',{
        method:'POST',
        body:JSON.stringify({
            ready: STATE.ready,
            scores: s()
        })
    });

    if(r.ok){
        toast('Sent Success!','ok',()=>{
            clearScoreDraft();
            STATE.screen='wait';
            pageWait();
        });
    }else{
        toast(r.error || 'Send error','warn');
    }
}

async function sendFsTech(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    /* =====================================
       5 SKILL:
       Technical Proficiency bắt buộc
    ===================================== */

    const missingProficiency =
        FS_TECH_SKILLS.find(skill=>{

            return (
                STATE.fsTech?.[skill]?.proficiency === null ||
                STATE.fsTech?.[skill]?.proficiency === undefined
            );

        });


    if(missingProficiency){

        return toast(
            'Please select Technical Proficiency for ' +
            missingProficiency +
            '!',
            'warn'
        );
    }


    /* =====================================
       BASIC MOVEMENT bắt buộc
    ===================================== */

    if(
        STATE.fsTech?.['Basic Movement']?.score === null ||
        STATE.fsTech?.['Basic Movement']?.score === undefined
    ){

        return toast(
            'Please score Basic Movement!',
            'warn'
        );
    }


    /* =====================================
       TẠO ĐIỂM CUỐI CÙNG TỪNG SKILL
    ===================================== */

    const finalTech = {};


    FS_TECH_SKILLS.forEach(skill=>{

        finalTech[skill] =
            getFsTechSkillTotal(skill);

    });


    finalTech['Basic Movement'] =
        getFsTechSkillTotal(
            'Basic Movement'
        );


    finalTech.Tech =
        Object.values(finalTech)
            .reduce(
                (sum,value)=>sum + Number(value || 0),
                0
            );


    /*
    finalTech lúc này VD:

    {
        "Jumping Side Kick": 6,
        "Multiple Kick": 7,
        "Spining Kick": 5,
        "Consecutive Kick": 8,
        "Acrobatic Kick": 9,
        "Basic Movement": 8,
        "Tech": 43
    }
    */


    STATE.freestyleTech = finalTech;


    saveScoreDraft();


    STATE.screen = 'freestyle_pre';

    STATE.localLockScreen = 'freestyle_pre';


    renderFreestylePre(true);


    toast(
        'Technical saved!',
        'ok'
    );
}

async function sendFsPre(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    if(!STATE.freestyleTech){

        return toast(
            'Technical missing!',
            'warn'
        );
    }


    if(
        Object.values(STATE.fsPre)
            .some(v=>v===0)
    ){

        return toast(
            'Please score all 4 fields!',
            'warn'
        );
    }


    /* ========================================
       CHỈ LƯU PRESENTATION
       CHƯA GỬI SUPABASE
    ======================================== */

    STATE.freestylePre = {
        ...STATE.fsPre
    };


    /* Reset lỗi khi bước vào màn Deduction mới */

    STATE.fsDeduction = {
        minus1:0,
        minus3:0
    };


    saveScoreDraft();


    STATE.screen =
        'freestyle_deduction';


    STATE.localLockScreen =
        'freestyle_deduction';


    renderFreestyleDeduction(false);


    toast(
        'Presentation saved!',
        'ok'
    );
}

async function sendFsFinal(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    if(!STATE.freestyleTech){

        return toast(
            'Technical missing!',
            'warn'
        );
    }


    if(!STATE.freestylePre){

        return toast(
            'Presentation missing!',
            'warn'
        );
    }


    const body = {

        ready: STATE.ready,

        tech: STATE.freestyleTech,

        pre: STATE.freestylePre,

        deduction:{
            minus1:Number(
                STATE.fsDeduction?.minus1 || 0
            ),

            minus3:Number(
                STATE.fsDeduction?.minus3 || 0
            )
        }

    };


    const r = await api(
        '/api/send_freestyle',
        {
            method:'POST',
            body:JSON.stringify(body)
        }
    );


    if(r.ok){

        toast(
            'Sent Success!',
            'ok',
            ()=>{

                clearScoreDraft();


                STATE.fsTech = null;

                STATE.fsPre = null;


                STATE.freestyleTech = null;

                STATE.freestylePre = null;


                STATE.fsDeduction = {
                    minus1:0,
                    minus3:0
                };


                STATE.localLockScreen = '';


                STATE.scores =
                    scoreInit();


                STATE.screen = 'wait';


                pageWait();

            }
        );

    }else{

        toast(
            r.error || 'Send error',
            'warn'
        );
    }
}

async function sendDemoScoring(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    /* ========================================
       CHỈ LƯU SCORE
       CHƯA GỬI SUPABASE
    ======================================== */


    /*
    Reset deduction khi lần đầu
    chuyển từ Scoring sang Deduction.
    */

    s().ded = {
        '-0.1':0,
        '-0.3':0,
        '-0.5':0
    };


    saveScoreDraft();


    STATE.screen =
        'demonstration_deduction';


    STATE.localLockScreen =
        'demonstration_deduction';


    renderDemoDeduction();


    toast(
        'Score saved!',
        'ok'
    );
}

async function sendDemo(){

    if(!(await canSend())){

        return toast(
            'The match not finish!',
            'warn'
        );
    }


    const r =
        await api(
            '/api/send_demo',
            {
                method:'POST',

                body:JSON.stringify({
                    ready:STATE.ready,
                    scores:s()
                })
            }
        );


    if(r.ok){

        toast(
            'Sent Success!',
            'ok',
            ()=>{

                clearScoreDraft();


                STATE.localLockScreen = '';


                STATE.scores =
                    scoreInit();


                STATE.screen =
                    'wait';


                pageWait();

            }
        );

    }else{

        toast(
            r.error || 'Send error',
            'warn'
        );
    }
}
async function pollTime(){
    if(!STATE.match) return;

    try{
        const match = STATE.match?.Match || '';
        let r = await api('/api/time?match=' + encodeURIComponent(match));

        if(!r.ok) return;

        let sec = Number(r.seconds || 0);
        let min = Number(r.minutes || 0);
        let control = String(r.control || '').toLowerCase();

        // Chặn lỗi nhảy 00:00 giả khi trận đang chạy
        if(
            control === 'start' &&
            min === 0 &&
            sec === 0 &&
            STATE.lastGoodTime.match === match &&
            (STATE.lastGoodTime.minutes > 0 || STATE.lastGoodTime.seconds > 0)
        ){
            min = STATE.lastGoodTime.minutes;
            sec = STATE.lastGoodTime.seconds;
            control = STATE.lastGoodTime.control || control;
        }else{
            STATE.lastGoodTime = {match, minutes:min, seconds:sec, control};
        }

        let el = document.getElementById('timer');

        if(sec < 0){
            let o = Math.abs(sec);
            STATE.timerText = '+' + String(Math.floor(o / 60)).padStart(2,'0') + ':' + String(o % 60).padStart(2,'0');
            STATE.timerColor = '#ff2a2a';
        }else{
            STATE.timerText = String(min).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
            STATE.timerColor = control === 'stop' ? '#00ff75' : '#ffef00';
        }

        if(el){
            el.innerText = STATE.timerText;
            el.style.color = STATE.timerColor;
        }

    }catch(e){}
}

function forceReadyFromRow(row, ready, judgeStatus){
    judgeStatus = String(judgeStatus || "").toLowerCase();

    let action = String(row?.["Action"] || "").toLowerCase();
    let poomsaeType = String(row?.["Poomsae Type"] || "").toLowerCase();

    const oneSideTypes = [
        "demonstration",
        "freestyle",
        "freestyle breaking",
        "vertical spinning kicks",
        "jumping multiple kicks"
    ];

    if(
        oneSideTypes.includes(poomsaeType) &&
        action !== "ready"
    ){
        return {
            slot:"",
            poomsae:""
        };
    }

    // Judge đã chấm xong bài 1 thì không ép hiện lại bài 1 nữa
    if(judgeStatus === "finish 1"){
        return {
            slot: "",
            poomsae: "Waiting Poomsae 2 Ready..."
        };
    }

    let current = String(row?.["Current Poomsae"] || "").toLowerCase();
    let p1Action = String(row?.["Poomsae 1 action"] || "").toLowerCase();
    let p2Action = String(row?.["Poomsae 2 action"] || "").toLowerCase();

    if(current.includes("2") || p2Action === "ready"){
        return {
            slot: "2",
            poomsae: row?.["Poomsae 2"] || ""
        };
    }

    if(current.includes("1") || p1Action === "ready"){
        return {
            slot: "1",
            poomsae: row?.["Poomsae 1"] || ""
        };
    }

    return ready || {slot:"", poomsae:""};
}

let pollBusy = false;
async function poll(){
    if(pollBusy) return;
    pollBusy = true;
    try{
        let r = await api('/api/active_match');

        if(!r.ok){
            STATE.noActiveCount++;

            // Chống văng màn hình khi Supabase trả rỗng thoáng qua
            if(STATE.noActiveCount < 3 && STATE.match){
                // Mất một phản hồi tạm thời: giữ nguyên DOM/cờ đang hiển thị.
                pollTime();
                return;
            }

            if(STATE.screen !== 'wait'){
                STATE.screen = 'wait';
                pageWait();
            }
            STATE.match = null;
            STATE.ready = {slot:'', poomsae:''};
            STATE.judge_status = '';
            STATE.localLockScreen='';
            STATE.readyRenderKey = '';
            return;
        }

        STATE.noActiveCount = 0;

        // Ép lấy bài đang chấm trực tiếp từ Current Poomsae / Poomsae action
        r.ready = forceReadyFromRow(r.row, r.ready, r.judge_status);
        let control = String(r.control || r.row?.["Control time"] || "").trim().toLowerCase();
        if(
            STATE.localLockScreen === 'freestyle_pre' &&
            r.screen === 'freestyle_tech'
        ){
            r.screen = 'freestyle_pre';
        }


        if(
            STATE.localLockScreen === 'freestyle_deduction' &&
            r.screen === 'freestyle_tech'
        ){
            r.screen = 'freestyle_deduction';
        }


        /* =========================================
           DEMONSTRATION DEDUCTION LOCK
        ========================================= */

        if(
            STATE.localLockScreen === 'demonstration_deduction' &&
            r.screen === 'demonstration'
        ){
            r.screen = 'demonstration_deduction';
        }



        let oldMatch = STATE.match?.Match || '';
        let oldSlot = STATE.ready?.slot || '';
        let oldPoomsae = STATE.ready?.poomsae || '';
        let oldCurrent = STATE.match?.["Current Poomsae"] || '';
        let oldScreen = STATE.screen || '';

        let newMatch = r.row?.Match || '';
        let newSlot = r.ready?.slot || '';
        let newPoomsae = r.ready?.poomsae || '';
        let newCurrent = r.row?.["Current Poomsae"] || '';

        let needRender =
            oldScreen !== r.screen ||
            oldMatch !== newMatch ||
            oldSlot !== newSlot ||
            oldPoomsae !== newPoomsae ||
            oldCurrent !== newCurrent;

        if(oldMatch && newMatch && oldMatch !== newMatch){

            STATE.fsTech = null;

            STATE.fsPre = null;


            STATE.freestyleTech = null;

            STATE.freestylePre = null;


            STATE.fsDeduction = {
                minus1:0,
                minus3:0
            };


            STATE.localLockScreen = '';


            STATE.scores =
                scoreInit();
        }

        STATE.match = r.row;
        STATE.ready = r.ready;
        STATE.judge_status = r.judge_status;

        if(!STATE.ready.slot){
            const finishedFirst = (r.judge_status || '').toLowerCase() === 'finish 1';
            // Tất cả trường ảnh hưởng tới màn hình READY. Chỉ cập nhật khi thay đổi.
            const readyKey = JSON.stringify([
                'no-slot', finishedFirst, newMatch,
                r.row?.Phase, r.row?.Division, r.row?.Method,
                r.row?.['Poomsae Type'],
                r.row?.['NOC Blue'], r.row?.['NOC Red'],
                r.row?.['Athlete Blue'], r.row?.['Athlete Red'],
                r.judge_status, r.row?.[STATE.judge_id],
                newPoomsae, newCurrent
            ]);
            if(STATE.screen !== 'ready' || STATE.readyRenderKey !== readyKey){
                STATE.screen = 'ready';
                STATE.readyRenderKey = readyKey;
                if(finishedFirst){
                    pageWait(
                        'WAITING POOMSAE 2',
                        'Waiting for controller to ready Poomsae 2...'
                    );
                }else{
                    pageReady(r.row, 'MATCH START');
                }
            }
            return;
        }

        if(control === 'stop'){

            if(
                STATE.screen === 'recognized_sbs' ||
                STATE.screen === 'recognized_obo' ||
                STATE.screen === 'recognized_cutoff' ||

                STATE.screen === 'demonstration' ||
                STATE.screen === 'demonstration_deduction' ||

                STATE.screen === 'freestyle_tech' ||
                STATE.screen === 'freestyle_pre' ||
                STATE.screen === 'freestyle_deduction' ||

                STATE.screen === 'freestyle_breaking' ||
                STATE.screen === 'vertical_spinning_kicks' ||
                STATE.screen === 'jumping_multiple_kicks'
            ){

                if(needRender){

                    STATE.screen = r.screen;

                    renderScoring(true);

                }else{

                    pollTime();
                }

                return;
            }
        }

        if(control !== 'start'){
            const waitText = `POOMSAE READY\n${STATE.ready.poomsae}\nWaiting Start Time...`;
            const readyKey = JSON.stringify([
                'with-slot', newMatch, newSlot, newPoomsae, newCurrent,
                r.row?.Phase, r.row?.Division, r.row?.Method,
                r.row?.['Poomsae Type'],
                r.row?.['NOC Blue'], r.row?.['NOC Red'],
                r.row?.['Athlete Blue'], r.row?.['Athlete Red'],
                r.judge_status, r.row?.[STATE.judge_id]
            ]);
            if(STATE.screen !== 'ready' || STATE.readyRenderKey !== readyKey){
                STATE.screen = 'ready';
                STATE.readyRenderKey = readyKey;
                pageReady(r.row, waitText);
            }else{
                pollTime();
            }

            return;
        }

        if(needRender){
            STATE.screen = r.screen;
            renderScoring(true);
        }else{
            pollTime();
        }

    }catch(e){
        console.log('Poll error:', e);
    }finally{ pollBusy = false; }
}
buildSliderPopupScale();
pageWait();
setInterval(poll,800);
setInterval(pollTime,350);
poll();
</script>
{% endif %}
</body></html>
'''

# =========================
# ROUTES
# =========================
# Use the Judge controller icon, independent of Tournament/Referee branding.
# Depending on the installation it is stored in icon/ or icons/.
def _judge_controller_favicon_path():
    for candidate in (
        ROOT / 'icon' / 'judge_icon.png',
        ROOT / 'icons' / 'judge_icon.png',
    ):
        if candidate.is_file():
            return candidate
    return None


@app.route('/favicon.png')
@app.route('/favicon.ico')
@app.route('/apple-touch-icon.png')
def judge_controller_favicon():
    image = _judge_controller_favicon_path()
    if image is None:
        abort(404)
    response = send_from_directory(str(image.parent), image.name, mimetype='image/png', max_age=3600)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/judge-login-background')
def judge_login_background():
    image = ROOT / 'icons' / 'TKD_web.png'
    if not image.is_file():
        abort(404)
    return send_from_directory(str(image.parent), image.name, max_age=3600)


def render_login(*, error=False, chosen_event=''):
    try:
        events = event_options()
        error_text = ''
    except Exception as exc:
        app.logger.error('Cannot load Judge events: %s', type(exc).__name__)
        events = []
        error_text = 'Could not load Event list. Check Supabase connection.'
    portal_event = str(request.args.get('event_code') or '').strip()
    if portal_event and not chosen_event:
        chosen_event = portal_event
    if portal_event:
        events = [entry for entry in events if entry.get('code', '').casefold() == portal_event.casefold()]
    return render_template_string(PAGE, judge_id=None, court_id=None, range=range,
                                  events=events, chosen_event=chosen_event,
                                  error=error, event_error=error_text)


@app.route('/', methods=['GET'])
def index():
    selected_event = str(request.args.get('event_code') or '').strip()
    claims = _judge_context()
    if selected_event and claims.get('event_code') and str(claims['event_code']).casefold() != selected_event.casefold():
        # A new event requires an independent Judge login: never reuse a previous event's authority.
        session.clear()
        return render_login(chosen_event=selected_event)
    if not claims.get('judge_id') or not claims.get('event_code'):
        return render_login()
    return render_template_string(PAGE, judge_id=claims.get('judge_id'),
                                  court_id=claims.get('court_id'),
                                  chosen_event=claims.get('event_code'),
                                  events=[], range=range, error=False,
                                  event_error='')


@app.route('/login', methods=['POST'])
def login():
    event_code = str(request.form.get('event_code') or '').strip()
    try:
        valid_events = {r['code'] for r in event_options()}
        if event_code not in valid_events:
            return render_login(error=True, chosen_event=event_code), 400
        if request.form.get('password', '') != judge_login_password(event_code):
            return render_login(error=True, chosen_event=event_code), 401
    except Exception as exc:
        app.logger.error('Judge login validation failed: %s', type(exc).__name__)
        return render_login(error=True, chosen_event=event_code), 503

    judge_id = str(request.form.get('judge_id') or '').strip()
    court_id = str(request.form.get('court_id') or '').strip()
    if judge_id not in {f'Judge {i}' for i in range(1, 8)} or court_id not in {f'Court {i}' for i in range(0, 7)}:
        return render_login(error=True, chosen_event=event_code), 400

    session.clear()
    session['judge_id'] = judge_id
    session['court_id'] = court_id
    session['event_code'] = event_code
    session['schedule_table'] = 'Poomsae Matchs'
    signed = _JUDGE_TOKEN.dumps({'judge_id': judge_id, 'court_id': court_id, 'event_code': event_code})
    return redirect(url_for('index', judge_session=signed))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


def require_login():
    ctx = _judge_context()
    judge_id = ctx.get("judge_id")
    schedule_table = ctx.get("schedule_table", "Poomsae Matchs")
    if not judge_id or not schedule_table or not ctx.get('event_code'):
        return None, None
    return judge_id, schedule_table


@app.route("/api/active_match")
def api_active_match():
    try:
        judge_id, schedule_table = require_login()

        if not judge_id:
            return jsonify(ok=False, error="Not logged in")

        row = get_active_match(schedule_table, judge_id)

        if not row:
            return jsonify(ok=False, error="No active match")

        judge_status = str(row.get(judge_id) or "").strip().lower()
        ready = resolve_ready(row, judge_status)
        control = str(row.get("Control time") or "").strip().lower()

        return jsonify(
            ok=True,
            row=row,
            ready=ready,
            control=control,
            screen=detect_screen(row),
            judge_status=judge_status
        )

    except Exception as e:
        return jsonify(ok=False, error=f"api_active_match error: {str(e)}")


@app.route("/api/time")
def api_time():
    try:
        judge_id, schedule_table = require_login()

        if not judge_id:
            return jsonify(ok=False, error="Not logged in")

        match_no = request.args.get("match")

        if not match_no:
            row = get_active_match(schedule_table, judge_id)
            match_no = row.get("Match") if row else ""

        if not match_no:
            return jsonify(ok=True, minutes=0, seconds=0, control="")

        minutes, seconds, control = get_match_time(schedule_table, match_no)

        return jsonify(ok=True, minutes=minutes, seconds=seconds, control=control)

    except Exception as e:
        return jsonify(ok=False, minutes=0, seconds=0, control="", error=f"api_time error: {str(e)}")


def active_context():
    judge_id, schedule_table = require_login()
    if not judge_id:
        raise RuntimeError("Not logged in")
    row = get_active_match(schedule_table, judge_id)
    if not row:
        raise RuntimeError("No active match")
    return judge_id, schedule_table, row


@app.route("/api/send_recognized", methods=["POST"])
def api_send_recognized():
    try:
        judge_id, schedule_table, info = active_context()
        payload = request.get_json(force=True)
        sc = payload.get("scores", {})
        ready = payload.get("ready", {})
        side = payload.get("side", "both")
        match = info.get("Match", "")
        verified = resolve_ready(info, str(info.get(judge_id) or ''))
        if not verified.get('slot'):
            return jsonify(ok=False, error='Judge already submitted; waiting for the next Poomsae')
        selected_poomsae = verified['poomsae']
        slot = verified['slot']
        _, _, control = get_match_time(schedule_table, match)
        if control != "stop":
            return jsonify(ok=False, error="The match not finish!")

        def send_side(color: str):
            is_blue = color == "blue"
            pre = round(float(sc.get("bs" if is_blue else "rs", 2)) + float(sc.get("br" if is_blue else "rr", 2)) + float(sc.get("bk" if is_blue else "rk", 2)), 1)
            send_judge_score({
                "Judge": judge_id,
                "Match": match,
                "NOC": info.get("NOC Blue" if is_blue else "NOC Red", ""),
                "Name": info.get("Athlete Blue" if is_blue else "Athlete Red", ""),
                "Tech -1": str(sc.get("b01" if is_blue else "r01", 0)),
                "Tech -3": str(sc.get("b03" if is_blue else "r03", 0)),
                "Speed/power": str(sc.get("bs" if is_blue else "rs", 2)),
                "Rhythm": str(sc.get("br" if is_blue else "rr", 2)),
                "KI": str(sc.get("bk" if is_blue else "rk", 2)),
                "Tech": str(round(float(sc.get("bt" if is_blue else "rt", 4)), 1)),
                "Pre": str(pre),
                "Poomsae": selected_poomsae,
            })

        if side == "blue":
            send_side("blue")
            mark_judge_finish(
                schedule_table,
                match,
                judge_id,
                "Red 1 Ready" if slot == "1" else "Red 2 Ready"
            )
        elif side == "red":
            send_side("red")
            mark_judge_finish(
                schedule_table,
                match,
                judge_id,
                "Finish 1" if slot == "1" else "Finish"
            )
        else:
            send_side("blue")
            send_side("red")
            mark_judge_finish(schedule_table, match, judge_id, "Finish 1" if slot == "1" else "Finish")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/send_cutoff", methods=["POST"])
def api_send_cutoff():
    try:
        judge_id, schedule_table, info = active_context()
        payload = request.get_json(force=True)
        sc = payload.get("scores", {})
        ready = payload.get("ready", {})
        match = info.get("Match", "")
        _, _, control = get_match_time(schedule_table, match)
        if control != "stop":
            return jsonify(ok=False, error="The match not finish!")
        pre = round(float(sc.get("cs", 2)) + float(sc.get("cr", 2)) + float(sc.get("ck", 2)), 1)
        send_judge_score({
            "Judge": judge_id,
            "Match": match,
            "Poomsae": ready.get("poomsae", ""),
            "NOC": info.get("NOC Blue", ""),
            "Name": info.get("Athlete Blue", ""),
            "Tech -1": str(sc.get("c01", 0)),
            "Tech -3": str(sc.get("c03", 0)),
            "Speed/power": str(sc.get("cs", 2)),
            "Rhythm": str(sc.get("cr", 2)),
            "KI": str(sc.get("ck", 2)),
            "Tech": str(round(float(sc.get("ct", 4)), 1)),
            "Pre": str(pre),
        })
        mark_judge_finish(schedule_table, match, judge_id, "Finish 1" if ready.get("slot") == "1" else "Finish")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/send_freestyle", methods=["POST"])
def api_send_freestyle():
    try:
        judge_id, schedule_table, info = active_context()

        payload = request.get_json(force=True)

        tech = payload.get("tech", {}) or {}
        pre = payload.get("pre", {}) or {}
        deduction = payload.get("deduction", {}) or {}
        ready = payload.get("ready", {}) or {}

        match = info.get("Match", "")

        _, _, control = get_match_time(
            schedule_table,
            match
        )

        if control != "stop":
            return jsonify(
                ok=False,
                error="The match not finish!"
            )


        # ====================================
        # TECHNICAL TOTAL
        # ====================================

        technical_total = round(
            float(
                tech.get("Tech", 0)
                or 0
            ),
            1
        )


        # ====================================
        # PRESENTATION TOTAL
        # ====================================

        presentation_total = round(
            sum(
                float(v or 0)
                for v in pre.values()
            ),
            1
        )


        # ====================================
        # DEDUCTION COUNTS
        #
        # Tech -1 = số LẦN phạm lỗi -1
        # Tech -3 = số LẦN phạm lỗi -3
        # ====================================

        minus1_count = int(
            deduction.get("minus1", 0)
            or 0
        )

        minus3_count = int(
            deduction.get("minus3", 0)
            or 0
        )


        deduction_points = (
            minus1_count * 1
            +
            minus3_count * 3
        )


        # ====================================
        # FINAL TOTAL
        #
        # Chỉ tính để kiểm soát logic.
        # Chưa tự ý lưu vào cột mới.
        # ====================================

        final_total = round(
            technical_total
            +
            presentation_total
            -
            deduction_points,
            1
        )


        data = {

            "Judge": judge_id,

            "Match": match,

            "Poomsae": "1 - Free",

            "NOC": info.get(
                "NOC Blue",
                ""
            ),

            "Name": info.get(
                "Athlete Blue",
                ""
            ),


            # ================================
            # TECHNICAL
            # ================================

            "Jumping Side Kick": str(
                tech.get(
                    "Jumping Side Kick",
                    0
                )
            ),

            "Multiple Kick": str(
                tech.get(
                    "Multiple Kick",
                    0
                )
            ),

            "Spining Kick": str(
                tech.get(
                    "Spining Kick",
                    0
                )
            ),

            "Consecutive Kick": str(
                tech.get(
                    "Consecutive Kick",
                    0
                )
            ),

            "Acrobatic Kick": str(
                tech.get(
                    "Acrobatic Kick",
                    0
                )
            ),

            "Basic Movement": str(
                tech.get(
                    "Basic Movement",
                    0
                )
            ),


            # ================================
            # DEDUCTION COUNTS
            # ================================

            "Tech -1": str(
                minus1_count
            ),

            "Tech -3": str(
                minus3_count
            ),


            # ================================
            # TOTAL TECHNICAL
            # ================================

            "Tech": str(
                technical_total
            ),


            # ================================
            # PRESENTATION
            # ================================

            "Creativeness": str(
                pre.get(
                    "Creativeness",
                    0
                )
            ),

            "Harmony": str(
                pre.get(
                    "Harmony",
                    0
                )
            ),

            "Expression Of Energy": str(
                pre.get(
                    "Expression Of Energy",
                    0
                )
            ),

            "Music & Choreography": str(
                pre.get(
                    "Music & Choreography",
                    0
                )
            ),

            "Pre": str(
                presentation_total
            ),

        }


        print(
            "FREESTYLE FINAL:",
            {
                "technical": technical_total,
                "presentation": presentation_total,
                "minus1_count": minus1_count,
                "minus3_count": minus3_count,
                "deduction_points": deduction_points,
                "final_total": final_total,
            }
        )


        send_judge_score(data)


        mark_judge_finish(
            schedule_table,
            match,
            judge_id,
            "Finish"
        )


        return jsonify(
            ok=True,
            technical=technical_total,
            presentation=presentation_total,
            deduction=deduction_points,
            total=final_total
        )


    except Exception as e:

        return jsonify(
            ok=False,
            error=str(e)
        )


@app.route("/api/send_demo", methods=["POST"])
def api_send_demo():
    try:
        judge_id, schedule_table, info = active_context()
        payload = request.get_json(force=True)
        sc = payload.get("scores", {})
        demo = sc.get("demo", {})
        ded = sc.get("ded", {})
        ready = payload.get("ready", {})
        match = info.get("Match", "")
        _, _, control = get_match_time(schedule_table, match)
        if control != "stop":
            return jsonify(ok=False, error="The match not finish!")
        deduction = round(float(ded.get("-0.1", 0)) * 0.1 + float(ded.get("-0.3", 0)) * 0.3 + float(ded.get("-0.5", 0)) * 0.5, 1)
        final_total = round(max(0, sum(float(v) for v in demo.values()) - deduction), 1)
        send_judge_score({
            "Judge": judge_id,
            "Match": match,
            "Poomsae": "1 - Demon",
            "NOC": info.get("NOC Blue", ""),
            "Name": info.get("Athlete Blue", ""),
            "Yeonmu": str(demo.get("Yeonmu", 2)),
            "Self-Defense": str(demo.get("Self-Defense", 2)),
            "Breaking": str(demo.get("Breaking", 5)),
            "Composition": str(demo.get("Composition", 1)),
            "Deduction": str(deduction),
            "Tech": str(final_total),
            "Pre": "0",
        })
        mark_judge_finish(schedule_table, match, judge_id, "Finish")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/api/send_special", methods=["POST"])
def api_send_special():

    try:
        judge_id, schedule_table, info = active_context()

        payload = request.get_json(force=True) or {}

        ready = payload.get("ready", {}) or {}

        poomsae_type = str(
            payload.get("poomsae_type")
            or info.get("Poomsae Type")
            or ""
        ).strip()

        scores = payload.get("scores", {}) or {}

        deduction_data = (
            payload.get("deduction", {})
            or {}
        )

        summary = (
            payload.get("summary", {})
            or {}
        )

        match = str(
            info.get("Match", "")
            or ""
        ).strip()


        # =========================================
        # TIMER PHẢI STOP MỚI CHO GỬI
        # =========================================

        _, _, control = get_match_time(
            schedule_table,
            match
        )

        if control != "stop":

            return jsonify(
                ok=False,
                error="The match not finish!"
            )


        # =========================================
        # DEDUCTION COUNT
        # =========================================

        minus3_count = int(
            deduction_data.get("minus3", 0)
            or 0
        )

        minus5_count = int(
            deduction_data.get("minus5", 0)
            or 0
        )


        # -3 = 0.3
        # -5 = 0.5

        deduction_score = round(
            (
                minus3_count * 0.3
                +
                minus5_count * 0.5
            ),
            3
        )


        # =========================================
        # SUMMARY
        # =========================================

        accuracy = round(
            float(
                summary.get("accuracy", 0)
                or 0
            ),
            3
        )


        presentation = round(
            float(
                summary.get("presentation", 0)
                or 0
            ),
            3
        )


        level_skill = summary.get(
            "level_skill",
            None
        )

        if level_skill is not None:

            level_skill = round(
                float(level_skill or 0),
                3
            )


        total = round(
            float(
                summary.get("total", 0)
                or 0
            ),
            3
        )


        # =========================================
        # POOMSAE
        # =========================================

        poomsae = str(
            ready.get("poomsae")
            or info.get("Poomsae 1")
            or poomsae_type
            or ""
        ).strip()


        # =========================================
        # DATA CHO BREAKING SCORE
        # =========================================

        data = {

            "Judge":
                judge_id,

            "Match":
                match,

            "Poomsae Type":
                poomsae_type,

            "Poomsae":
                poomsae,

            "NOC":
                info.get(
                    "NOC Blue",
                    ""
                ),

            "Name":
                info.get(
                    "Athlete Blue",
                    ""
                ),

            # Toàn bộ raw score từng lượt
            "Scores":
                scores,

            # Lưu số LẦN bấm
            "Deduction -3":
                minus3_count,

            "Deduction -5":
                minus5_count,

            # Điểm tổng hợp
            "Accuracy":
                accuracy,

            "Presentation":
                presentation,

            "Level Skill":
                level_skill,

            "Deduction":
                deduction_score,

            "Total":
                total,
        }


        # =========================================
        # TRÁNH GỬI TRÙNG CÙNG JUDGE + MATCH
        # =========================================

        old = (
            supabase
            .table("Breaking Score")
            .select("id")
            .eq("Match", match)
            .eq("Judge", judge_id)
            .limit(1)
            .execute()
        )


        if old.data:

            row_id = old.data[0]["id"]

            (
                supabase
                .table("Breaking Score")
                .update(data)
                .eq("id", row_id)
                .execute()
            )

        else:

            (
                supabase
                .table("Breaking Score")
                .insert(data)
                .execute()
            )


        # =========================================
        # JUDGE FINISH
        # =========================================

        mark_judge_finish(
            schedule_table,
            match,
            judge_id,
            "Finish"
        )


        return jsonify(
            ok=True,
            accuracy=accuracy,
            presentation=presentation,
            level_skill=level_skill,
            deduction=deduction_score,
            total=total
        )


    except Exception as e:

        print(
            "SEND SPECIAL ERROR:",
            e
        )

        return jsonify(
            ok=False,
            error=str(e)
        )


@app.route('/flags/<path:filename>')
def serve_flag(filename):
    return send_from_directory(str(ROOT / 'flags'), filename)

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Poomsae Scoring System",
        "short_name": "Poomsae",
        "start_url": request.script_root + "/",
        "display": "fullscreen",
        "background_color": "#02040a",
        "theme_color": "#02040a",
        "orientation": "landscape",
        "icons": [
            {"src": request.script_root + "/favicon.png?v=judge2", "sizes": "any", "type": "image/png", "purpose": "any"}
        ]
    })

import sys
import threading
import webbrowser
from PIL import Image

tray_icon = None
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = str(ROOT)
    return os.path.join(base_path, relative_path)

def run_flask():
    port = 5000  # Must match Poomsae-Score.py readiness probe.

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )

def open_browser(icon=None, item=None):
    webbrowser.open("http://127.0.0.1:5000")

def quit_app(icon, item):
    icon.stop()
    os._exit(0)

def setup_tray():
    from pystray import Icon, MenuItem, Menu
    global tray_icon

    image = Image.open(resource_path("icons/main.png"))

    menu = Menu(
        MenuItem("Open Poomsae Scoring", open_browser),
        MenuItem("Exit", quit_app)
    )

    tray_icon = Icon(
        "Poomsae Scoring",
        image,
        "Poomsae Scoring",
        menu
    )

    tray_icon.run()

if __name__ == "__main__":

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    webbrowser.open("http://127.0.0.1:5000")

    setup_tray()
