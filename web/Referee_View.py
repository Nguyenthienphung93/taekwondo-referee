
# Access shared modules while retaining project-root data paths.
from pathlib import Path as _ProjectPath
import sys as _project_sys
_PROJECT_ROOT = _ProjectPath(__file__).resolve().parent.parent
_project_sys.path.insert(0, str(_PROJECT_ROOT / "shared modules"))

from flask import Flask, jsonify, render_template_string, request, send_from_directory, session, g, abort, redirect, url_for
from supabase import create_client, Client
import json
import os
import re
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps
import threading
import time
from secrets import token_urlsafe
from dotenv import load_dotenv

# ================== CONFIG ==================
# Load .env beside this script, NOT the process working directory.
load_dotenv(os.path.join(str(_PROJECT_ROOT), '.env'))


def _env_first(*names):
    """Use the first configured nonempty variable; never expose key values."""
    return next((os.environ[n].strip() for n in names if os.environ.get(n, '').strip()), '')


def _new_client(label, url, key):
    """Keep Poomsae and Kyeorugi clients independent; give safe diagnostics."""
    from urllib.parse import urlparse
    if not url or not key:
        print(f'[{label}] Missing Supabase URL or key in .env')
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.supabase\.co', host)
            or any(word in host for word in ('your-', 'example', 'placeholder'))):
        print(f'[{label}] Invalid Supabase URL hostname: {host or "(empty)"}. '
              'Copy Project URL from Supabase Dashboard > Project Settings > API.')
        return None
    try:
        return create_client(url, key)
    except Exception as exc:
        print(f'[{label}] Cannot initialize client for {host}: {type(exc).__name__}')
        return None


# Prefer dedicated Poomsae credentials. Existing Kyeorugi desktop variables are
# supported without duplicating sensitive values in .env.
_poomsae_url = _env_first('POOMSAE_SUPABASE_URL', 'SUPABASE_URL')
_poomsae_key = _env_first('POOMSAE_SUPABASE_KEY', 'SUPABASE_KEY', 'SUPABASE_PUBLIC_KEY')
_kyeorugi_url = _env_first('KYEORUGI_SUPABASE_URL', 'SUPABASE_URL')
_kyeorugi_key = _env_first('KYEORUGI_SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_SECRET_KEY')
supabase = _new_client('POOMSAE', _poomsae_url, _poomsae_key)
kyeorugi_db = _new_client('KYEORUGI', _kyeorugi_url, _kyeorugi_key)
POOMSAE_MATCH_TABLE = "Poomsae Matchs"
RESULT_TABLE = "Result"  # đổi nếu bảng Supabase của Ken tên khác, ví dụ: match_result/results
# Bảng danh sách VĐV/đoàn trong Supabase
# Sam để fallback vì có nơi đặt là "Information list", có nơi gõ thiếu r là "Infomation list"
SIGMA_EVENTS_TABLE = "sigma_events"
ACTIVE_EVENT_CODE = os.environ.get("ACTIVE_EVENT_CODE", "").strip()
SIGMA_DIVISIONS_TABLE = "sigma_divisions"

INFORMATION_TABLE_CANDIDATES = [
    "Information Lists",
    "Information lists",
    "Information_Lists",
    "Information_lists",
]

JSON_FILE = os.path.join(
    str(_PROJECT_ROOT),
    "sigma",
    "Open Taekwondo Championships 2026.json"
)

LOGO_FILE = "icon/NTP.png"            # đặt file vào thư mục static/NTP.png
# ============================================

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or token_urlsafe(40)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
)


def make_event_code(name):
    text = str(name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "active_event"

MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def parse_event_date(value):
    text = str(value or "").strip()
    if not text:
        return None

    formats = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d/%m/%y",
        "%d-%m-%y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass

    return None


def format_event_date_range(date_from, date_to):
    start = parse_event_date(date_from)
    end = parse_event_date(date_to)

    # Nếu không parse được thì fallback dữ liệu gốc
    if not start and not end:
        return str(date_from or "").strip()

    if start and not end:
        return f"{start.day} {MONTH_NAMES[start.month]} {start.year}"

    if end and not start:
        return f"{end.day} {MONTH_NAMES[end.month]} {end.year}"

    # Cùng ngày
    if start.date() == end.date():
        return f"{start.day} {MONTH_NAMES[start.month]} {start.year}"

    # Cùng tháng, cùng năm: 22–24 May 2026
    if start.month == end.month and start.year == end.year:
        return f"{start.day}–{end.day} {MONTH_NAMES[start.month]} {start.year}"

    # Khác tháng, cùng năm: 22 April – 24 May 2026
    if start.year == end.year:
        return (
            f"{start.day} {MONTH_NAMES[start.month]} – "
            f"{end.day} {MONTH_NAMES[end.month]} {end.year}"
        )

    # Khác năm: 22 December 2026 – 5 January 2027
    return (
        f"{start.day} {MONTH_NAMES[start.month]} {start.year} – "
        f"{end.day} {MONTH_NAMES[end.month]} {end.year}"
    )

def read_local_json(path, default=None):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Read local JSON error {path}:", e)

    return default if default is not None else {}


def _request_event_code():
    """Trust only the event slug resolved on the server, never a request body/URL query."""
    code = getattr(g, 'event_code', None)
    if not code:
        raise ValueError('Missing verified Event Code; use /{event_slug}/...')
    return str(code)


def _scoped_query(db, table, fields='*'):
    return db.table(table).select(fields).eq('event_code', _request_event_code())


def load_active_sigma_event():
    selected = getattr(g, 'event_code', None) if request else None
    if selected:
        try:
            result = (supabase.table(SIGMA_EVENTS_TABLE).select('*')
                      .eq('event_code', selected).limit(1).execute())
            return (result.data or [None])[0]
        except Exception as exc:
            app.logger.warning('Event lookup: %s', type(exc).__name__)
            return None
    if supabase is None:
        return None
    try:
        query = supabase.table(SIGMA_EVENTS_TABLE).select("*")

        if ACTIVE_EVENT_CODE:
            res = query.eq("event_code", ACTIVE_EVENT_CODE).limit(1).execute()
        else:
            res = (
                query
                .eq("is_active", True)
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )

        rows = res.data or []
        if rows:
            return rows[0]

    except Exception as e:
        print("Load active sigma_events error:", e)

    return None

def load_sigma_divisions_from_supabase(event_code):
    if supabase is None:
        return {}
    try:
        if not event_code:
            return {}

        res = (
            supabase
            .table(SIGMA_DIVISIONS_TABLE)
            .select("division_key, athletes")
            .eq("event_code", event_code)
            .execute()
        )

        rows = res.data or []
        divisions = {}

        for r in rows:
            key = str(r.get("division_key") or "").strip()
            athletes = r.get("athletes") or []

            if key:
                divisions[key] = athletes

        return divisions

    except Exception as e:
        print("Load sigma_divisions error:", e)
        return {}

from event_config import load_event

def load_setup():
    # Event-scoped configuration from sigma_events.setup, with legacy fallback ONLY
    # for the original event. This fallback must not leak across events.
    row = load_active_sigma_event()
    if row:
        setup = row.get('setup') if isinstance(row.get('setup'), dict) else {}
        legacy = load_event() if row.get('event_code') == get_legacy_event_code() else {}
        result = dict(legacy)
        result.update({k:v for k,v in setup.items() if v not in (None, '')})
        result['event'] = (setup.get('event') or setup.get('event_name')
                           or row.get('event_name') or result.get('event') or '')
        return result
    return load_event()


def load_event_json():
    # Event metadata from Setup/event.json; draw/athletes stay in existing sources.
    metadata = load_setup()
    row = load_active_sigma_event()
    if row:
        event_code = str(row.get('event_code') or '')
        divisions = load_sigma_divisions_from_supabase(event_code)
        if divisions:
            return {'event_name': metadata.get('event', ''), 'divisions': divisions}
        if event_code != get_legacy_event_code():
            return {'event_name': metadata.get('event', ''), 'divisions': {}}
    data = read_local_json(JSON_FILE, {})
    return {'event_name': metadata.get('event',''), 'divisions': data.get('divisions', {})}


# Favicon: reuse the same original NTP logo used in the portal header.
# Browser requests to /favicon.ico are redirected to the existing logo endpoint.
@app.get('/favicon.ico')
def ntp_favicon():
    return redirect('/referee-logo?v=20261118', code=302)


@app.route("/icon/<path:filename>")
def icon_files(filename):
    return send_from_directory(
        os.path.join(str(_PROJECT_ROOT), "icon"),
        filename
    )

TYPE_ALIASES = {
    "recognized": "Recognized",
    "recogmized": "Recognized",
    "recog": "Recognized",
    "freestyle": "Freestyle",
    "free": "Freestyle",
    "demonstration": "Demonstration",
    "demonstartion": "Demonstration",
    "demon": "Demonstration",
    "demo": "Demonstration",
    "mixer": "Mixer",
    "mixed": "Mixer",
}

DIVISION_WORDS = ["Individual", "Pair", "Team"]

def normalize_type(raw: str) -> str:
    s = str(raw or "").strip()
    low = s.lower()
    for k, v in TYPE_ALIASES.items():
        if k in low:
            return v
    return s.title() if s else "Other"

def parse_division_key(key: str):
    """Parse: Recognized - Individual Male Cadet -> type/division/age/gender"""
    key = str(key or "").strip()
    if " - " in key:
        raw_type, rest = key.split(" - ", 1)
    else:
        raw_type, rest = "Other", key

    typ = normalize_type(raw_type)
    division = "Other"
    for d in DIVISION_WORDS:
        if re.search(rf"\b{d}\b", rest, re.I):
            division = d
            break

    gender = ""
    if re.search(r"\bMale\b", rest, re.I):
        gender = "Male"
    elif re.search(r"\bFemale\b", rest, re.I):
        gender = "Female"

    age = rest
    age = re.sub(r"\b(Individual|Pair|Team|Male|Female)\b", "", age, flags=re.I)
    age = re.sub(r"\s+", " ", age).strip(" -")
    return {"key": key, "type": typ, "division": division, "age": age or "Open", "gender": gender}


def build_menu():
    data = load_event_json()

    menu = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(list)
            )
        )
    )
    flat = []

    for key, athletes in data.get("divisions", {}).items():
        meta = parse_division_key(key)

        athlete_rows = athletes or []

        # Số dòng thật trong file Sigma
        meta["count"] = len(athlete_rows)

        # Số VĐV/đội thi thật dựa theo No.
        # Individual: mỗi No. là 1 VĐV
        # Pair/Team: nhiều dòng chung 1 No. nhưng chỉ tính là 1 đội
        competitor_nos = set()

        for a in athlete_rows:
            no = str(a.get("No.", "") or "").strip()
            if no:
                competitor_nos.add(no)

        meta["competitor_count"] = len(competitor_nos) if competitor_nos else len(athlete_rows)

        flat.append(meta)

        gender = meta.get("gender") or "Mixed"

        menu[
            meta["type"]
        ][
            meta["division"]
        ][
            gender
        ][
            meta["age"]
        ].append(meta)

    # Only Kyeorugi Matchs controls which Kyeorugi divisions exist.
    try:
        for item in _kyeorugi_divisions():
            meta = {**item, 'type': 'Kyeorugi',
                    'division': item['group'],
                    'competitor_count': item['count']}
            flat.append(meta)
            # Kyeorugi uses its own sidebar tree from `flat`, not Poomsae hierarchy.
    except Exception as exc:
        print('[KYEORUGI MENU]', exc)
    return data.get("event_name", "Event"), convert_dict(menu), flat

def safe_float(v):
    try:
        if v is None or v == "": return None
        return float(v)
    except Exception:
        return None

def get_information_list_rows():
    """Read the ONE shared Information Lists table, with explicit failure."""
    errors = []
    clients = [('Poomsae', supabase), ('Kyeorugi', kyeorugi_db)]
    available = [(label, db) for label, db in clients if db is not None]
    if not available:
        raise RuntimeError('Chưa cấu hình kết nối Supabase cho Information Lists.')
    empty_success = False
    for label, db in available:
        try:
            rows, offset = [], 0
            while True:
                batch = (_scoped_query(db, 'Information Lists')
                         .range(offset, offset + 499).execute().data or [])
                rows.extend(batch)
                if len(batch) < 500:
                    break
                offset += 500
            if rows:
                return rows
            empty_success = True
        except Exception as exc:
            errors.append(f'{label}: {type(exc).__name__}: {exc}')
            print('[INFORMATION LIST]', label, type(exc).__name__)
    if empty_success:
        return []
    raise RuntimeError('Không thể đọc bảng Information Lists. ' + '; '.join(errors))


def get_results_for_division(division_key: str):
    """
    Bảng Result hiện tại không có cột Division, nên không lọc theo Division ở Supabase.
    Trả toàn bộ Result để JS tự đối chiếu theo Match/Result và không làm văng web.
    """
    try:
        rows = []
        for offset in range(0, 50000, 500):
            batch = (_scoped_query(supabase, RESULT_TABLE)
                     .range(offset, offset + 499).execute().data or [])
            rows.extend(batch)
            if len(batch) < 500:
                break
        for r in rows:
            r["_score"] = safe_float(
                r.get("Total")
                or r.get("total")
                or r.get("Score")
                or r.get("score")
            )
        return rows
    except Exception as e:
        print("Result load error:", e)
        return []

# ================== KYEORUGI / WEIGH-IN ==================
K_INFO = 'Information Lists'
K_REFEREES = 'Referee Lists'
K_ROLES = ('Weigh-in', 'Inspectors', 'Coordinator', 'Recorder', 'Referee', 'Judge 1', 'Judge 2', 'Judge 3', 'Judge 4', 'Judge 5', 'Judge 6', 'Review Jury', 'Technical Assistant')
K_WEIGHTS = ('Gen. Weigh-in 1', 'Gen. Weigh-in 2', 'Random Weigh-in')


def _kyeorugi_client():
    if kyeorugi_db is None:
        raise RuntimeError('Chưa có kết nối Kyeorugi: kiểm tra URL Supabase hợp lệ và secret key trong .env; xem Terminal để biết hostname bị lỗi.')
    return kyeorugi_db


def _pages(table, *, columns='*'):
    db = _kyeorugi_client()
    rows = []
    start = 0
    while True:
        block = _scoped_query(db, table, columns).range(start, start + 499).execute().data or []
        rows.extend(block)
        if len(block) < 500:
            break
        start += 500
    return rows


def _kyeorugi_event_meta(event):
    """Accept Kyeorugi age/weight labels, including bare -38kg and -38.+38."""
    text = str(event or '').strip()
    low = text.lower()
    if not text or any(x in low for x in ('poomsae', 'recognized', 'freestyle', 'demonstration')):
        return None
    weights = re.findall(r'([+-])\s*(\d{1,3})(?:\s*kg)?', text, re.I)
    team = re.search(r'\bteam\s*([345])\b', text, re.I)
    mixed = bool(re.search(r'\bmixed\s*team\b', text, re.I))
    if not weights and not team and not mixed:
        return None
    division = ('Mixed Team' if mixed else f'Team {team.group(1)}' if team else 'Individual')
    # Check Female first, because 'female' contains 'male'.
    gender = ('Female' if re.search(r'\bfemale\b', low) else
              'Male' if re.search(r'\bmale\b', low) else 'Mixed' if mixed else '')
    age_match = re.search(r'\b(U9|U12|U14|U17|O18|Cadet|Junior|Senior)\b', text, re.I)
    age = age_match.group(1) if age_match else 'Open'
    age = {'u14':'Cadet (U14)', 'cadet':'Cadet (U14)',
           'u17':'Junior (U17)', 'junior':'Junior (U17)',
           'o18':'Senior (O18)', 'senior':'Senior (O18)'}.get(age.lower(), age.upper() if age.lower().startswith('u') else age)
    return {'division': division, 'gender': gender, 'age': age,
            'weight': f'{weights[0][0]}{weights[0][1]} kg' if division == 'Individual' and weights else ''}


def _kyeorugi_events(row):
    found = []
    for key, value in row.items():
        if re.fullmatch(r'Event_\d+', str(key), re.I) and value:
            meta = _kyeorugi_event_meta(value)
            if not meta:
                continue
            if meta['division'] == 'Individual':
                for sign, number in re.findall(r'([+-])\s*(\d{1,3})(?:\s*kg)?', str(value), re.I):
                    one = {**meta, 'weight': f'{sign}{number} kg'}
                    if one not in found:
                        found.append(one)
            elif meta not in found:
                found.append(meta)
    return found


def _kyeorugi_rows():
    # Explicit Position check to prevent officials/coaches being weighed.
    data = []
    for row in _pages(K_INFO):
        position = str(row.get('Position') or 'Athlete').lower()
        if any(x in position for x in ('coach', 'referee', 'official', 'manager', 'hlv')):
            continue
        events = _kyeorugi_events(row)
        if events:
            row['_kyeorugi_events'] = events
            data.append(row)
    return data


def _schedule_division_meta(label):
    """Parse Kyeorugi Matchs.Division; never infer a division from athlete events."""
    raw = re.sub(r'\s+', ' ', str(label or '').strip())
    if not raw:
        return None
    low = raw.lower()
    if re.search(r'\b(mixed\s*team|team\s*mixed)\b', low):
        group, team_kind = 'Team', 'Mixed Team'
    else:
        t = re.search(r'\bteam\s*([345])\b', low)
        if t:
            group, team_kind = 'Team', 'Team ' + t.group(1)
        elif re.search(r'\bindividuals?\b', low):
            group, team_kind = 'Individuals', ''
        else:
            return None
    gender = ('Female' if re.search(r'\bfemale\b', low) else
              'Male' if re.search(r'\bmale\b', low) else None)
    # A scheduled division without a gender remains hidden until its classification
    # is specified, rather than incorrectly merging male and female brackets.
    if gender is None:
        return None
    match = re.search(r'\b(U9|U12|U14|U17|O18|Cadet|Junior|Senior)\b', raw, re.I)
    if match is None:
        return None
    age_code = match.group(1).lower()
    age = {'u9': 'U9', 'u12': 'U12', 'u14': 'Cadet (U14)',
           'cadet': 'Cadet (U14)', 'u17': 'Junior (U17)',
           'junior': 'Junior (U17)', 'o18': 'Senior (O18)',
           'senior': 'Senior (O18)'}[age_code]
    weights = re.findall(r'([+-])\s*(\d{1,3})(?:\s*kg)?', raw, re.I)
    if group == 'Individuals' and not weights:
        return None
    weight = (f'{weights[-1][0]}{int(weights[-1][1])}kg' if group == 'Individuals' else '')
    return {'group': group, 'team_kind': team_kind, 'gender': gender,
            'age': age, 'weight': weight}


def _kyeorugi_divisions():
    """Only divisions actually scheduled in Kyeorugi Kyeorugi Matchs."""
    groups = {}
    for row in _pages('Kyeorugi Matchs'):
        raw = str(row.get('Division') or '').strip()
        meta = _schedule_division_meta(raw)
        if not meta:
            continue
        # Case-insensitive duplicate labels are counted together.
        key = raw.casefold()
        if key not in groups:
            groups[key] = {**meta, 'key': 'Kyeorugi - ' + raw,
                           'schedule_division': raw, 'count': 0}
        groups[key]['count'] += 1
    return list(groups.values())


def _api_error(err, status=400):
    return jsonify({'error': str(err)}), status


def _require_referee(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if (not session.get('k_referee_id')
                or session.get('k_event_code') != _request_event_code()
                or not session.get('k_page_token')
                or request.headers.get('X-Weighin-Page') != session.get('k_page_token')):
            return _api_error('Vui lòng đăng nhập Referee Portal.',401)
        return fn(*args,**kwargs)
    return inner

def _require_official(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if (not session.get('k_referee_id') or not session.get('k_referee_weighin')
                or session.get('k_event_code') != _request_event_code()
                or request.headers.get('X-Weighin-Page') != session.get('k_page_token')):
            return _api_error('Chưa xác thực trọng tài cân.', 401)
        try:
            assigned = (_kyeorugi_client().table(K_REFEREES).select('"Weigh-in"')
                        .eq('"ID.Referee"', session['k_referee_id'])
                        .eq('event_code', _request_event_code()).limit(1).execute().data or [])
            if not assigned or not _allowed_assignment(assigned[0].get('Weigh-in')):
                session['k_referee_weighin'] = False
                return _api_error('Phân công Weigh-in đã bị thu hồi hoặc không tồn tại.', 403)
        except Exception:
            app.logger.warning('Unable to verify current weigh-in assignment')
            return _api_error('Không xác minh được quyền cân ký. Vui lòng thử lại.', 503)
        return fn(*args, **kwargs)
    return inner


def _clear_weighin_session():
    """Revoke only weigh-in permission, preserving unrelated portal sessions."""
    for key in ('k_event_code', 'k_referee_id', 'k_referee_weighin',
                'k_referee_name', 'k_csrf', 'k_page_token'):
        session.pop(key, None)


def _allowed_assignment(value):
    # Any value other than SQL NULL or a blank/whitespace-only string grants duty.
    # Examples: 'OK', 'Ok', 'X', 'V', '1', 'Assigned' all count.
    return value is not None and bool(str(value).strip())


@app.route('/api/kyeorugi/athletes')
@_require_official
def kyeorugi_athletes_api():
    try:
        rows = _kyeorugi_rows()
        # Minimize returned athlete fields; avoid sharing full personal records.
        keep = ('GAL/ID ATHLETE', 'Full name', 'DOB', 'Gender', 'NOC', 'Country',
                *K_WEIGHTS)
        return jsonify([{**{k: r.get(k) for k in keep}, '_events': r['_kyeorugi_events']}
                        for r in rows])
    except Exception as exc:
        return _api_error(exc, 503)


@app.route('/api/kyeorugi/session')
def kyeorugi_session_api():
    authorized = bool(session.get('k_referee_id') and
                      session.get('k_event_code') == _request_event_code())
    return jsonify({'authorized':authorized,
                    'weighin':bool(session.get('k_referee_weighin')) if authorized else False,
                    'referee':session.get('k_referee_name','') if authorized else '',
                    'csrf':session.get('k_csrf','') if authorized else ''})


@app.route('/api/kyeorugi/login', methods=['POST'])
def kyeorugi_login_api():
    try:
        body = request.get_json(silent=True) or {}
        # Referee ID is used for event-scoped identification (not strong authentication).
        from time import monotonic
        remote = request.remote_addr or 'unknown'
        now = monotonic()
        recent = [t for t in _LOGIN_ATTEMPTS.get(remote,[]) if now-t < 600]
        _LOGIN_ATTEMPTS[remote]=recent
        if len(recent) >= 10:
            return _api_error('Đã vượt số lần thử đăng nhập. Thử lại sau.',429)
        _LOGIN_ATTEMPTS[remote].append(now)
        purpose = str(body.get('purpose') or 'referee-list').strip()
        referee_id = str(body.get('referee_id') or '').strip()
        if not referee_id or len(referee_id) > 100:
            return _api_error('Nhập ID Referee.', 400)
        # Always scope identity to the selected Event Code on the server.
        rows = (_kyeorugi_client().table(K_REFEREES).select('*')
                .eq('"ID.Referee"', referee_id).eq('event_code', _request_event_code()).limit(1).execute().data or [])
        official = rows[0] if rows else None
        if not official:
            return _api_error('ID Referee không tồn tại.', 403)
        has_weighin_duty = _allowed_assignment(official.get('Weigh-in'))
        if purpose == 'weigh-in' and not has_weighin_duty:
            return _api_error('ID Referee chưa được phân công Weigh-in cho giải đấu này.', 403)
        session.clear()
        session['k_event_code'] = _request_event_code()
        session['k_referee_id'] = referee_id
        session['k_referee_weighin'] = has_weighin_duty
        session['k_referee_name'] = official.get('Fullname') or referee_id
        session['k_csrf'] = token_urlsafe(24)
        session['k_page_token'] = token_urlsafe(32)
        return jsonify({'authorized': True, 'weighin': has_weighin_duty, 'referee': session['k_referee_name'],
                        'csrf': session['k_csrf'], 'page_token': session['k_page_token']})
    except Exception as exc:
        app.logger.error('Referee login failed: %s', type(exc).__name__)
        return _api_error('Không đăng nhập được. Kiểm tra cấu hình và bảng Referee Lists trên máy chủ.', 503)


@app.route('/api/kyeorugi/logout', methods=['POST'])
def kyeorugi_logout_api():
    if request.headers.get('X-CSRF-Token') != session.get('k_csrf'):
        return _api_error('CSRF token không hợp lệ.', 403)
    _clear_weighin_session()
    return jsonify({'ok': True})


@app.route('/api/kyeorugi/leave', methods=['POST'])
def kyeorugi_leave_api():
    """End weigh-in when navigating menus/pages (also supports sendBeacon)."""
    body = request.get_json(silent=True) or {}
    csrf = request.headers.get('X-CSRF-Token') or body.get('csrf')
    if not csrf or csrf != session.get('k_csrf'):
        return _api_error('CSRF token không hợp lệ.', 403)
    _clear_weighin_session()
    return jsonify({'ok': True})


# Direct-write controller for the current single-process Flask installation.
# Correction windows are intentionally NON-persistent: a server restart closes them.
# For multi-worker/VPS deployments, enforce the same 5-second rule atomically in
# PostgreSQL (RPC/trigger), rather than relying on process-local state.
_K_WEIGH_WRITE_LOCK = threading.RLock()
_K_WEIGH_WINDOWS = {}  # (event_code, athlete_id, field) -> (referee_id, first_monotonic_time)

@app.route('/api/kyeorugi/weigh-in', methods=['POST'])
@_require_official
def kyeorugi_weighin_api():
    if request.headers.get('X-CSRF-Token') != session.get('k_csrf'):
        return _api_error('CSRF token không hợp lệ.', 403)
    body = request.get_json(silent=True) or {}
    athlete = str(body.get('athlete_id') or '').strip()
    field = str(body.get('field') or '').strip()
    raw = str(body.get('weight') or '').strip().replace(',', '.')
    if field not in K_WEIGHTS or not athlete or len(athlete) > 150:
        return _api_error('VĐV hoặc cột cân không hợp lệ.')
    try:
        num = Decimal(raw)
        if not num.is_finite() or num <= 0 or num > 999 or num.as_tuple().exponent < -3:
            raise ValueError()
        new_weight = str(num.quantize(Decimal('0.001')))
    except (InvalidOperation, ValueError):
        return _api_error('Số cân không hợp lệ. Ví dụ: 58,890.')

    event_code = _request_event_code()
    referee_id = session['k_referee_id']
    db = _kyeorugi_client()
    key = (event_code, athlete, field)
    try:
        # Revalidate referee duty for each write. Never trust client-side "Authorized".
        official = (db.table(K_REFEREES).select('"Weigh-in"')
                    .eq('"ID.Referee"', referee_id)
                    .eq('event_code', event_code).limit(1).execute().data or [])
        if not official or not _allowed_assignment(official[0].get('Weigh-in')):
            session.clear()
            return _api_error('Trọng tài không được phân công cân.', 403)

        with _K_WEIGH_WRITE_LOCK:
            # Use BOTH event_code and GAL/ID. Reject ambiguous identifiers rather
            # than updating multiple rows with the same athlete ID.
            records = (db.table(K_INFO).select('*')
                       .eq('event_code', event_code)
                       .eq('"GAL/ID ATHLETE"', athlete)
                       .limit(2).execute().data or [])
            if len(records) != 1:
                return _api_error('Không tìm thấy đúng một VĐV theo mã GAL/ID trong giải.', 409)
            record = records[0]
            if field not in record:
                return _api_error('Cột cân không tồn tại trong Information Lists: ' + field, 409)
            current = record[field]
            has_value = current is not None and str(current).strip() != ''
            now = time.monotonic()
            window = _K_WEIGH_WINDOWS.get(key)
            if has_value:
                if not window:
                    return _api_error('Ô cân đã có dữ liệu và đang khóa. Không được ghi đè.', 409)
                original_referee, first_at = window
                if original_referee != referee_id:
                    return _api_error('Chỉ trọng tài ghi lần đầu mới được chỉnh sửa.', 403)
                if now - first_at >= 5:
                    return _api_error('Đã hết 5 giây chỉnh sửa.', 409)
            else:
                # A new initial write must not inherit an old correction window.
                _K_WEIGH_WINDOWS.pop(key, None)

            # Optimistic compare-and-set: only modify the row if the old cell
            # still equals what we read. This prevents a stale browser overwriting
            # a recent write by another referee.
            upd = (db.table(K_INFO).update({field: new_weight})
                   .eq('event_code', event_code)
                   .eq('"GAL/ID ATHLETE"', athlete))
            if current is None:
                upd = upd.is_(f'"{field}"', 'null')
            else:
                upd = upd.eq(f'"{field}"', current)
            saved = upd.execute().data or []
            if len(saved) != 1:
                return _api_error('Ô cân vừa thay đổi hoặc không được phép ghi; hãy Refresh.', 409)
            if not has_value:
                _K_WEIGH_WINDOWS[key] = (referee_id, time.monotonic())
            expires = _K_WEIGH_WINDOWS.get(key, ('', 0))[1] + 5
            return jsonify({'ok': True, 'weight': new_weight,
                            'seconds_remaining': max(0, round(expires-time.monotonic(), 2))})
    except Exception as exc:
        # Log only safe diagnostic fields, never access tokens or personal data.
        code = str(getattr(exc, 'code', '') or '')
        message = str(getattr(exc, 'message', '') or '')
        message = re.sub(r'[A-Za-z0-9_-]{45,}', '[REDACTED]', message)
        message = re.sub(r'(?i)(bearer|apikey|token|secret)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', message)
        app.logger.error('WEIGHIN DIRECT SAVE FAILED | code=%s | error=%s', code[:40], message[:280])
        return _api_error('Không ghi được số cân vào Information Lists. Kiểm tra Terminal.', 503)



REFEREE_HTML = r'''<!doctype html><html lang="vi"><head>
<link rel="icon" type="image/png" sizes="any" href="/referee-logo?v=20261118" />
<link rel="apple-touch-icon" href="/referee-logo?v=20261118" /><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Referee | {{ event_name }}</title><style>

*{box-sizing:border-box}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#07111f;color:#eef6ff}
.topbar{
    min-height:126px;
    height:auto;
    display:grid;
    grid-template-columns:180px minmax(0,1fr) 180px;
    align-items:center;
    padding:10px 24px;
    background:linear-gradient(90deg,#03152d,#064b93,#03152d);
    border-bottom:4px solid #f5c542;
}

.logoBox img{
    max-height:100px;
    max-width:150px;
    object-fit:contain;
    background:white;
}
/* Keep the event logo visible on the left even if an image is temporarily missing. */
.logoBox{display:flex;align-items:center;justify-content:flex-start;min-width:0}
.site-logo-fallback{width:100px;height:74px;align-items:center;justify-content:center;background:#fff;color:#142d41;font:bold italic 31px Arial,sans-serif;border-radius:3px}

.eventTitle{
    text-align:center;
    min-width:0;
    overflow:hidden;
    padding:0 10px;
}

.eventTitle h1{
    margin:0 0 6px;
    font-size:clamp(20px, 3vw, 34px);
    line-height:1.12;
    text-transform:uppercase;
    letter-spacing:.8px;
    white-space:normal;
    overflow-wrap:break-word;
}

.eventTitle div{
    font-size:clamp(13px, 1.6vw, 18px);
    line-height:1.25;
}
.layout{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 118px)}
.sidebar{background:#081827;border-right:1px solid #21415f;padding:16px;overflow:auto}
.sideTitle{font-weight:900;color:#f5c542;margin:6px 0 14px;font-size:20px}
.quickMenu{
    margin-bottom:18px;
    padding-bottom:16px;
    border-bottom:1px solid #21415f;
}

.quickMenuTitle{
    font-weight:900;
    color:#ffd34d;
    margin:6px 0 12px;
    font-size:20px;
    text-transform:uppercase;
}

.quickMenuBtn{
    width:100%;
    display:block;
    border:1px solid #244765;
    border-radius:10px;
    background:#123c67;
    color:white;
    font-size:17px;
    font-weight:900;
    padding:13px 14px;
    margin-bottom:9px;
    text-align:left;
    cursor:pointer;
}

.quickMenuBtn:hover,
.quickMenuBtn.active{
    background:#ffd34d;
    color:#06111f;
}

.tool-card{
    background:#0b1d30;
    border:1px solid #203e5b;
    border-radius:14px;
    padding:14px;
    margin-bottom:16px;
}

.tool-title-row{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    margin-bottom:12px;
}

.tool-title-row h3{
    margin:0;
    color:#ffd34d;
    font-size:22px;
}

.filter-grid{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:10px;
    margin-bottom:12px;
}

.filter-grid input,
.filter-grid select{
    width:100%;
    padding:10px 12px;
    border:1px solid #244765;
    border-radius:10px;
    background:#06192b;
    color:white;
    outline:none;
}

.filter-grid input::placeholder{
    color:#91a9bf;
}

.table-scroll{
    width:100%;
    overflow:auto;
    border:1px solid #1e3a55;
    border-radius:12px;
}

#infoListTable,
#rankingTable{
    min-width:900px;
}

.rank-gold{
    color:#ffd34d;
    font-weight:900;
}

.rank-silver{
    color:#dce7f3;
    font-weight:900;
}

.rank-bronze{
    color:#e2a85d;
    font-weight:900;
}

.rank-fourth{
    color:#9fd0ff;
    font-weight:900;
}

.ranking-card-grid{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(420px,1fr));
    gap:16px;
}
/* Kyeorugi ranking: 3 divisions per row on desktop. */
/* Same card dimensions, title fonts and table spacings as Poomsae Ranking.
   Reserve three equal grid slots on wide screens, even when only two exist. */
#kRankingCards{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:1100px ){ #kRankingCards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px ){ #kRankingCards{grid-template-columns:1fr}}


.ranking-division-card{
    border:1px solid #24527a;
    border-radius:14px;
    overflow:hidden;
    background:#08233c;
}

.ranking-division-title{
    background:#123c67;
    color:#ffd34d;
    font-size:18px;
    font-weight:900;
    text-align:center;
    padding:12px;
    letter-spacing:.5px;
}

.ranking-medal-table{
    width:100%;
    border-collapse:collapse;
    min-width:0!important;
}

.ranking-medal-table th{
    background:#0f2e4e;
    color:white;
    font-size:13px;
    padding:9px;
}

.ranking-medal-table td{
    color:white;
    font-size:14px;
    font-weight:700;
    padding:10px;
    border-bottom:1px solid #1e3a55;
}

.medal-cell{
    width:95px;
    white-space:nowrap;
    font-weight:900;
}

.medal-gold{
    color:#ffd34d!important;
}

.medal-silver{
    color:#dce7f3!important;
}

.medal-bronze{
    color:#e2a85d!important;
}

.ranking-empty-cell{
    color:#7893aa!important;
    font-weight:600!important;
}

.medal-standing-table{
    min-width:1200px;
    table-layout:auto;
}

.medal-standing-table th{
    text-align:center;
    vertical-align:middle;
    white-space:nowrap;
}

.medal-standing-table td{
    text-align:center;
    vertical-align:middle;
}



.medal-division-cell{
    min-width:118px;
    font-weight:900;
    line-height:1.35;
    white-space:nowrap;
}

.medal-icon-wrap{
    display:flex;
    align-items:center;
    justify-content:center;
    gap:6px;
    font-size:20px;
    min-height:24px;
}

.medal-icon-item{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    font-weight:900;
}

.medal-icon-symbol{
    font-size:22px;
    line-height:1;
}

.medal-standing-table{
    min-width:1200px;
    table-layout:auto;
    border-collapse:separate;
    border-spacing:0;
}

.medal-standing-table thead th{
    text-align:center!important;
    vertical-align:middle!important;
    white-space:nowrap;
    font-weight:900;
    border:1px solid #3b79ad!important;
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);
}

.medal-standing-table tbody td{
    border-right:1px solid #1e3a55;
    border-bottom:1px solid #1e3a55;
}

.medal-standing-head-type{
    background:#164d82!important;
    color:#ffd34d!important;
    font-size:17px!important;
    letter-spacing:.5px;
    height:34px;
}

.medal-standing-head-division{
    background:#1fd0a6!important;
    color:#06111f!important;
    font-size:15px!important;
    height:32px;
    text-transform:uppercase;
}

.medal-standing-head-age{
    background:#0b2947!important;
    color:#d7efff!important;
    font-size:12px!important;
    min-width:120px;
    height:32px;
}

.medal-standing-fixed-head{
    background:#123c67!important;
    color:#ffd34d!important;
    font-size:14px!important;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-summary-head{
    background:#123c67!important;
    color:#ffd34d!important;
    font-size:13px!important;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

/* ===== Medal Standings: chỉ cố định cột Unit/NOC, không ăn nhầm header con ===== */

.medal-standing-fixed-head{
    position:sticky;
    left:0;
    z-index:20;
    background:#123c67!important;
    color:#ffd34d!important;
    font-size:14px!important;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-unit-cell{
    position:sticky;
    left:0;
    z-index:10;
    background:#0b1d30!important;
    font-weight:900;
    color:#ffd34d;
    text-align:left!important;
    border-right:1px solid #3b79ad!important;
    border-bottom:1px solid #1e3a55!important;
}

.medal-standing-head-type{
    background:#164d82!important;
    color:#ffd34d!important;
    font-size:17px!important;
    letter-spacing:.5px;
    height:34px;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-head-division{
    background:#1fd0a6!important;
    color:#06111f!important;
    font-size:15px!important;
    height:32px;
    text-transform:uppercase;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-head-age{
    background:#0b2947!important;
    color:#d7efff!important;
    font-size:12px!important;
    min-width:120px;
    height:32px;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-summary-head{
    background:#123c67!important;
    color:#ffd34d!important;
    font-size:13px!important;
    text-align:center!important;
    vertical-align:middle!important;
    border:1px solid #3b79ad!important;
}

.medal-standing-gold{
    color:#ffd34d;
}

.medal-standing-silver{
    color:#dce7f3;
}

.medal-standing-bronze{
    color:#e2a85d;
}

.medal-standing-total{
    font-weight:900;
    font-size:15px;
}

.medal-standing-rank{
    font-size:18px;
    font-weight:900;
    color:#7fe7ff;
}

.medal-unit-sub{
    display:block;
    margin-top:4px;
    color:#9bb7cf;
    font-size:12px;
    font-weight:700;
    line-height:1.35;
}

.medal-unit-main{
    display:block;
    color:#ffd34d;
    font-size:15px;
    font-weight:900;
    margin-bottom:4px;
}

.medal-unit-total{
    color:#ffffff;
    font-weight:900;
}

.medal-unit-male{
    color:#7fe7ff;
    font-weight:800;
}

.medal-unit-female{
    color:#ffb3d1;
    font-weight:800;
}

.medal-unit-other{
    color:#cfe6ff;
    font-weight:800;
}

.medal-unit-coach{
    color:#b8ff7a;
    font-weight:800;
}

.group{margin-bottom:10px;border:1px solid #203b57;border-radius:12px;overflow:hidden;background:#0c2135}
.groupHead,.subHead,.genderHead,.ageBtn{
    width:100%;
    border:0;
    color:#fff;
    text-align:left;
    cursor:pointer;
}

.groupHead{
    padding:14px 18px;
    background:#123c67;
    font-size:18px;
    font-weight:900;
}

.subHead{
    padding:12px 34px;
    background:#0f2e4e;
    font-size:16px;
    font-weight:800;
}

.genderHead{
    padding:11px 52px;
    background:#0c2842;
    color:#d7e9ff;
    font-size:15px;
    font-weight:800;
}

.ageBtn{
    padding:10px 76px;
    background:#0b2239;
    font-size:14px;
    border-top:1px solid #173959;
}

.ageBtn:hover,
.ageBtn.active{
    background:#f5c542;
    color:#06111f;
    font-weight:900;
}
.hidden{display:none!important}
.notice-dialog{width:min(610px,calc(100vw - 28px));text-align:center}
.notice-message{font-size:16px;line-height:1.65;white-space:pre-line;color:#eef6ff;margin:18px 0}
.notice-dialog .actions{justify-content:center!important}
.notice-dialog .actions button{min-width:125px}
.content{padding:18px;overflow:auto}
.selectedBox{display:flex;justify-content:space-between;align-items:center;background:#0d2238;border:1px solid #244765;border-radius:14px;padding:16px 18px;margin-bottom:16px}
.label{color:#9bb7cf;font-size:13px;text-transform:uppercase}
.selectedBox h2{margin:4px 0 0;font-size:25px}
.selectedBox button,.btn-result{padding:10px 22px;border:0;border-radius:10px;background:#ffd34d;color:#001b33;font-weight:900;cursor:pointer}
.selectedBox button:hover,.btn-result:hover{filter:brightness(1.08)}
.card{background:#0b1d30;border:1px solid #203e5b;border-radius:14px;padding:14px;margin-bottom:16px}
.section-title-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.section-title-row h3,.card h3{margin:0;color:#ffd34d}
table{
    width:100%;
    max-width:100%;
    border-collapse:collapse;
    min-width:0;
    table-layout:auto;
}
th,td{
    border-bottom:1px solid #1e3a55;
    padding:10px;
    text-align:left;
    font-size:14px;
    white-space:normal;
    word-break:normal;
    overflow-wrap:break-word;
}
#athleteTable th:nth-child(1),
#athleteTable td:nth-child(1){
    width:70px;
    text-align:center;
    white-space:nowrap;
}

#athleteTable th:nth-child(2),
#athleteTable td:nth-child(2){
    width:90px;
    text-align:center;
    white-space:nowrap;
}

#athleteTable th:nth-child(3),
#athleteTable td:nth-child(3){
    width:auto;
    min-width:260px;
    white-space:normal;
    word-break:normal;
    overflow-wrap:break-word;
}

#athleteTable th:nth-child(4),
#athleteTable td:nth-child(4){
    width:95px;
    text-align:center;
    white-space:nowrap;
}
th{background:#123c67;color:#fff;position:sticky;top:0}
tr:hover td{background:#102b46}
.empty{padding:18px;color:#b9c9d8}
.layout.result-mode{display:block}
.layout.result-mode .content{width:100%;max-width:none;padding:18px;min-height:calc(100vh - 118px)}
.layout.result-mode #sigmaView{min-height:calc(100vh - 155px);display:flex;flex-direction:column}
.layout.result-mode .sigma-bracket{flex:1;min-height:0}
.layout.result-mode .sigma-svg{height:auto!important;min-height:0!important}
.sigma-view{margin-top:10px;border:1px solid #24527a;border-radius:14px;padding:14px;background:#06192b}
.sigma-title-row{
    display:grid;
    grid-template-columns:120px 1fr 120px;
    align-items:center;
    margin-bottom:22px;
    padding-top:6px;
}
.sigma-title{
    text-align:center;
    color:#ffd34d;
    font-size:38px;
    font-weight:900;
    letter-spacing:4px;
    margin:0;
    line-height:1.05;
    text-shadow:
        0 2px 0 rgba(0,0,0,.55),
        0 0 12px rgba(255,211,77,.25);
}
.sigma-bracket{display:block;width:100%;overflow:hidden;padding:0}
.sigma-svg{width:100%;height:auto;min-height:0;display:block;background:#06192b;border:1px solid #24527a;border-radius:12px}
.sigma-svg-box{fill:#08233c;stroke:#2f6fa3;stroke-width:1.8}
.sigma-svg-box.player-blue{
    fill:#0B63CE;
    stroke:#003B8E;
    stroke-width:3;
}

.sigma-svg-box.player-red{
    fill:#C93C4A;
    stroke:#7A1F2B;
    stroke-width:3;
}

.sigma-svg-name-blue,
.sigma-svg-noc-blue,
.sigma-svg-seed-blue{
    fill:white;
    font-size:15px;
    font-weight:900;
}

.sigma-svg-name-red,
.sigma-svg-noc-red,
.sigma-svg-seed-red{
    fill:white;
    font-size:15px;
    font-weight:900;
}
.sigma-svg-line{
    stroke:#9fd0ff;
    stroke-width:2;
    fill:none;
    stroke-linecap:square;
    stroke-linejoin:miter;
}

.sigma-svg-guide{
    stroke:#5f7f9d;
    stroke-width:1;
    stroke-dasharray:4 6;
    opacity:.55;
}

.sigma-svg-phase-box{
    fill:#f5f8fc;
    stroke:#cbd5e1;
    stroke-width:1.2;
}

.sigma-svg-phase-text{
    fill:#17324d;
    font-size:12px;
    font-weight:800;
}

.sigma-svg-code-box{
    fill:#ffffff;
    stroke:green;
    stroke-width:2.2;
}

.sigma-svg-code{
    fill:green;
    font-size:13px;
    font-weight:900;
    paint-order:stroke;
    stroke:#7a5a00;
    stroke-width:.6px;
    stroke-linejoin:round;
}
.sigma-svg-code-box.winner{fill:#ffd34d;stroke:#ffd34d}

.sigma-svg-code-box.winner-blue{
    fill:#0B63CE;
    stroke:#003B8E;
    stroke-width:3;
}

.sigma-svg-code-box.winner-red{
    fill:#C93C4A;
    stroke:#7A1F2B;
    stroke-width:3;
}



.sigma-svg-winner-code{
    fill:#001b33;
    font-size:14px;
    font-weight:900;
}

.sigma-svg-winner-code-blue{
    fill:white;
    font-size:14px;
    font-weight:900;
}

.sigma-svg-winner-code-red{
    fill:white;
    font-size:14px;
    font-weight:900;
}

/* ===== Champion Final Box ===== */
.sigma-svg-code-box.champion{
    fill:#f6d56b!important;
    stroke:#ffbf00!important;
    stroke-width:3.5!important;
}

.sigma-svg-winner-code-champion{
    fill:#ffffff!important;
    font-size:15px!important;
    font-weight:900!important;
    paint-order:stroke;
    stroke:#5b4100;
    stroke-width:2px;
    stroke-linejoin:round;
}

.sigma-svg-champion-cup{
    font-size:42px!important;
    font-weight:900!important;
}


@media(max-width:1100px){.layout{grid-template-columns:280px 1fr}.eventTitle h1{font-size:25px}}
@media(max-width:700px){
    .topbar{
        grid-template-columns:120px minmax(0,1fr);
        padding:8px 10px;
        min-height:110px;
    }

    .rightBlank{
        display:none;
    }

    .logoBox img{
        max-height:78px;
        max-width:110px;
    }

    .eventTitle{
        padding:0 4px;
    }

    .header-title{
        font-size:clamp(18px, 6vw, 28px);
        line-height:1.08;
        letter-spacing:.5px;
    }

    .header-date,
    .header-location{
        font-size:clamp(11px, 3.2vw, 15px);
        line-height:1.18;
        margin-top:3px;
    }

    .layout{
        min-height:calc(100vh - 110px);
    }
}

@media(max-width:430px){
    .topbar{
        grid-template-columns:1fr;
        text-align:center;
        min-height:auto;
        padding:8px 8px 10px;
    }

    .logoBox{
        display:flex;
        justify-content:center;
        margin-bottom:6px;
    }

    .logoBox img{
        max-height:62px;
        max-width:96px;
    }

    .header-title{
        font-size:22px;
    }

    .header-date,
    .header-location{
        font-size:12px;
    }
}
.header-title{
    color:#FF8F0F;
    font-size:clamp(34px, 4.2vw, 60px);
    font-weight:900;
    letter-spacing:1.4px;
    line-height:1.05;
    text-transform:uppercase;
    text-shadow:
        0 2px 0 rgba(0,30,70,.9),
        0 0 10px rgba(255,255,255,.38),
        0 0 22px rgba(0,180,255,.25);
}

.event-meta{
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    gap:5px;
    margin-top:8px;
}

.event-meta-row{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:8px;
    min-height:25px;
    padding:3px 13px;
    border:1px solid rgba(255,211,77,.35);
    border-radius:999px;
    background:rgba(3,21,45,.45);
    box-shadow:
        inset 0 0 0 1px rgba(255,255,255,.04),
        0 3px 10px rgba(0,0,0,.18);
}

.event-meta-row.venue-row{
    border-color:rgba(127,231,255,.32);
    background:rgba(3,28,55,.42);
}

.event-meta-label{
    color:#ffd34d;
    font-size:12px;
    font-weight:900;
    letter-spacing:1.2px;
    text-transform:uppercase;
}

.header-date{
    color:#ffffff;
    font-size:16px;
    font-weight:900;
    letter-spacing:.3px;
}

.header-location{
    color:#7fe7ff;
    font-size:16px;
    font-weight:900;
    letter-spacing:.2px;
}
.genderHead{
    width:100%;
    border:0;
    cursor:pointer;
    text-align:left;

    padding:10px 52px;

    background:#12385d;

    color:#d7e9ff;
    font-size:15px;
    font-weight:700;

    border-left:4px solid #2f6aa3;
}
.cutoff-result-wrap{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(360px,1fr));
    gap:18px;
    padding:10px;
}

.cutoff-phase-box{
    border:1px solid #24527a;
    border-radius:14px;
    background:#08233c;
    overflow:hidden;
}

.cutoff-phase-title{
    background:#123c67;
    color:#ffd34d;
    font-size:22px;
    font-weight:900;
    text-align:center;
    padding:12px;
    letter-spacing:1px;
}

.cutoff-table{
    width:100%;
    min-width:0;
    border-collapse:collapse;
}

.cutoff-table th{
    background:#0f2e4e;
    color:white;
    font-size:14px;
    padding:10px;
}

.cutoff-table td{
    color:white;
    font-size:15px;
    font-weight:700;
    padding:10px;
    border-bottom:1px solid #1e3a55;
}

.cutoff-table td:first-child{
    color:#ffd34d;
    font-weight:900;
    text-align:center;
}

/* ================= LEFT SIDEBAR TOGGLE + DRAG TABLE ================= */

.sidebar-toggle-btn{
    position:fixed;
    left:285px;
    top:150px;
    z-index:9999;
    width:35px;
    height:35px;
    border:2px solid #ffd34d;
    border-radius:10px;
    background:#123c67;
    color:#ffd34d;
    font-size:20px;
    font-weight:900;
    line-height:1;
    cursor:pointer;
    box-shadow:0 8px 24px rgba(0,0,0,.35);
    display:flex;
    align-items:center;
    justify-content:center;
}

.sidebar-toggle-btn:hover{
    background:#ffd34d;
    color:#06111f;
}

body.sidebar-collapsed-desktop .sidebar-toggle-btn{
    left:20px;
}

.sidebar-backdrop{
    display:none;
}

.layout{
    grid-template-columns:340px minmax(0,1fr);
}

.layout > .sidebar{
    grid-column:1;
}

.layout > .content{
    grid-column:2;
    min-width:0;
}

.layout.sidebar-collapsed{
    grid-template-columns:0 minmax(0,1fr);
}

.layout.sidebar-collapsed .sidebar{
    width:0!important;
    min-width:0!important;
    padding:0!important;
    overflow:hidden!important;
    border-right:0!important;
}

.layout.sidebar-collapsed .content{
    grid-column:2;
    padding-left:18px;
}

/* Medal Standings kéo ngang/dọc */
#medalStandingsView .table-scroll{
    max-height:calc(100vh - 265px);
    overflow:auto;
    cursor:grab;
    user-select:none;
    -webkit-overflow-scrolling:touch;
    overscroll-behavior:contain;
}

#medalStandingsView .table-scroll.dragging{
    cursor:grabbing;
}

#medalStandingsTable{
    min-width:max-content;
}

/* ===== Medal Standings: đóng băng header 3 tầng, không bị hở ===== */

#medalStandingsTable{
    border-collapse:separate!important;
    border-spacing:0!important;
}

/* Ép chiều cao 3 hàng header cố định để rowspan hai bên khớp tuyệt đối */
#medalStandingsTable thead tr:nth-child(1){
    height:36px!important;
}

#medalStandingsTable thead tr:nth-child(2){
    height:32px!important;
}

#medalStandingsTable thead tr:nth-child(3){
    height:32px!important;
}

/* Tất cả ô header không dùng padding lớn nữa, tránh tạo khoảng hở */
#medalStandingsTable thead th{
    position:sticky!important;
    padding:0 8px!important;
    line-height:1.15!important;
    box-sizing:border-box!important;
    vertical-align:middle!important;
    background-clip:padding-box!important;
}

/* Hàng 1: Recognized / Freestyle + Unit/NOC + Total */
#medalStandingsTable thead tr:nth-child(1) th{
    top:0!important;
    height:36px!important;
    z-index:60!important;
}

/* Hàng 2: Individual / Pair / Team */
#medalStandingsTable thead tr:nth-child(2) th{
    top:36px!important;
    height:32px!important;
    z-index:58!important;
}

/* Hàng 3: Female Cadet / Male Cadet / ... */
#medalStandingsTable thead tr:nth-child(3) th{
    top:68px!important;
    height:32px!important;
    z-index:56!important;
}

/* Các ô rowspan 3 hàng phải cao đúng bằng 36 + 32 + 32 = 100px */
#medalStandingsTable .medal-standing-fixed-head,
#medalStandingsTable .medal-standing-summary-head{
    height:100px!important;
    min-height:100px!important;
    top:0!important;
    z-index:70!important;
}

/* Unit/NOC header vừa sticky dọc vừa sticky ngang */
#medalStandingsTable .medal-standing-fixed-head{
    position:sticky!important;
    left:0!important;
    z-index:90!important;
}

/* Cột Unit/NOC body sticky ngang */
#medalStandingsTable tbody .medal-standing-unit-cell{
    position:sticky!important;
    left:0!important;
    z-index:35!important;
    background:#0b1d30!important;
}

/* Nền chắc để không bị xuyên/chừa hở khi kéo */
#medalStandingsTable .medal-standing-head-type{
    background:#164d82!important;
}

#medalStandingsTable .medal-standing-head-division{
    background:#1fd0a6!important;
}

#medalStandingsTable .medal-standing-head-age{
    background:#0b2947!important;
}

#medalStandingsTable .medal-standing-fixed-head,
#medalStandingsTable .medal-standing-summary-head{
    background:#123c67!important;
}


/* Mobile / Tablet: menu trượt từ bên trái */
@media(max-width:900px){
    body{
        overflow-x:hidden;
    }

    .topbar{
        grid-template-columns:72px minmax(0,1fr) 48px;
        min-height:92px;
        padding:8px 8px;
    }

    .logoBox img{
        max-height:62px;
        max-width:74px;
    }

    .header-title{
        font-size:clamp(17px, 5vw, 28px)!important;
        line-height:1.08;
    }

    .header-date,
    .header-location{
        font-size:clamp(11px, 3vw, 14px)!important;
    }

    .layout{
        display:block;
        min-height:calc(100vh - 92px);
    }

    .content{
        width:100%;
        padding:10px;
    }

    .sidebar{
        position:fixed;
        top:0;
        left:-86vw;
        width:86vw;
        max-width:360px;
        height:100vh;
        z-index:9998;
        background:#081827;
        border-right:2px solid #ffd34d;
        border-left:0;
        padding:16px;
        overflow:auto;
        transition:left .24s ease;
        box-shadow:12px 0 30px rgba(0,0,0,.45);
    }

    .layout.menu-open .sidebar{
        left:0;
    }

    .sidebar-backdrop{
        position:fixed;
        inset:0;
        z-index:9997;
        background:rgba(0,0,0,.48);
        display:none;
    }

    body.sidebar-open .sidebar-backdrop{
        display:block;
    }

    .sidebar-toggle-btn{
        top:22px;
        left:10px;
        right:auto;
        width:42px;
        height:42px;
        font-size:24px;
        border-radius:11px;
    }

    body.sidebar-open .sidebar-toggle-btn{
        left:calc(min(86vw, 360px) + 8px);
    }

    .tool-card{
        padding:10px;
        border-radius:12px;
    }

    .tool-title-row{
        align-items:flex-start;
    }

    .tool-title-row h3{
        font-size:18px;
        padding-left:0;
    }

    .btn-result{
        padding:9px 14px;
        font-size:13px;
    }

    .filter-grid{
        grid-template-columns:1fr;
        gap:8px;
    }

    #medalStandingsView .table-scroll{
        max-height:calc(100vh - 225px);
        border-radius:10px;
    }

    .medal-standing-table th,
    .medal-standing-table td{
        padding:7px 8px;
        font-size:12px;
    }

    .medal-standing-head-type{
        font-size:14px!important;
        height:30px;
    }

    .medal-standing-head-division{
        font-size:12px!important;
        height:28px;
    }

    .medal-standing-head-age{
        font-size:11px!important;
        min-width:92px;
        height:28px;
    }

    .medal-standing-fixed-head{
        min-width:126px!important;
        font-size:12px!important;
    }

    .medal-standing-unit-cell{
        min-width:126px;
    }

    .medal-unit-sub{
        font-size:10px;
        line-height:1.25;
    }

    .medal-icon-symbol{
        font-size:18px;
    }

    .medal-standing-total,
    .medal-standing-rank{
        font-size:13px;
    }
}

/* ================= CLEAN MOBILE FIX: LIST / RANKING / SIGMA ================= */

/* Không cho trang chính bị kéo ngang, nhưng không ép layout con quá mức */
html,
body{
    max-width:100%;
    overflow-x:hidden;
}

.layout,
.content{
    min-width:0;
}

/* Ranking Summary: desktop giữ đẹp, mobile chuyển 1 cột */
@media(max-width:900px){

    .content{
        padding:10px!important;
        overflow:visible!important;
    }

    .tool-card,
    .card,
    .selectedBox,
    .sigma-view{
        max-width:100%;
        min-width:0;
        box-sizing:border-box;
    }

    /* Ranking không bị bóp / không tràn */
    .ranking-card-grid{
        display:grid!important;
        grid-template-columns:1fr!important;
        gap:14px!important;
        width:100%!important;
    }

    .ranking-division-card{
        width:100%!important;
        max-width:100%!important;
        min-width:0!important;
        border-radius:16px!important;
        overflow:hidden!important;
    }

    .ranking-division-title{
        font-size:20px!important;
        line-height:1.2!important;
        padding:14px 10px!important;
        word-break:break-word;
    }

    .ranking-medal-table{
        width:100%!important;
        min-width:0!important;
        table-layout:fixed!important;
    }

    .ranking-medal-table th,
    .ranking-medal-table td{
        padding:10px 8px!important;
        font-size:14px!important;
        white-space:normal!important;
        word-break:break-word!important;
    }

    .ranking-medal-table th:nth-child(1),
    .ranking-medal-table td:nth-child(1){
        width:30%!important;
    }

    .ranking-medal-table th:nth-child(2),
    .ranking-medal-table td:nth-child(2){
        width:22%!important;
    }

    .ranking-medal-table th:nth-child(3),
    .ranking-medal-table td:nth-child(3){
        width:48%!important;
    }

    .medal-cell{
        width:auto!important;
        white-space:normal!important;
    }

    /* Information List: giữ dạng kéo ngang, không bóp cột */
    #infoListView .table-scroll{
        width:100%!important;
        max-width:100%!important;
        overflow:auto!important;
        -webkit-overflow-scrolling:touch;
    }

    #infoListTable{
        min-width:900px!important;
        width:max-content!important;
    }

    /* Team/Athlete List: bảng kéo ngang trong card, không làm vỡ màn hình */
    .athlete-card{
        overflow:hidden!important;
    }

    .athlete-card #athleteTable{
        display:block!important;
        width:100%!important;
        max-width:100%!important;
        min-width:0!important;
        overflow-x:auto!important;
        -webkit-overflow-scrolling:touch;
        border-collapse:collapse!important;
    }

    #athleteTable thead,
    #athleteTable tbody,
    #athleteTable tr{
        width:max-content;
        min-width:620px;
    }

    #athleteTable th,
    #athleteTable td{
        white-space:nowrap!important;
        font-size:14px!important;
        padding:10px 12px!important;
    }

    #athleteTable th:nth-child(1),
    #athleteTable td:nth-child(1){
        min-width:60px!important;
        width:60px!important;
    }

    #athleteTable th:nth-child(2),
    #athleteTable td:nth-child(2){
        min-width:90px!important;
        width:90px!important;
    }

    #athleteTable th:nth-child(3),
    #athleteTable td:nth-child(3){
        min-width:300px!important;
        width:300px!important;
        white-space:normal!important;
    }

    #athleteTable th:nth-child(4),
    #athleteTable td:nth-child(4){
        min-width:110px!important;
        width:110px!important;
    }

    /* Watching box: không để nút Refresh đè chữ */
    .selectedBox{
        display:grid!important;
        grid-template-columns:minmax(0,1fr) auto!important;
        gap:10px!important;
        align-items:center!important;
        padding:14px!important;
        overflow:hidden!important;
    }

    .selectedBox h2{
        font-size:clamp(22px, 6vw, 36px)!important;
        line-height:1.18!important;
        word-break:break-word!important;
        margin:4px 0 0!important;
    }

    .selectedBox button{
        padding:10px 18px!important;
        min-width:110px!important;
        white-space:nowrap!important;
    }

    /* Team/Athlete title row: không để nút Sigma đè tiêu đề */
    .section-title-row{
        display:grid!important;
        grid-template-columns:minmax(0,1fr) auto!important;
        gap:10px!important;
        align-items:center!important;
    }

    .section-title-row h3{
        font-size:clamp(20px, 5.5vw, 30px)!important;
        line-height:1.18!important;
        word-break:break-word!important;
    }

    .section-title-row .btn-result{
        min-width:105px!important;
        white-space:nowrap!important;
    }

    /* Sigma Result: sửa header 3 cột bị đè */
    .sigma-title-row{
        display:grid!important;
        grid-template-columns:1fr!important;
        gap:12px!important;
        text-align:center!important;
    }

    .sigma-title-row > div:first-child{
        display:none!important;
    }

    .sigma-title-row .btn-result,
    #btnReturn{
        justify-self:center!important;
        min-width:130px!important;
        padding:11px 22px!important;
    }

    .sigma-title{
        font-size:clamp(34px, 10vw, 56px)!important;
        line-height:1.1!important;
        letter-spacing:4px!important;
    }

    #sigmaDivisionTitle{
        font-size:clamp(22px, 6.2vw, 34px)!important;
        line-height:1.2!important;
        word-break:break-word!important;
    }

    /* Cutoff/Sigma table: nằm gọn trong khung, kéo ngang khi cần */
    .sigma-bracket{
        width:100%!important;
        overflow:auto!important;
        -webkit-overflow-scrolling:touch;
    }

    .cutoff-result-wrap{
        display:grid!important;
        grid-template-columns:1fr!important;
        gap:14px!important;
        padding:8px!important;
        min-width:0!important;
    }

    .cutoff-phase-box{
        width:100%!important;
        max-width:100%!important;
        overflow:auto!important;
        border-radius:14px!important;
    }

    .cutoff-table{
        min-width:520px!important;
        width:max-content!important;
    }

    .cutoff-table th,
    .cutoff-table td{
        font-size:14px!important;
        padding:10px 8px!important;
        white-space:normal!important;
    }

    /* Medal standings giữ kéo ngang */
    #medalStandingsView{
        overflow:hidden!important;
    }

    #medalStandingsView .table-scroll{
        width:100%!important;
        max-width:100%!important;
        overflow:auto!important;
        -webkit-overflow-scrolling:touch;
    }

    #medalStandingsTable{
        min-width:max-content!important;
    }
}

/* Điện thoại nhỏ */
@media(max-width:480px){

    .content{
        padding:8px!important;
    }

    .tool-card,
    .card,
    .selectedBox,
    .sigma-view{
        border-radius:15px!important;
        padding:10px!important;
    }

    .ranking-division-title{
        font-size:18px!important;
    }

    .ranking-medal-table th,
    .ranking-medal-table td{
        font-size:13px!important;
        padding:9px 7px!important;
    }

    #athleteTable thead,
    #athleteTable tbody,
    #athleteTable tr{
        min-width:600px;
    }

    #athleteTable th,
    #athleteTable td{
        font-size:13px!important;
    }

    .selectedBox{
        grid-template-columns:1fr!important;
    }

    .selectedBox button{
        justify-self:end!important;
    }

    .section-title-row{
        grid-template-columns:1fr auto!important;
    }
}

.athlete-table-scroll{
    width:100%;
    max-width:100%;
    overflow:auto;
    -webkit-overflow-scrolling:touch;
}

@media(max-width:900px){
    .athlete-card #athleteTable{
        display:table!important;
        width:max-content!important;
        min-width:620px!important;
    }
}

/* ================= SIGMA PRINT / PDF BUTTONS ================= */

.sigma-action-box{
    display:flex;
    flex-direction:column;
    gap:8px;
    align-items:stretch;
    justify-content:center;
}

.sigma-action-btn{
    min-width:118px;
    padding:9px 14px!important;
    white-space:nowrap;
    font-size:13px!important;

    background:#123c67!important;
    color:#ffd34d!important;
    border:2px solid #ffd34d!important;
    border-radius:10px!important;

    display:flex;
    align-items:center;
    justify-content:center;
    gap:7px;

    font-weight:900!important;
    box-shadow:0 4px 12px rgba(0,0,0,.28);
}

.sigma-action-btn:hover{
    background:#ffd34d!important;
    color:#06111f!important;
    filter:none!important;
}

.sigma-action-btn:active{
    transform:translateY(1px);
}

.cutoff-phase-title-row{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    background:#123c67;
    padding:10px 12px;
}

.cutoff-phase-title-row .cutoff-phase-title{
    background:transparent!important;
    padding:0!important;
    flex:1;
}

.cutoff-phase-actions{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    justify-content:flex-end;
}

.cutoff-print-btn{
    width:34px;
    height:30px;
    padding:0!important;

    display:inline-flex;
    align-items:center;
    justify-content:center;

    border:2px solid #ffd34d!important;
    border-radius:9px;

    background:#123c67!important;
    color:#ffd34d!important;

    font-size:15px;
    font-weight:900;
    line-height:1;

    cursor:pointer;
    white-space:nowrap;

    box-shadow:0 3px 10px rgba(0,0,0,.28);
}

.cutoff-print-btn:hover{
    background:#ffd34d!important;
    color:#06111f!important;
    filter:none!important;
}

.cutoff-print-btn:active{
    transform:translateY(1px);
}

.cutoff-phase-actions{
    display:flex;
    gap:6px;
    flex-wrap:nowrap;
    justify-content:flex-end;
    align-items:center;
}

@media(max-width:900px){
    .sigma-title-row{
        grid-template-columns:1fr!important;
        gap:12px!important;
    }

    .sigma-title-row > div:first-child{
        display:none!important;
    }

    .sigma-action-box{
        flex-direction:row;
        flex-wrap:wrap;
        justify-content:center;
        align-items:center;
    }

    .sigma-action-btn{
        min-width:105px;
        font-size:12px!important;
        padding:9px 12px!important;
    }

    .cutoff-phase-title-row{
        flex-direction:column;
        align-items:center;
        text-align:center;
    }

    .cutoff-phase-actions{
        justify-content:center;
        gap:7px;
    }

    .cutoff-print-btn{
        width:36px;
        height:32px;
        font-size:15px;
    }
}

.sigma-action-icon{
    font-size:17px;
    line-height:1;
    display:inline-flex;
    align-items:center;
    justify-content:center;
}

#btnReturn.sigma-action-btn{
    background:#ffd34d!important;
    color:#06111f!important;
    border:2px solid #ffd34d!important;
}

#btnReturn.sigma-action-btn:hover{
    background:#ffdf70!important;
    color:#06111f!important;
}

/* ================= SIDEBAR PANEL IN SIGMA RESULT ================= */

.layout.result-mode.menu-open .sidebar{
    display:block!important;
    position:fixed!important;
    top:0!important;
    left:0!important;
    width:340px!important;
    max-width:86vw!important;
    height:100vh!important;
    z-index:9998!important;

    background:#081827!important;
    border-right:2px solid #ffd34d!important;
    padding:16px!important;
    overflow:auto!important;

    box-shadow:12px 0 30px rgba(0,0,0,.45)!important;
}

.layout.result-mode.menu-open .content{
    width:100%!important;
}

.layout.result-mode.menu-open ~ .sidebar-backdrop,
body.sidebar-open .sidebar-backdrop{
    display:block!important;
}

/* Nút 3 gạch luôn nổi trên Sigma */
.layout.result-mode ~ .sidebar-toggle-btn,
body .sidebar-toggle-btn{
    z-index:10000!important;
}

/* Vị trí nút khi đang ở Sigma Result */
.layout.result-mode .sidebar-toggle-btn{
    left:14px!important;
}

/* Vì nút nằm ngoài .layout nên dùng body/result-mode bằng class phụ */
body.sigma-result-mode .sidebar-toggle-btn{
    left:14px!important;
    top:204px!important;
}

body.sigma-result-mode.sidebar-open .sidebar-toggle-btn{
    left:calc(min(340px, 86vw) + 10px)!important;
}

/* Mobile trong Sigma */
@media(max-width:900px){
    body.sigma-result-mode .sidebar-toggle-btn{
        top:22px!important;
        left:10px!important;
    }

    body.sigma-result-mode.sidebar-open .sidebar-toggle-btn{
        left:calc(min(86vw, 360px) + 8px)!important;
    }

    .layout.result-mode.menu-open .sidebar{
        width:86vw!important;
        max-width:360px!important;
    }
}



  /* Kyeorugi Weigh-in: same color family as existing Sigma UI */
  .k-w-wrap{overflow:auto;max-height:calc(100vh - 350px);border:1px solid #264b6d;border-radius:10px}
  #kWeighTable{min-width:1170px;white-space:nowrap}
  #kWeighTable th{white-space:nowrap}
  #kWeighTable td{white-space:nowrap}
  .k-weight-input{width:112px;padding:8px;border:1px solid #35628c;border-radius:7px;background:#06192b;color:#fff;text-align:center;font-size:15px}
  .k-weight-input:disabled{background:#172537;color:#a0aec1;border-color:#24394e}
  .k-weight-input.saved{border-color:#4ade80;background:#143d31}
  .k-weight-input.pending{border-color:#ffd34d}
  /* Official weigh-in verdict: checked against the registered weight category. */
  #kWeighTable .k-weight-input.k-pass,
  #kWeighTable .k-weight-input.k-pass:disabled{
      background:#dcfce7!important;color:#166534!important;
      border:2px solid #86efac!important;font-weight:800!important;
      opacity:1!important;-webkit-text-fill-color:#166534!important;
  }
  #kWeighTable .k-weight-input.k-fail,
  #kWeighTable .k-weight-input.k-fail:disabled{
      background:#fee2e2!important;color:#991b1b!important;
      border:2px solid #fca5a5!important;font-weight:900!important;
      opacity:1!important;-webkit-text-fill-color:#991b1b!important;
  }
  #kWeighTable .k-weight-input:disabled:not(.k-pass):not(.k-fail){
      opacity:.65;cursor:not-allowed;
  }
  .k-status{font-size:13px;color:#9bb7cf;margin:9px 0}
  .k-login-panel{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:11002;width:min(94vw,450px);border:1px solid #365a78;padding:20px;border-radius:12px;background:#102b46;box-shadow:0 20px 70px #000a}
  .k-login-backdrop{position:fixed;inset:0;z-index:11001;background:#0009}
  .k-login-panel input{display:block;width:100%;margin:10px 0;padding:12px;border-radius:8px;border:1px solid #3b6281;background:#081827;color:white}
  .k-login-panel label{color:#bbd9f4;font-weight:bold}
  .k-topbar{display:flex;gap:8px;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  .k-session{color:#79e4a8;font-weight:bold}
  .k-division-cell{min-width:95px;white-space:nowrap;font-weight:700;color:#fcd34d;}

  /* Kyeorugi ONLY: wider Full name column, with left-aligned names.
     Existing Poomsae Team/Athlete List CSS is unaffected. */
  #athleteTable.kyeorugi-athletes th:nth-child(1),
  #athleteTable.kyeorugi-athletes td:nth-child(1){
      width:82px!important; min-width:82px!important; text-align:center!important;
  }
  #athleteTable.kyeorugi-athletes th:nth-child(2),
  #athleteTable.kyeorugi-athletes td:nth-child(2){
      width:82px!important; min-width:82px!important; text-align:center!important;
  }
  #athleteTable.kyeorugi-athletes th:nth-child(3),
  #athleteTable.kyeorugi-athletes td:nth-child(3){
      width:235px!important; min-width:185px!important; text-align:left!important;
  }
  #athleteTable.kyeorugi-athletes th:nth-child(4),
  #athleteTable.kyeorugi-athletes td:nth-child(4){
      width:auto!important; min-width:390px!important;
      text-align:left!important; white-space:normal!important;
  }
  #athleteTable.kyeorugi-athletes th:nth-child(5),
  #athleteTable.kyeorugi-athletes td:nth-child(5){
      width:135px!important; min-width:135px!important; text-align:left!important;
  }

  /* Poomsae athlete/team roster: 4 columns, grouped by competition No. */
  #athleteTable.poomsae-athletes th:nth-child(1),
  #athleteTable.poomsae-athletes td:nth-child(1){
      width:75px!important;min-width:75px!important;text-align:center!important;
  }
  #athleteTable.poomsae-athletes th:nth-child(2),
  #athleteTable.poomsae-athletes td:nth-child(2){
      width:95px!important;min-width:95px!important;text-align:center!important;
  }
  #athleteTable.poomsae-athletes th:nth-child(3),
  #athleteTable.poomsae-athletes td:nth-child(3){
      width:210px!important;min-width:150px!important;text-align:left!important;
      white-space:normal!important;
  }
  #athleteTable.poomsae-athletes th:nth-child(4),
  #athleteTable.poomsae-athletes td:nth-child(4){
      width:auto!important;min-width:300px!important;text-align:left!important;
      white-space:normal!important;overflow-wrap:anywhere!important;
  }
  
/* ===== Timelines inside current Event (single-file, read-only) ===== */
.timeline-tool{width:100%;min-width:0;max-width:none;background:#091725;border:1px solid #24527a;border-radius:13px;padding:9px;margin-bottom:12px}
.timeline-heading{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:6px 8px 10px;color:#f8faff}
.timeline-heading-title{font-size:clamp(18px,2vw,28px);font-weight:900;color:#ffe096}
.timeline-heading-title small{display:block;font-size:11px;letter-spacing:.08em;color:#b5d2e6}
.timeline-heading-meta{flex:1;text-align:center;color:#ffe096;font-weight:900;font-size:clamp(12px,1.2vw,17px)}
.timeline-clock{border:1.5px solid #d8b656;border-radius:12px;padding:7px 14px;text-align:center;white-space:nowrap;color:#ffe096;font-weight:900;font-size:clamp(12px,1.3vw,20px)}
.timeline-alert{font-size:13px;color:#ffd1d1;text-align:center;padding:2px 5px;min-height:18px}
.timeline-court-stack{display:flex;flex-direction:column;gap:6px;min-width:0}
.timeline-court-row{display:grid;grid-template-columns:3fr 2fr;gap:6px;height:clamp(142px,calc((100vh - 340px)/5),210px);min-height:142px}
.timeline-panel{min-width:0;border:1px solid #47647b;overflow:hidden;display:flex;flex-direction:column}
.timeline-panel-head{flex:0 0 auto;padding:5px 9px;font-weight:900;font-size:clamp(11px,1vw,16px);line-height:1.15;color:white;background:#135f89}
.timeline-current .timeline-panel-head{background:#864436}
.timeline-next-table{flex:1;min-height:0;display:grid;grid-template-rows:repeat(3,minmax(0,1fr))}
.timeline-next-line{min-height:0;display:grid;grid-template-columns:minmax(38px,7%) minmax(36px,7%) minmax(0,1.8fr) minmax(120px,1.15fr) minmax(0,1.8fr) minmax(36px,7%) minmax(38px,7%)}
.timeline-next-line>div{display:flex;align-items:center;min-width:0;overflow:hidden;padding:2px 4px;border:1px solid #153249;font-weight:800;font-size:clamp(10px,1.03vw,16px);line-height:1.12}
.timeline-next-line .tl-blue,.timeline-current-blue{background:#c6e5ff;color:#173451}
.timeline-next-line .tl-red,.timeline-current-red{background:#ffe1e2;color:#772735}
.timeline-next-line .tl-mid{background:#163349;color:#ffe096;justify-content:center;text-align:center;font-size:clamp(10px,.92vw,14px);white-space:normal;overflow-wrap:anywhere}
.timeline-next-line .tl-name{overflow-wrap:anywhere;word-break:normal}
.timeline-next-line .tl-red.tl-name{justify-content:flex-end;text-align:right}
.timeline-next-line .tl-wait{justify-content:center!important;text-align:center!important}
.timeline-flag{display:flex!important;justify-content:center!important;padding:1px!important}
.timeline-flag img{max-width:100%;max-height:35px;object-fit:contain}
.timeline-empty-flag{display:block}
.timeline-current-status{flex:0 0 auto;min-height:20px;background:#112b40;color:#ffe096;text-align:center;font-size:clamp(10px,.85vw,13px);font-weight:800;padding:3px 5px}
.timeline-current-sides{flex:1;display:grid;grid-template-columns:1fr 1fr;min-height:0}
.timeline-current-side{min-width:0;overflow:hidden;padding:5px 8px;display:flex;flex-direction:column;gap:2px}
.timeline-identity{display:flex;align-items:center;gap:8px;font-weight:900;font-size:clamp(13px,1.3vw,20px);min-height:27px}
.timeline-current-red .timeline-identity{justify-content:flex-end}
.timeline-current-side .timeline-flag img{max-width:52px;max-height:32px}
.timeline-athlete{font-size:clamp(12px,1.05vw,17px);font-weight:900;line-height:1.12;overflow-wrap:anywhere;min-height:1.2em}
.timeline-current-red .timeline-athlete{text-align:right}
.timeline-score-lines{margin-top:auto;display:grid;gap:2px}
.timeline-score-line{font-size:clamp(10px,1vw,15px);white-space:nowrap;overflow:hidden;text-overflow:clip;font-weight:850}
.timeline-current-red .timeline-score-line{text-align:right}
.timeline-score-line:last-child.timeline-total{background:#afd2f7;border-radius:4px;padding:2px}
.timeline-current-red .timeline-score-line:last-child.timeline-total{background:#f7bac9}
.timeline-soon{background:#112b40;border-radius:12px;min-height:220px;padding:40px 18px;text-align:center}
.timeline-soon h2{color:#ffe096;margin:0 0 14px}
.timeline-soon p{color:#bfd8e8}
.timeline-row-wave{animation:timeline-wave .45s ease both}
@keyframes timeline-wave{from{opacity:.15;transform:translateY(-14px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:1120px){.timeline-next-line{grid-template-columns:minmax(30px,7%) minmax(32px,7%) minmax(0,1.65fr) minmax(100px,1.1fr) minmax(0,1.65fr) minmax(32px,7%) minmax(30px,7%)}.timeline-court-row{height:158px}}
@media(max-width:850px){.timeline-court-row{grid-template-columns:1fr;height:auto;min-height:0}.timeline-panel{min-height:156px}.timeline-current{min-height:135px}.timeline-next-line>div{font-size:clamp(9px,1.4vw,13px)}.timeline-heading-meta{flex-basis:100%;order:3}}


/* Match Timeline V4 WEB: shared clock + viewport-locked five-court board. */
.rightBlank{display:flex;justify-content:flex-end;align-items:center;min-width:0}
.site-header-clock{width:clamp(120px,12vw,188px);padding:9px 8px;border:1.5px solid #e6be57;border-radius:11px;background:#17364c;color:#ffe096;text-align:center;font-weight:900;font-variant-numeric:tabular-nums;line-height:1.18}
#siteHeaderClockTime{font-size:clamp(16px,1.6vw,25px)}
#siteHeaderClockDate{font-size:clamp(12px,1.02vw,16px);margin-top:5px;color:white}
body.timeline-screen-open,body.timeline-screen-open html{overflow:hidden!important;height:100dvh}
body.timeline-screen-open .topbar{position:relative}
body.timeline-screen-open .layout.timeline-active{height:calc(100dvh - var(--timeline-topbar-height, 126px))!important;min-height:0!important;overflow:hidden!important}
body.timeline-screen-open .layout.timeline-active > .content{height:100%;min-height:0;overflow:hidden!important;padding:8px 12px!important;display:flex;flex-direction:column}
body.timeline-screen-open #timelineView{height:100%;min-height:0;margin:0;padding:6px 8px;display:flex;flex-direction:column;overflow:hidden}
body.timeline-screen-open #timelineMatchesContent{height:100%;flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden}
body.timeline-screen-open .timeline-court-stack{flex:1;min-height:0;display:grid;grid-template-rows:repeat(5,minmax(0,1fr));gap:5px;overflow:hidden}
body.timeline-screen-open .timeline-court-row{height:auto!important;min-height:0!important;grid-template-columns:minmax(0,3fr) minmax(0,2fr)!important;gap:5px}
body.timeline-screen-open .timeline-panel{min-height:0!important;min-width:0;overflow:hidden}
body.timeline-screen-open .timeline-next-table,body.timeline-screen-open .timeline-current-sides{min-height:0}
body.timeline-screen-open .timeline-next-line{min-height:0}
body.timeline-screen-open .timeline-next-line>div{min-height:0;padding:1px 3px;font-size:clamp(9px,.96vw,16px)}
body.timeline-screen-open .timeline-panel-head{padding:3px 8px;font-size:clamp(10px,.92vw,15px)}
body.timeline-screen-open .timeline-current-status{padding:1px 3px;min-height:0}
body.timeline-screen-open .timeline-current-side{padding:3px 6px}
body.timeline-screen-open .timeline-heading{position:relative;display:flex;align-items:center;justify-content:center;flex:none;min-height:58px;padding:1px 88px 4px}
body.timeline-screen-open .timeline-heading-center{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-width:0;text-align:center}
body.timeline-screen-open .timeline-heading-title{font-size:clamp(18px,1.7vw,27px);line-height:1.13}
body.timeline-screen-open .timeline-heading-meta{flex:none;font-size:clamp(12px,1.1vw,17px);line-height:1.13}
body.timeline-screen-open .timeline-refresh{position:absolute;right:5px;top:50%;transform:translateY(-50%);padding:8px 14px}
body.timeline-screen-open .timeline-alert{flex:none;min-height:0;padding:0;font-size:12px}
/* Page enters from the left, tilting softly like a page turn. The left panel leads. */
body.timeline-screen-open .timeline-row-wave{animation:none!important}
body.timeline-screen-open .timeline-row-wave > .timeline-panel{transform-origin:left center;animation:timeline-page-sweep .52s cubic-bezier(.22,.7,.22,1) both}
body.timeline-screen-open .timeline-row-wave > .timeline-panel.timeline-current{animation-delay:.11s}
@keyframes timeline-page-sweep{0%{opacity:.25;transform:perspective(1200px) rotateY(-6deg) translateX(-20px)}65%{opacity:.94;transform:perspective(1200px) rotateY(1deg) translateX(2px)}100%{opacity:1;transform:perspective(1200px) rotateY(0) translateX(0)}}
@media(max-width:900px){
 .rightBlank{display:flex!important}
 .site-header-clock{padding:5px;width:115px}
 #siteHeaderClockTime{font-size:14px}
 #siteHeaderClockDate{font-size:11px}
 body.timeline-screen-open .timeline-next-line{grid-template-columns:minmax(24px,6%) minmax(29px,7%) minmax(0,1.8fr) minmax(65px,1.15fr) minmax(0,1.8fr) minmax(29px,7%) minmax(24px,6%)}
 body.timeline-screen-open .timeline-next-line>div{font-size:9px}
 body.timeline-screen-open .timeline-refresh{padding:6px 8px;font-size:11px}
}
@media(max-width:550px){.topbar{grid-template-columns:60px minmax(0,1fr) 90px!important}.logoBox img{max-width:54px!important;max-height:55px!important}.site-header-clock{width:87px!important}#siteHeaderClockTime{font-size:12px}#siteHeaderClockDate{font-size:10px}}
@media(prefers-reduced-motion:reduce){body.timeline-screen-open .timeline-row-wave > .timeline-panel{animation:none!important}}


/* V4: adaptive text fits within its own timeline cell; no column-size shifts. */
.timeline-next-line .tl-fit-text,
.timeline-current-side .tl-fit-text{display:block;width:100%;max-width:100%;min-width:0;line-height:1.07;overflow-wrap:anywhere;word-break:normal}
/* Center NOC codes for both CHEONG and HONG in NEXT MATCHES. */
.timeline-next-line .tl-noc{justify-content:center!important;text-align:center!important}
.timeline-next-line .tl-noc .tl-fit-text{text-align:center!important}
.timeline-next-line .tl-mid .tl-fit-text{text-align:center}
.timeline-next-line .tl-name.tl-wait .tl-fit-text{text-align:center}
.timeline-next-line .tl-red.tl-name .tl-fit-text{text-align:right}
.timeline-next-line .tl-blue.tl-name .tl-fit-text{text-align:left}
.timeline-current-side .timeline-score-line .tl-fit-text{white-space:nowrap}
/* Accordion selection remains visible inside the sidebar, not the document. */
.sidebar .groupHead:focus-visible,.sidebar .subHead:focus-visible,
.sidebar .genderHead:focus-visible,.sidebar .ageBtn:focus-visible{outline:2px solid #ffd34d;outline-offset:-2px}


/* Referee-specific presentation uses Sigma header/sidebar/footer palette */
body{min-height:100vh;display:flex;flex-direction:column;background-image:linear-gradient(#07111fd9,#07111fe8),url('/referee-background');background-size:cover;background-position:center;background-attachment:fixed}
main.layout{flex:1;min-height:calc(100vh - 176px)}.ref-section{max-width:1500px;margin:auto}.ref-tab-btn{white-space:normal}
.ref-table-scroll{max-height:calc(100vh - 370px);overflow:auto;border:1px solid #27547b;border-radius:12px}
.ref-footer{margin-top:10px;text-align:center;color:#b9d1e8;font-size:13px;padding:15px;background:linear-gradient(180deg,#081b30,#061323);border-top:1px solid #315879;line-height:1.0}
.footer-name{font-size:17px;font-weight:900;color:#ffd34d;letter-spacing:.7px}.footer-quote{font-size:14px;color:#d9eaff;margin:6px auto;max-width:740px}.footer-contact{margin-top:7px}.footer-contact a{color:#83d7ff;text-decoration:none;font-weight:700}.footer-bottom{margin-top:9px;color:#829fb9;font-size:11px}
.ref-status{color:#9bcce7;padding:12px}.ref-search{width:100%;padding:12px;border-radius:10px;border:1px solid #427294;background:#06192b;color:#fff}
@media(max-width:900px){.sidebar{position:static;left:auto;width:auto;max-width:none;height:auto;border:0;box-shadow:none}.layout{display:block!important}.content{width:100%}.topbar{grid-template-columns:72px minmax(0,1fr) 115px}.ref-table-scroll{max-height:65vh}}

.ref-profile-box{border:1px solid #d8b656;border-radius:15px;padding:18px;margin:0 0 14px;background:linear-gradient(105deg,#123d67,#0b2139);box-shadow:0 9px 22px #0006}
.ref-profile-title{font-size:20px;color:#ffd34d;font-weight:900;margin-bottom:12px}
.ref-profile-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.ref-profile-grid>div{background:#081b30;border:1px solid #264a6b;border-radius:9px;padding:10px 13px;overflow-wrap:anywhere}
.ref-profile-grid small{display:block;font-size:11px;color:#9fc0d7;text-transform:uppercase;margin-bottom:5px}
.ref-profile-grid strong{font-size:15px;color:#fff}
.ref-roster-scroll{max-height:calc(100vh - 420px)}
.ref-roster-table{width:100%;min-width:1020px;border-collapse:separate;border-spacing:0;table-layout:fixed}
.ref-roster-table th,.ref-roster-table td{padding:10px 8px;border-right:1px solid #254a69;border-bottom:1px solid #254a69;vertical-align:middle}
.ref-roster-table th{background:#123c67;color:#ffd34d;text-align:center;position:sticky;top:0;z-index:2}
.ref-roster-table td:nth-child(4n+1){width:6%;text-align:center;color:#ffd34d;font-weight:900}
.ref-roster-table td:nth-child(4n+2){width:8%;text-align:center}
.ref-roster-table td:nth-child(4n+3){width:8%;text-align:center;color:#9ee0ff;font-weight:800}
.ref-roster-table td:nth-child(4n+4){width:26%;font-weight:700}
.ref-roster-table td.empty-roster{background:rgba(255,255,255,.015)}
.ref-flag{width:28px;height:19px;object-fit:cover;border:1px solid #ced8e0;vertical-align:middle}
@media(max-width:800px){.ref-profile-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ref-roster-scroll{max-height:60vh}}
@media(max-width:440px){.ref-profile-grid{grid-template-columns:1fr}}

/* ===== Referee V3: viewport-locked header/footer, center-only scrolling ===== */
html{height:100%;overflow:hidden!important}
body{height:100vh;height:100dvh;min-height:0!important;overflow:hidden!important;display:flex;flex-direction:column}
body>.topbar{flex:0 0 auto;position:relative;z-index:3}
body>main.layout{flex:1 1 auto!important;min-height:0!important;height:auto!important;overflow:hidden!important;display:grid;grid-template-columns:272px minmax(0,1fr)}
body>main.layout>.sidebar{min-height:0;overflow-y:auto;overscroll-behavior:contain}
body>main.layout>.content{min-height:0!important;min-width:0;overflow-y:auto!important;overflow-x:hidden!important;overscroll-behavior:contain;scrollbar-gutter:stable;padding:16px}
body>.ref-footer{flex:0 0 auto!important;margin-top:0!important;position:relative;z-index:3;padding:9px 12px;font-size:11px;line-height:1.12}
.ref-footer .footer-name{font-size:15px}.ref-footer .footer-quote{font-size:12px;margin:3px auto}.ref-footer .footer-contact{margin-top:3px}.ref-footer .footer-bottom{margin-top:3px;font-size:10px}
.ref-section{max-width:none!important;width:100%;margin:0 0 16px!important}
/* Referee identity card */
.ref-profile-box{padding:17px 18px 19px;margin-bottom:17px;border-radius:17px;border:1px solid #e5bc48;background:linear-gradient(115deg,#143b62,#0b223c 60%,#081b31);box-shadow:0 10px 30px #0005}
.ref-profile-title{font-size:clamp(19px,2.2vw,26px);color:#ffd34d;font-weight:900;margin-bottom:15px;line-height:1.25;overflow-wrap:anywhere}
.ref-profile-title .ref-profile-fullname{color:#fff}
.ref-profile-layout{display:grid;grid-template-columns:minmax(210px,29%) minmax(0,1fr);gap:15px;align-items:stretch}
.ref-profile-country,.ref-profile-details{border:1px solid #356187;border-radius:14px;background:rgba(4,19,36,.72);padding:16px;min-width:0}
.ref-profile-country{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;min-height:185px}
.ref-country-label{color:#8eccef;font-weight:900;font-size:12px;letter-spacing:2px}
.ref-country-code{color:#fff;font-size:clamp(25px,3.2vw,41px);font-weight:900;line-height:1}
.ref-country-flag-frame{display:flex;align-items:center;justify-content:center;width:min(100%,235px);height:115px;background:#061426;border:1px solid #456f92;border-radius:11px;padding:9px;box-shadow:inset 0 0 22px #0006}
.ref-profile-big-flag{display:block;max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;box-shadow:0 5px 15px #0008;border:1px solid #fff5}
.ref-profile-flag-unknown{font-size:13px;color:#b7cada;text-align:center}
.ref-profile-details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ref-detail-field{min-width:0;padding:13px 15px;border-radius:10px;border:1px solid #274c6c;background:#102e4b;display:flex;flex-direction:column;justify-content:center;gap:7px}
.ref-detail-label{font-size:12px;font-weight:800;color:#91c8ed;letter-spacing:.4px;text-transform:uppercase}
.ref-detail-value{font-size:clamp(17px,1.9vw,23px);font-weight:900;overflow-wrap:anywhere;color:#fff}
/* Three separate referee roster tables with visible gaps */
.ref-roster-groups{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:start;min-width:0}
.ref-roster-card{min-width:0;border:1px solid #326589;border-radius:13px;overflow:hidden;background:#0a2138;box-shadow:0 5px 16px #0004}
.ref-roster-card table{width:100%;min-width:0!important;border-collapse:collapse;table-layout:fixed}
.ref-roster-card th,.ref-roster-card td{padding:10px 6px;border-right:0;border-bottom:1px solid #244965;vertical-align:middle}
.ref-roster-card th{position:sticky;top:0;background:#164a77;color:#ffd34d;font-size:12px;text-align:center;z-index:1}
.ref-roster-card td{font-size:13px;font-weight:750}
.ref-roster-card th:nth-child(1),.ref-roster-card td:nth-child(1){width:11%;text-align:center;color:#ffdf72;font-weight:900}
.ref-roster-card th:nth-child(2),.ref-roster-card td:nth-child(2){width:19%;text-align:center}
.ref-roster-card th:nth-child(3),.ref-roster-card td:nth-child(3){width:17%;text-align:center;color:#a2dfff;font-weight:900}
.ref-roster-card th:nth-child(4),.ref-roster-card td:nth-child(4){width:53%;text-align:left;overflow-wrap:anywhere;word-break:normal}
.ref-roster-card .ref-flag{width:43px;height:29px;max-width:100%;object-fit:contain;background:#ffffff08}
.ref-roster-card tr:last-child td{border-bottom:0}
@media(max-width:850px){.ref-roster-groups{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:770px){.ref-roster-groups{grid-template-columns:1fr}.ref-profile-layout{grid-template-columns:1fr}.ref-profile-country{min-height:145px}.ref-profile-details{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:450px){.ref-profile-details{grid-template-columns:1fr}.ref-profile-title{font-size:20px}}
@media(max-width:900px){
 body>main.layout{display:grid!important;grid-template-columns:minmax(0,1fr)!important;grid-template-rows:auto minmax(0,1fr)!important}
 body>main.layout>.sidebar{grid-column:1!important;grid-row:1;position:static!important;width:100%!important;max-width:none!important;height:auto!important;max-height:175px;padding:10px!important;border-right:0;border-bottom:1px solid #21415f}
 body>main.layout>.content{grid-column:1!important;grid-row:2;overflow-y:auto!important;padding:11px!important}
 .sidebar .quickMenu{margin-bottom:8px;padding-bottom:6px}.sidebar .quickMenuTitle{font-size:14px;margin:3px 0 6px}.sidebar .quickMenuBtn{display:inline-block;width:auto;font-size:12px;padding:8px 10px;margin:0 5px 5px 0}
}
/* Referee V4: name and refresh share one line; country card shows code + flag only. */
.ref-profile-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;min-width:0}
.ref-profile-heading .ref-profile-title{margin:0;min-width:0;overflow-wrap:anywhere}
.ref-profile-refresh{flex:none;white-space:nowrap}
.ref-profile-country{gap:10px}
@media(max-width:520px){.ref-profile-heading{align-items:flex-start;flex-wrap:wrap}.ref-profile-refresh{margin-left:auto}}
/* V6: Weigh-in summary and Refresh on one line, independently scrollable body. */
.k-summary-row{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:8px 0 12px;min-width:0}
.k-summary-row .k-status{flex:1;min-width:0;margin:0;padding:2px 0;color:#d7efff;font-size:clamp(15px,1.45vw,20px);line-height:1.3;font-weight:900;overflow-wrap:anywhere}
.k-summary-row #kReloadBtn{flex:none;margin-left:auto;white-space:nowrap;padding:10px 20px}
@media(max-width:520px){.k-summary-row{align-items:center;gap:8px}.k-summary-row .k-status{font-size:14px}.k-summary-row #kReloadBtn{padding:9px 11px}}


/* Division Timelines: public read-only Excel-inspired ten-court board */
.division-timeline-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.division-timeline-heading h3{font-size:23px;color:#ffd34d;margin:0 0 4px}
.division-timeline-caption{font-size:13px;color:#a6c3da}
.division-day-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.division-day-tab{cursor:pointer;background:#123c67;color:#fff;border:1px solid #315e80;border-radius:9px;padding:10px 18px;font-weight:900;font-size:14px}
.division-day-tab.active{background:#ffd34d;color:#102037;border-color:#ffd34d}
.division-timeline-status{font-size:13px;color:#a9d7f6;margin:7px 0 11px;min-height:19px}
.division-board-scroll{overflow:auto;max-height:calc(100dvh - 350px);border:1px solid #47647b;border-radius:12px;background:#f4f7fa;color:#162738;overscroll-behavior:contain}
.division-board{min-width:1420px;font-family:Arial,Helvetica,sans-serif;font-size:13px}
.division-courts{display:grid;grid-template-columns:125px repeat(10,minmax(125px,1fr));position:sticky;top:0;z-index:5}
.division-courts>div{background:#ffe6a0;color:#142536;font-weight:900;text-align:center;padding:11px 4px;border-right:1px solid #39434e;border-bottom:2px solid #39434e}
.division-session{display:grid;grid-template-columns:125px repeat(10,minmax(125px,1fr));position:relative}
.division-session-label{grid-column:1;grid-row:1/-1;z-index:2;background:#edf2f7;border-right:1px solid #566472;display:flex;justify-content:center;align-items:center;font-weight:900;color:#22354a;min-height:100%;border-bottom:1px solid #697887}
.division-cell{min-height:68px;border-right:1px solid #8897a5;border-bottom:1px solid #8897a5;background:#fff}
.division-slot-time{grid-column:1;z-index:1;background:#f9fafc;border-right:1px solid #8897a5;border-bottom:1px solid #8897a5;display:flex;align-items:center;justify-content:center;color:#43596b;font-size:12px}
.division-block{z-index:3;border:1px solid #3d607f;background:#dbecfa;color:#082b46;display:flex;align-items:center;justify-content:center;padding:7px;text-align:center;font-weight:800;white-space:pre-line;overflow-wrap:anywhere;box-shadow:inset 0 0 0 1px #fff8}
.division-lunch{padding:10px;background:#d7dce2;border-block:1px solid #8e969f;text-align:center;font-size:14px;letter-spacing:.4px;font-weight:900;color:#243748}
.division-empty{padding:16px;background:#eaf1f7;color:#40556a;text-align:center}
@media(max-width:800px){.division-board-scroll{max-height:calc(100dvh - 355px)}.division-day-tab{padding:8px 12px}}

/* Division Timeline Admin + responsive ten-court scheduling */
.division-day-add{background:#163d62;color:#ffe187;border:2px dashed #d8b656;font-size:22px;min-width:47px}
.division-day-wrap{position:relative;display:inline-flex}
.division-day-remove{position:absolute;right:-6px;top:-7px;border:1px solid #f4a5a5;border-radius:100%;background:#921b30;color:white;width:21px;height:21px;cursor:pointer;font-weight:bold}
.division-admin-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0;padding:9px;border:1px solid #4a6e89;border-radius:10px;background:#0b253e}
.division-admin-tools button,.dt-actions button{border:1px solid #e6bf52;border-radius:8px;padding:8px 12px;background:#153f68;color:#ffe18a;font-weight:800;cursor:pointer}
.division-admin-tools button:nth-child(4),.dt-actions button:last-child{background:#ffd34d;color:#09243c}
#dtAdminHint{color:#c9d9e6;font-size:12px}
.dt-dialog{position:fixed;inset:0;background:#000b;z-index:12000;display:flex;align-items:center;justify-content:center;padding:12px}
.dt-panel{background:#102b46;border:1px solid #f5c542;border-radius:14px;padding:22px;width:min(620px,96vw);box-shadow:0 20px 70px #0009}
.dt-panel h3{color:#ffe18a;margin:0 0 15px}.dt-panel label{display:block;color:#c9e4f5;font-size:13px;font-weight:bold;margin:9px 0 4px}
.dt-panel input,.dt-panel select{display:block;width:100%;padding:11px;background:#06192b;color:white;border:1px solid #426b88;border-radius:8px}
.dt-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
.dt-admin-cell{cursor:pointer!important;outline:1px dashed #93bcd6;outline-offset:-3px}
.dt-admin-cell:hover{background:#fff0b8!important}.dt-slot-remove{border:1px solid #d67f7f;border-radius:5px;background:#9d2630;color:white;cursor:pointer;margin-left:5px}
.dt-managed-row{display:grid;grid-template-columns:125px repeat(10,minmax(125px,1fr));min-height:74px}
.dt-managed-row>div{border-right:1px solid #9caebc;border-bottom:1px solid #9caebc;padding:8px 5px;overflow-wrap:anywhere;display:flex;align-items:center;justify-content:center;text-align:center}
.dt-managed-row .dt-time{background:#e7edf4;display:block;color:#153752;font-weight:bold;padding-top:12px}
.dt-managed-row .dt-booked{background:#d8ecfa;color:#12334d;font-weight:800}
.dt-managed-row .dt-empty{background:#fff;color:#a1acb8}
.dt-managed-row .dt-booked small{display:block;font-weight:600;color:#385a73;margin-top:4px}

/* V3: multiple rounds in one court slot */
.dt-cell-item{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#123956;border:1px solid #426b88;border-radius:7px;padding:8px 9px;margin:5px 0;color:#eaf6ff}
.dt-cell-item button{background:#a82d39;color:#fff;border:0;border-radius:5px;padding:3px 10px;cursor:pointer;font-weight:bold}
.dt-cell-item small{color:#afd1e7}.dt-cell-empty{color:#b4c6d6;font-size:13px;padding:10px 0}
.dt-cell-controls{display:flex;justify-content:flex-end;margin-top:8px}
.dt-cell-controls button{background:#ffcf4b;border:0;border-radius:8px;color:#132b43;padding:9px 14px;cursor:pointer;font-weight:800}
.dt-cell-controls button:disabled{opacity:.4;cursor:default}
.dt-entries{width:100%;display:flex;flex-direction:column;align-items:stretch;gap:3px}
.dt-div-entry{padding:5px 3px;border-bottom:1px solid #8fb5cf;line-height:1.2}
.dt-div-entry:last-child{border-bottom:0}
.dt-div-entry strong{display:block;font-size:14px;font-weight:900;letter-spacing:.1px;color:#062b46}.dt-div-entry small{display:block;font-size:11px;color:#385a73}
.dt-add-more{display:inline-block;color:#0e5a85;font-size:18px;font-weight:bold;margin:2px}
/* Division timeline V2: responsive columns per-day and distinct full-width breaks */
.division-board.dt-dynamic-board{min-width:0;width:100%}
.dt-dynamic-board .division-courts,.dt-dynamic-board .dt-managed-row{
 grid-template-columns:100px repeat(var(--dt-courts,1),minmax(105px,1fr));
 min-width:max(100%,calc(100px + var(--dt-courts,1)*105px));
}
.dt-managed-row.dt-break-row{display:block!important;min-height:0!important}
.dt-break-row .dt-break-band{min-height:39px;display:flex;justify-content:center;align-items:center;gap:10px;
 border-bottom:1px solid #6d7884;padding:8px 36px;position:relative;font-weight:900;letter-spacing:.4px;text-align:center}
.dt-break-band.dt-lunch{background:#e6d3a7;color:#68480a;border-color:#b69547}
.dt-break-band.dt-rest{background:#c9e9e1;color:#0b5d50;border-color:#63a99a}
.dt-break-band .dt-break-edit{border:1px solid currentColor;border-radius:6px;padding:3px 9px;background:#ffffffa8;color:inherit;cursor:pointer;font-weight:800}
.dt-break-band .dt-break-remove{position:absolute;right:8px;top:6px;background:#962b38;color:#fff;border:0;border-radius:5px;cursor:pointer;padding:3px 8px}
.dt-lunch-placeholder{background:#e7dce0;color:#56677b;padding:9px 12px;text-align:center;font-size:12px;font-weight:700}
.dt-lunch-placeholder button{margin-left:8px;border:1px solid #bc9762;background:#fff6dd;color:#68480a;border-radius:5px;padding:3px 8px;cursor:pointer}
.dt-panel .dt-time-note{font-size:12px;color:#bcd9e8;line-height:1.5;margin-top:6px}
@media(max-width:700px){.dt-dynamic-board .division-courts,.dt-dynamic-board .dt-managed-row{grid-template-columns:95px repeat(var(--dt-courts,1),minmax(110px,1fr))}}

/* Vietnamese header typography: crisp combining marks on Windows/Chrome. */
.topbar .eventTitle,
.topbar .eventTitle h1,
.topbar .event-meta,
.topbar .header-date,
.topbar .header-location {
  font-family: "Segoe UI", "Arial", sans-serif !important;
  font-kerning: normal;
  font-synthesis: none;
  letter-spacing: 0 !important;
  -webkit-font-smoothing: auto;
  text-rendering: optimizeLegibility;
}
.topbar .eventTitle { overflow: visible !important; }
.topbar .header-title {
  font-size: clamp(22px, 2.4vw, 42px) !important;
  font-weight: 800 !important;
  line-height: 1.32 !important;
  white-space: normal !important;
  overflow: visible !important;
  overflow-wrap: normal !important;
  word-break: normal !important;
  text-shadow: 0 2px 2px rgba(0, 24, 55, .75) !important;
}
.topbar .header-date,
.topbar .header-location {
  font-weight: 700 !important;
  line-height: 1.42 !important;
  text-shadow: none !important;
  white-space: normal;
}
.topbar .event-meta-row { min-height: 27px; line-height: 1.42; }
@media (max-width: 900px) {
  .topbar .header-title { font-size: clamp(17px, 4.1vw, 30px) !important; line-height: 1.32 !important; }
  .topbar .header-date, .topbar .header-location { font-size: clamp(11px, 2.4vw, 15px) !important; }
}
@media (max-width: 430px) {
  .topbar .header-title { font-size: clamp(18px, 5.2vw, 24px) !important; }
}


/* ===== REFEREE WEIGH-IN: contained scrolling + pinned identity columns ===== */
body>main.layout{grid-template-columns:272px minmax(0,1fr)!important;min-width:0}
body>main.layout>.content{min-width:0!important;overflow-x:hidden!important}
#kWeighView:not(.hidden){display:flex;flex-direction:column;min-width:0;max-width:100%;height:100%;min-height:0;margin:0!important;overflow:hidden}
#kWeighView .k-topbar,#kWeighView .filter-grid,#kWeighView .k-summary-row{flex:0 0 auto}
#kWeighView .k-w-wrap{flex:1 1 auto;min-height:0;max-height:none!important;width:100%;max-width:100%;overflow:auto!important;overscroll-behavior:contain;scrollbar-gutter:stable;cursor:grab;position:relative;-webkit-overflow-scrolling:touch}
#kWeighView .k-w-wrap.k-dragging{cursor:grabbing;user-select:none}
#kWeighTable{width:max-content!important;min-width:1400px!important;border-collapse:separate!important;border-spacing:0!important;table-layout:fixed}
#kWeighTable th,#kWeighTable td{box-sizing:border-box;white-space:nowrap!important;height:46px;border-bottom:1px solid #24405b}
#kWeighTable th{position:sticky;top:0;z-index:4;background:#163c63}
#kWeighTable th:nth-child(1),#kWeighTable td:nth-child(1){width:150px;min-width:150px;max-width:150px;position:sticky!important;left:0!important;text-align:left;z-index:5;background:#102b48;box-shadow:inset -1px 0 #315875}
#kWeighTable th:nth-child(2),#kWeighTable td:nth-child(2){width:225px;min-width:225px;max-width:225px;position:sticky!important;left:150px!important;text-align:left;z-index:5;background:#102b48;box-shadow:3px 0 6px #0005,inset -1px 0 #315875}
#kWeighTable th:nth-child(1),#kWeighTable th:nth-child(2){z-index:9!important;background:#163c63}
#kWeighTable tbody tr:hover td:nth-child(-n+2){background:#1b4166}
#kWeighTable th:nth-child(3),#kWeighTable td:nth-child(3){min-width:115px}
#kWeighTable th:nth-child(4),#kWeighTable td:nth-child(4){min-width:95px}
#kWeighTable th:nth-child(5),#kWeighTable td:nth-child(5){min-width:72px}
#kWeighTable th:nth-child(6),#kWeighTable td:nth-child(6){min-width:235px}
#kWeighTable th:nth-child(7),#kWeighTable td:nth-child(7){min-width:120px}
#kWeighTable th:nth-child(n+8),#kWeighTable td:nth-child(n+8){min-width:150px;text-align:center}
#kWeighTable .k-weight-input{cursor:text}
/* Sidebar always visible; collapsed state becomes an icon rail, not display:none. */
.ref-sidebar-bar{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.ref-menu-toggle{width:44px;height:42px;flex:none;border:1px solid #e7c354;border-radius:9px;background:#153e63;color:#ffe19b;font-size:25px;font-weight:bold;cursor:pointer}
.ref-menu-toggle:hover{background:#ffd34d;color:#0a2035}
.ref-sidebar-brand{font-weight:900;color:#ffe19b;letter-spacing:.8px}
.sidebar .quickMenuBtn{display:flex!important;align-items:center;gap:12px;min-width:0}
.ref-menu-icon{display:inline-flex;flex:none;width:25px;justify-content:center;align-items:center;font-size:22px;line-height:1.1;color:inherit}
.ref-menu-label{min-width:0;white-space:normal}
body.ref-menu-compact>main.layout{grid-template-columns:76px minmax(0,1fr)!important}
body.ref-menu-compact>main.layout>.sidebar{width:76px!important;min-width:76px!important;padding:12px 9px!important;overflow-x:hidden}
body.ref-menu-compact .ref-sidebar-brand,body.ref-menu-compact .sidebar .quickMenuTitle,body.ref-menu-compact .ref-menu-label{display:none!important}
body.ref-menu-compact .sidebar .quickMenuBtn{width:56px!important;min-height:51px;padding:8px 5px!important;margin-bottom:8px!important;justify-content:center!important}
body.ref-menu-compact .ref-menu-icon{width:auto;font-size:24px}
body.ref-menu-compact .ref-menu-toggle{width:56px}
@media(max-width:900px){
 body>main.layout,body.ref-menu-compact>main.layout{display:grid!important;grid-template-columns:272px minmax(0,1fr)!important;grid-template-rows:minmax(0,1fr)!important}
 body.ref-menu-compact>main.layout{grid-template-columns:76px minmax(0,1fr)!important}
 body>main.layout>.sidebar{grid-column:1!important;grid-row:1!important;position:relative!important;width:auto!important;height:auto!important;max-height:none!important;border-right:1px solid #21415f;padding:12px!important;overflow-y:auto!important}
 body>main.layout>.content{grid-column:2!important;grid-row:1!important;min-width:0!important;overflow-y:auto!important}
 body.ref-menu-compact>main.layout>.sidebar{width:76px!important}
}

</style></head><body>
<header class="topbar"><div class="logoBox"><img src="/referee-logo" alt="NTP" onerror="this.style.display='none'"/></div>
<div class="eventTitle"><h1 class="header-title">{{ event_name }}</h1><div class="event-meta">
{% if event_date %}<div class="event-meta-row"><span class="event-meta-label">Dates</span><span class="header-date">{{ event_date }}</span></div>{% endif %}
{% if event_location %}<div class="event-meta-row venue-row"><span class="event-meta-label">Venue</span><span class="header-location">{{ event_location }}</span></div>{% endif %}
</div></div><div class="rightBlank"><div class="site-header-clock"><div id="siteHeaderClockTime"></div><div id="siteHeaderClockDate"></div></div></div></header>
<main class="layout"><aside class="sidebar" id="refSidebar"><div class="ref-sidebar-bar"><button type="button" id="refMenuToggle" class="ref-menu-toggle" onclick="refToggleMenu()" aria-label="Thu gọn menu" aria-expanded="true" title="Thu gọn / mở menu">☰</button><span class="ref-sidebar-brand">MENU</span></div><div class="quickMenu"><div class="quickMenuTitle">REFEREE PORTAL</div>
<button id="tabRefList" class="quickMenuBtn active" title="Referee Information" onclick="showRefereeTab('referee-list')"><span class="ref-menu-icon">♙</span><span class="ref-menu-label">Referee Information</span></button>
<button id="tabMission" class="quickMenuBtn" title="Mission" onclick="showRefereeTab('mission')"><span class="ref-menu-icon">▤</span><span class="ref-menu-label">Mission</span></button>
<button id="btnKyeorugiWeighin" class="quickMenuBtn" type="button" title="Weigh-in"><span class="ref-menu-icon">⚖</span><span class="ref-menu-label">Weigh-in</span></button>
<button id="tabPoomsae" class="quickMenuBtn" title="Poomsae Score" onclick="openPoomsaeScore()"><span class="ref-menu-icon">★</span><span class="ref-menu-label">Poomsae Score</span></button>
<button id="tabDivisionTimeline" class="quickMenuBtn" type="button" title="Division Timelines" onclick="showRefereeTab('division-timelines')"><span class="ref-menu-icon">▦</span><span class="ref-menu-label">Division Timelines</span></button>
</div><div class="quickMenu"><div class="quickMenuTitle">EVENT</div><a class="quickMenuBtn" href="/" title="Select Event" style="text-decoration:none"><span class="ref-menu-icon">⌂</span><span class="ref-menu-label">Select Event</span></a></div></aside>
<section class="content">
<section id="refListView" class="tool-card ref-section">
<div id="refereeProfile" class="ref-profile-box hidden"></div>
<div class="ref-status" id="refListStatus">Nhấn Referee Information và nhập ID.Referee để xem thông tin.</div>
<div id="refRosterGroups" class="ref-roster-groups" aria-label="Referee roster divided into three tables"></div></section>
<section id="divisionTimelineView" class="tool-card ref-section hidden">
 <div class="division-timeline-heading"><div><h3>Division Timelines</h3><div class="division-timeline-caption">Competition order by day and court</div></div><button class="btn-result" type="button" onclick="openTimelineAdmin()" id="divisionAdminBtn">Admin Login</button></div>
 <div id="divisionDayTabs" class="division-day-tabs" role="tablist" aria-label="Competition days"></div>
 <div id="divisionAdminTools" class="division-admin-tools hidden"><button type="button" onclick="dtAddSlot()">+ Time Slot</button><button type="button" onclick="dtSetLunch()">Lunch Time</button><button type="button" onclick="dtAddBreak()">+ Break Time</button><button type="button" onclick="dtSave()">Apply &amp; Save</button><button type="button" onclick="dtLogout()">Logout</button><span id="dtAdminHint">Click a Court cell to select Round / Division</span></div>
 <div id="divisionTimelineStatus" class="division-timeline-status" role="status"></div>
 <div class="division-board-scroll"><div id="divisionTimelineBoard" class="division-board"></div></div>
</section>
<section id="missionView" class="tool-card ref-section hidden"><div class="tool-title-row"><h3>Mission — Referee Assignment</h3><button class="btn-result" onclick="loadMission()">Refresh</button></div><div class="ref-status" id="missionStatus"></div><div class="ref-table-scroll"><table id="missionTable"></table></div></section>
<section id="kWeighView" class="tool-card ref-section hidden"><div class="k-topbar"><h3>Kyeorugi — Official Weigh-in</h3><span id="kRefereeLabel" class="k-session"></span></div>
<div class="filter-grid"><select id="kCountryFilter"><option value="">All Units / Country</option></select><select id="kAgeFilter" aria-label="Age"><option value="">All Age</option><option value="U9">U9</option><option value="U12">U12</option><option value="Cadet (U14)">Cadet (U14)</option><option value="Junior (U17)">Junior (U17)</option><option value="Senior (O18)">Senior (O18)</option></select><select id="kGenderFilter"><option value="">All Gender</option><option>Male</option><option>Female</option></select><select id="kWeightFilter"><option value="">All Weights</option></select><input id="kSearchFilter" placeholder="GAL/ID, Full name, NOC..."/></div>
<div class="k-summary-row"><div id="kWeighStatus" class="k-status" aria-live="polite"></div><button id="kReloadBtn" class="btn-result" type="button">Refresh</button></div><div class="k-w-wrap"><table id="kWeighTable"></table></div></section>

</section></main><footer class="ref-footer" role="contentinfo"><div class="footer-name">Taekwondo Event &amp; Competition Management Platform</div><div class="footer-quote">Connecting Passion · Honoring Sportsmanship · Shaping Taekwondo’s Future</div><div>Created and Designed by <strong style="color:#fff">Nguyễn Thiên Phụng</strong></div><div class="footer-contact">Contact: <a href="mailto:nhoctotokute93@gmail.com">nhoctotokute93@gmail.com</a></div></footer>
<div id="kLoginBackdrop" class="k-login-backdrop hidden"></div><div id="kLoginBox" class="k-login-panel hidden" role="dialog" aria-modal="true"><h3 style="color:#ffd34d">Referee Authorization</h3>
<label>ID Referee</label><input id="kRefId" autocomplete="username" maxlength="100"/>
<button class="btn-result" type="button" id="kLoginBtn">Verify / Enter</button> <button class="btn-result" type="button" id="kLoginClose">Cancel</button><div id="kLoginStatus" class="k-status"></div></div>
<!-- Division Timeline Admin: dialog required by dtDialog() -->
<div id="dtDialog" class="dt-dialog hidden" role="dialog" aria-modal="true" aria-labelledby="dtDialogTitle">
  <div class="dt-panel">
    <h3 id="dtDialogTitle">Division Timeline — Admin</h3>
    <div id="dtDialogContents"></div>
    <div class="dt-actions">
      <button type="button" id="dtDialogCancel" onclick="dtCloseDialog()">Cancel</button>
      <button type="button" id="dtDialogApply">Confirm</button>
    </div>
  </div>
</div>
<script>
const SIGMA_PUBLIC_PREFIX={{ event_prefix|tojson }};
function sigmaFetch(resource,options){const url=typeof resource==='string'&&resource.startsWith('/api/')?SIGMA_PUBLIC_PREFIX+resource:resource;return window.fetch(url,options)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function refToggleMenu(){
 const closed=document.body.classList.toggle('ref-menu-compact');
 const btn=document.getElementById('refMenuToggle');
 btn.setAttribute('aria-expanded', String(!closed));
 btn.setAttribute('aria-label', closed?'Mở menu':'Thu gọn menu');
 btn.title=closed?'Mở menu':'Thu gọn menu';
}
function clearQuickMenuActive(){document.querySelectorAll('.quickMenuBtn').forEach(x=>x.classList.remove('active'))}
function closeSidebarOnMobile(){}
let CURRENT_REFEREE_TAB='referee-list';
function showRefereeTab(which,load=true){
 // A deliberate change of menu ends this page's referee authorization.
 // Internal navigation after a fresh login uses load=false and keeps that login.
 if(load && which!==CURRENT_REFEREE_TAB){kRevokeWeighinOnLeave();}
 CURRENT_REFEREE_TAB=which;
 const map={'referee-list':'refListView','mission':'missionView','weigh-in':'kWeighView','division-timelines':'divisionTimelineView'};
 for(const id of Object.values(map))document.getElementById(id)?.classList.add('hidden');
 document.getElementById(map[which]||map['referee-list'])?.classList.remove('hidden');clearQuickMenuActive();
 const b={'referee-list':'tabRefList','mission':'tabMission','weigh-in':'btnKyeorugiWeighin','division-timelines':'tabDivisionTimeline'};
 document.getElementById(b[which])?.classList.add('active');
 if(load&&which==='referee-list')loadRefereeList();if(load&&which==='mission')loadMission();if(load&&which==='division-timelines')loadDivisionTimeline();

}

// Admin Timeline Editor. Public viewing does not require login.
let DT_ADMIN=false,DT_CSRF='',DT_CATALOG=[],DT_REVISION=0;
const dtApi=(url,opts={})=>sigmaFetch('/api/division-timelines'+url,{credentials:'same-origin',...opts,
 headers:{'Content-Type':'application/json',...(DT_CSRF?{'X-Timeline-CSRF':DT_CSRF}:{}),...(opts.headers||{})}})
 .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||'HTTP '+r.status);return d});
function dtCloseDialog(){document.getElementById('dtDialog').classList.add('hidden')}
document.addEventListener('keydown',ev=>{
 const box=document.getElementById('dtDialog');
 if(!box || box.classList.contains('hidden'))return;
 if(ev.key==='Escape'){ev.preventDefault();dtCloseDialog();}
 if(ev.key==='Enter' && (ev.target.id==='dtId'||ev.target.id==='dtPass')){ev.preventDefault();document.getElementById('dtDialogApply').click();}
});
function dtDialog(title,content,onApply){document.getElementById('dtDialogTitle').textContent=title;
 document.getElementById('dtDialogContents').innerHTML=content;
 document.getElementById('dtDialogApply').onclick=onApply;
 document.getElementById('dtDialog').classList.remove('hidden');
 const first=document.getElementById('dtDialogContents').querySelector('input,select');
 if(first)first.focus();}
async function openTimelineAdmin(){
 if(DT_ADMIN){dtLogout();return;}
 dtDialog('Division Timeline — Admin Login',
 '<label>ID</label><input id="dtId" autocomplete="username"><label>Password</label><input id="dtPass" type="password" autocomplete="current-password">',
 async()=>{try{const d=await dtApi('/admin/login',{method:'POST',body:JSON.stringify({id:document.getElementById('dtId').value,password:document.getElementById('dtPass').value})});
 DT_CSRF=d.csrf;DT_ADMIN=true;dtCloseDialog();
 const data=await dtApi('/admin/catalog');DT_CATALOG=data.items||[];
 // Legacy is intentionally not edited in-place. New slots publish separately.
 if(!DIVISION_DATA||DIVISION_DATA.mode!=='managed')DIVISION_DATA={mode:'managed',days:DIVISION_DATA?.days||1,slots:[],revision:0};
 DT_REVISION=DIVISION_DATA.revision||0;dtDrawAdminState();drawDivisionTimeline();
 }catch(e){alert(e.message)}});
}
function dtDrawAdminState(){document.getElementById('divisionAdminTools').classList.toggle('hidden',!DT_ADMIN);
 document.getElementById('divisionAdminBtn').textContent=DT_ADMIN?'Exit Admin':'Admin Login';}
async function dtLogout(){try{await dtApi('/admin/logout',{method:'POST'})}catch(e){} DT_ADMIN=false;DT_CSRF='';DT_CATALOG=[];dtDrawAdminState();DIVISION_DATA=null;await loadDivisionTimeline(true);}
function dtAddDay(){if(!DT_ADMIN)return;if(DIVISION_DATA.days>=31){alert('Tối đa 31 Day');return;}
 DIVISION_DATA.days++;DIVISION_DAY=DIVISION_DATA.days;drawDivisionTimeline();}
function dtRemoveDay(){if(!DT_ADMIN||DIVISION_DATA.days<=1)return;
 const d=DIVISION_DATA.days;if((DIVISION_DATA.slots||[]).some(s=>s.day===d)&&!confirm('Day cuối đã có khung giờ. Xóa toàn bộ Day '+d+'?'))return;
 DIVISION_DATA.slots=DIVISION_DATA.slots.filter(s=>s.day!==d);DIVISION_DATA.days--;DIVISION_DAY=DIVISION_DATA.days;drawDivisionTimeline();}
function dtNewId(prefix){return prefix+'_'+Date.now()+'_'+Math.floor(Math.random()*100000)}
function dtTimeOK(v,isEnd=false){return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(v)||(isEnd&&v==='24:00')}
// Shorthand: 19=>19:00; 1930=>19:30; 113=>11:30; 093=>09:30.
function dtNormalizeTime(raw,isEnd=false){
 const v=String(raw??'').trim();if(!v)return null;
 let hh,mm;
 if(v.includes(':')){
  const m=v.match(/^(\d{1,2}):(\d{0,2})$/);if(!m)return null;
  hh=Number(m[1]);mm=m[2].length===1?Number(m[2])*10:Number(m[2]||0);
 }else if(/^\d{1,4}$/.test(v)){
  if(v.length<=2){hh=Number(v);mm=0;}
  else if(v.length===3){
   hh=Number(v.slice(0,2));mm=Number(v[2])*10;
   if(hh>23){hh=Number(v[0]);mm=Number(v.slice(1));}
  }else{hh=Number(v.slice(0,2));mm=Number(v.slice(2));}
 }else return null;
 const result=String(hh).padStart(2,'0')+':'+String(mm).padStart(2,'0');
 return dtTimeOK(result,isEnd)?result:null;
}
function dtTimeFields(st,en){return '<label>Start (24-hour)</label><input type="text" inputmode="numeric" id="dtStart" maxlength="5" placeholder="09, 093, 0930" value="'+esc(st)+'">'+
 '<label>End (24-hour)</label><input type="text" inputmode="numeric" id="dtEnd" maxlength="5" placeholder="19, 1930, 113" value="'+esc(en)+'">'+
 '<div class="dt-time-note">Nhập nhanh: 19 → 19:00 · 1930 → 19:30 · 113 → 11:30 · 093 → 09:30. Phút viết 1 số là hàng chục phút. End được nhập 24 hoặc 2400.</div>'}
function dtReadTime(){
 const startEl=document.getElementById('dtStart'),endEl=document.getElementById('dtEnd');
 const st=dtNormalizeTime(startEl.value),en=dtNormalizeTime(endEl.value,true);
 if(!st||!en||st>=en){alert('Thời gian không hợp lệ. Ví dụ: 0930 - 113 hoặc 17 - 1930.');return null;}
 startEl.value=st;endEl.value=en;
 if((DIVISION_DATA.slots||[]).some(s=>s.day===DIVISION_DAY && s.id!==DT_EDITING_ID && st<s.end&&en>s.start)){
   alert('Khung giờ bị trùng với Time Slot, Lunch hoặc Break khác.');return null;}
 return [st,en];}
let DT_EDITING_ID=null;
function dtAddSlot(){if(!DT_ADMIN)return;DT_EDITING_ID=null;
 const existingLunch=(DIVISION_DATA.slots||[]).find(s=>s.day===DIVISION_DAY&&s.kind==='lunch');
 const defaultStart=existingLunch?'13:30':'09:00';
 dtDialog('Time Slot — Day '+DIVISION_DAY,dtTimeFields(defaultStart,existingLunch?'14:00':'10:00'),()=>{
 const t=dtReadTime();if(!t)return;
 const lunch=(DIVISION_DATA.slots||[]).find(s=>s.day===DIVISION_DAY&&s.kind==='lunch');
 const session=lunch && t[0]>=lunch.end?'Evening':'Morning';
 DIVISION_DATA.slots.push({id:dtNewId('slot'),day:DIVISION_DAY,kind:'match',session,start:t[0],end:t[1],assignments:{}});
 dtCloseDialog();drawDivisionTimeline();});}
function dtSetLunch(){if(!DT_ADMIN)return;
 const existing=(DIVISION_DATA.slots||[]).find(s=>s.day===DIVISION_DAY&&s.kind==='lunch');
 DT_EDITING_ID=existing?.id||null;
 dtDialog('Lunch Time — Day '+DIVISION_DAY,dtTimeFields(existing?.start||'12:00',existing?.end||'13:30'),()=>{
 const t=dtReadTime();if(!t)return;
 if(existing){existing.start=t[0];existing.end=t[1];}
 else DIVISION_DATA.slots.push({id:dtNewId('lunch'),day:DIVISION_DAY,kind:'lunch',session:'Break',start:t[0],end:t[1],assignments:{}});
 DT_EDITING_ID=null;dtCloseDialog();drawDivisionTimeline();});}
function dtAddBreak(editId){if(!DT_ADMIN)return;
 const existing=editId?(DIVISION_DATA.slots||[]).find(s=>s.id===editId&&s.day===DIVISION_DAY&&s.kind==='break'):null;
 DT_EDITING_ID=existing?.id||null;
 dtDialog((existing?'Edit':'Add')+' Break Time — Day '+DIVISION_DAY,dtTimeFields(existing?.start||'17:00',existing?.end||'17:30'),()=>{
 const t=dtReadTime();if(!t)return;
 if(existing){existing.start=t[0];existing.end=t[1];}
 else DIVISION_DATA.slots.push({id:dtNewId('break'),day:DIVISION_DAY,kind:'break',session:'Break',start:t[0],end:t[1],assignments:{}});
 DT_EDITING_ID=null;dtCloseDialog();drawDivisionTimeline();});}
function dtDeleteSlot(slotId){if(!DT_ADMIN)return;if(!confirm('Xóa khung giờ và tất cả các Court đã chọn?'))return;
 DIVISION_DATA.slots=DIVISION_DATA.slots.filter(s=>s.id!==slotId);drawDivisionTimeline();}
function dtAssignmentList(value){
 if(Array.isArray(value))return value.filter(x=>x && typeof x==='object');
 return value && typeof value==='object'?[value]:[];
}
function dtSameAssignment(a,b){return a.discipline===b.discipline&&a.division===b.division&&a.phase===b.phase;}
function dtIsAssignedElsewhere(choice,slotId,court){
 return (DIVISION_DATA.slots||[]).some(slot=>slot.kind==='match' &&
   Object.entries(slot.assignments||{}).some(([c,entries])=>
    !(slot.id===slotId && Number(c)===Number(court))&&
    dtAssignmentList(entries).some(x=>dtSameAssignment(x,choice)&&
      Number(slot.day)===Number(choice.day)&&Number(c)===Number(choice.court))));
}
function dtEditCell(slotId,court){if(!DT_ADMIN)return;
 const s=DIVISION_DATA.slots.find(x=>x.id===slotId);if(!s||s.kind==='lunch'||s.kind==='break')return;
 const available=DT_CATALOG.filter(x=>Number(x.day)===Number(s.day)&&Number(x.court)===Number(court));
 let draft=dtAssignmentList(s.assignments?.[court]).map(x=>({...x}));
 const box='<div id="dtCellItems"></div><label>Add Round / Division (set in Match Schedule)</label>'+ 
  '<select id="dtChoice"></select><div class="dt-cell-controls"><button type="button" id="dtAddChoice">+ Add Division</button></div>'+
  '<div class="dt-time-note">Đã xếp ở ô khác sẽ tự ẩn. Bấm × để xóa riêng từng nội dung; hoặc chọn Empty để xóa nội dung cuối.</div>';
 dtDialog('Day '+s.day+' · Court '+court+' · '+s.start+'–'+s.end,box,()=>{
  if(!s.assignments)s.assignments={};
  if(draft.length)s.assignments[court]=draft;
  else delete s.assignments[court];
  dtCloseDialog();drawDivisionTimeline();
 });
 const choice=document.getElementById('dtChoice'),items=document.getElementById('dtCellItems');
 function draw(){
  items.innerHTML=draft.length?draft.map((x,i)=>'<div class="dt-cell-item"><span><strong>'+esc(x.phase)+'</strong> · '+esc(x.division)+' <small>('+esc(x.discipline)+')</small></span><button type="button" data-remove-item="'+i+'" title="Remove division">×</button></div>').join(''):'<div class="dt-cell-empty">No divisions selected</div>';
  items.querySelectorAll('[data-remove-item]').forEach(el=>el.addEventListener('click',()=>{draft.splice(Number(el.dataset.removeItem),1);draw();}));
  const unused=available.filter(x=>!draft.some(a=>dtSameAssignment(a,x))&&!dtIsAssignedElsewhere(x,s.id,court));
  choice.innerHTML='<option value="">Empty — no match</option>'+unused.map((x,i)=>'<option value="'+i+'">'+esc(x.label)+' | '+x.matches.length+' matches</option>').join('');
  choice._available=unused;
  document.getElementById('dtAddChoice').disabled=unused.length===0;
 }
 document.getElementById('dtAddChoice').addEventListener('click',()=>{
  const i=choice.value;if(i===''){if(draft.length){draft.pop();draw();}return;}
  const x=choice._available[Number(i)];if(!x)return;
  draft.push({discipline:x.discipline,display_type:x.display_type,division:x.division,phase:x.phase});draw();
 });
 choice.addEventListener('change',()=>{
  // Selecting a division immediately adds it; each entry can be individually removed.
  if(choice.value==='')return;
  const x=choice._available[Number(choice.value)];if(!x)return;
  draft.push({discipline:x.discipline,display_type:x.display_type,division:x.division,phase:x.phase});draw();
 });
 draw();
}
async function dtSave(){if(!DT_ADMIN)return;
 try{const d=await dtApi('/admin/save',{method:'PUT',body:JSON.stringify({day_count:DIVISION_DATA.days,slots:DIVISION_DATA.slots,revision:DT_REVISION})});
 DT_REVISION=d.revision;DIVISION_DATA.revision=d.revision;alert('Đã lưu và công bố Division Timelines.');}
 catch(e){alert('Không lưu được lịch: '+e.message)}}
function dtCourtCount(day,slots){
 const fromMatches=DT_CATALOG.filter(x=>Number(x.day)===Number(day)).map(x=>Number(x.court)||0);
 const savedMax=Number(DIVISION_DATA?.court_max_by_day?.[String(day)]||0);
 const assigned=slots.flatMap(s=>Object.keys(s.assignments||{}).map(Number));
 // The highest scheduled Court is retained: 1,3,4 => show 1..4.
 return Math.min(10,Math.max(1,savedMax,...fromMatches,...assigned));
}
function dtDisplayType(a){
 const explicit=String(a?.display_type||'').toLowerCase();
 const hint=String(a?.division||'').toLowerCase();
 const discipline=String(a?.discipline||'').toLowerCase();
 if(discipline==='kyeorugi'||discipline==='kyorugi')return 'Kyorugi';
 if(explicit.includes('freestyle')||hint.includes('freestyle'))return 'Freestyle';
 if(explicit.includes('demonstration')||hint.includes('demonstration'))return 'Demonstration';
 return 'Poomsae';
}
function dtDrawManaged(){
 const data=DIVISION_DATA,days=Number(data.days||1),tabs=document.getElementById('divisionDayTabs');
 tabs.innerHTML=Array.from({length:days},(_,i)=>'<span class="division-day-wrap"><button class="division-day-tab'+(i+1===DIVISION_DAY?' active':'')+'" data-day="'+(i+1)+'">Day '+(i+1)+'</button>'+(DT_ADMIN&&i+1===days&&days>1?'<button class="division-day-remove" title="Delete last Day" onclick="dtRemoveDay()">×</button>':'')+'</span>').join('')+
 (DT_ADMIN?'<button class="division-day-tab division-day-add" onclick="dtAddDay()" title="Add Day">+</button>':'');
 tabs.querySelectorAll('[data-day]').forEach(btn=>btn.addEventListener('click',()=>{DIVISION_DAY=Number(btn.dataset.day);drawDivisionTimeline()}));
 const slots=(data.slots||[]).filter(x=>x.day===DIVISION_DAY).sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end));
 const courts=dtCourtCount(DIVISION_DAY,slots),lunch=slots.find(s=>s.kind==='lunch');
 document.getElementById('divisionTimelineStatus').textContent='Day '+DIVISION_DAY+' · '+slots.filter(s=>!['lunch','break'].includes(s.kind)).length+' time slots · '+courts+' Courts'+(DT_ADMIN?' · Admin Edit Mode':'');
 const board=document.getElementById('divisionTimelineBoard');board.classList.add('dt-dynamic-board');board.style.setProperty('--dt-courts',String(courts));
 let html='<div class="division-courts"><div>Time</div>'+Array.from({length:courts},(_,i)=>'<div>Court '+(i+1)+'</div>').join('')+'</div>';
 let lunchFallback=false;
 const placeholder=()=>'<div class="dt-lunch-placeholder">LUNCH TIME · Chưa thiết lập giờ'+(DT_ADMIN?' <button type="button" onclick="dtSetLunch()">Set Lunch</button>':'')+'</div>';
 for(const s of slots){
  const kind=s.kind||'match';
  if(!lunch && !lunchFallback && s.session==='Evening'){html+=placeholder();lunchFallback=true;}
  if(kind==='lunch'||kind==='break'){
   const label=kind==='lunch'?'LUNCH TIME':'BREAK TIME';
   html+='<div class="dt-managed-row dt-break-row"><div class="dt-break-band '+(kind==='lunch'?'dt-lunch':'dt-rest')+'">'+label+' · '+esc(s.start)+'–'+esc(s.end)+
   (DT_ADMIN?' <button class="dt-break-edit" data-break-edit="'+esc(s.id)+'">Edit</button>'+
   (kind==='break'?'<button class="dt-break-remove" data-delete="'+esc(s.id)+'">×</button>':''):'')+'</div></div>';
   continue;
  }
  html+='<div class="dt-managed-row"><div class="dt-time">'+esc(s.start)+'–'+esc(s.end)+
   (DT_ADMIN?' <button class="dt-slot-remove" data-delete="'+esc(s.id)+'">×</button>':'')+'</div>';
  for(let court=1;court<=courts;court++){
   const entries=dtAssignmentList(s.assignments?.[court]);
   const text=entries.length?entries.map((a,i)=>'<div class="dt-div-entry"><strong>'+esc(a.phase)+' - '+esc(dtDisplayType(a))+'</strong><small>'+esc(a.division)+'</small></div>').join('')+(DT_ADMIN?'<span class="dt-add-more">+</span>':''):(DT_ADMIN?'+':'');
   html+='<div class="'+(entries.length?'dt-booked':'dt-empty')+(DT_ADMIN?' dt-admin-cell':'')+'" '+(DT_ADMIN?'data-slot="'+esc(s.id)+'" data-court="'+court+'"':'')+'><div class="dt-entries">'+text+'</div></div>';
  }html+='</div>';
 }
 if(!lunch&&!lunchFallback&&DT_ADMIN)html+=placeholder();
 if(!slots.length)html+='<div class="division-empty">Chưa có khung giờ cho Day '+DIVISION_DAY+'.'+(DT_ADMIN?' Bấm + Time Slot hoặc Lunch Time để thiết lập.':'')+'</div>';
 board.innerHTML=html;
 board.querySelectorAll('[data-slot][data-court]').forEach(el=>el.addEventListener('click',()=>dtEditCell(el.dataset.slot,Number(el.dataset.court))));
 board.querySelectorAll('[data-delete]').forEach(el=>el.addEventListener('click',()=>dtDeleteSlot(el.dataset.delete)));
 board.querySelectorAll('[data-break-edit]').forEach(el=>el.addEventListener('click',()=>{
 const item=DIVISION_DATA.slots.find(s=>s.id===el.dataset.breakEdit);if(item?.kind==='lunch')dtSetLunch();else dtAddBreak(el.dataset.breakEdit);
 }));
}

// Division Timelines: public data only, never uses referee authentication.
let DIVISION_DATA=null, DIVISION_DAY=1;
async function loadDivisionTimeline(force=false){
 const status=document.getElementById('divisionTimelineStatus');
 if(DIVISION_DATA&&!force){drawDivisionTimeline();return;}
 status.textContent='Loading Division Timelines...';
 try{
  const response=await sigmaFetch('/api/division-timelines',{credentials:'same-origin'});
  const payload=await response.json();
  if(!response.ok)throw new Error(payload.error||'Cannot load timeline');
  DIVISION_DATA=payload;
  const days=Number(payload.days||1);
  DIVISION_DAY=Math.min(Math.max(1,DIVISION_DAY),days);
  drawDivisionTimeline();
 }catch(e){status.textContent='Không tải được lịch thi đấu: '+e.message;document.getElementById('divisionTimelineBoard').innerHTML='';}
}
function drawDivisionTimeline(){
 const data=DIVISION_DATA;if(!data)return;
 if(data.mode==='managed'){dtDrawManaged();return;}
 const days=Number(data.days||1), tabs=document.getElementById('divisionDayTabs');
 tabs.innerHTML=Array.from({length:days},(_,i)=>'<button type="button" role="tab" class="division-day-tab'+(i+1===DIVISION_DAY?' active':'')+'" aria-selected="'+(i+1===DIVISION_DAY)+'" data-day="'+(i+1)+'">Day '+(i+1)+'</button>').join('');
 tabs.querySelectorAll('[data-day]').forEach(btn=>btn.addEventListener('click',()=>{DIVISION_DAY=Number(btn.dataset.day);drawDivisionTimeline()}));
 const rows=(data.items||[]).filter(x=>Number(x.day_number)===DIVISION_DAY);
 const status=document.getElementById('divisionTimelineStatus');
 status.textContent='Day '+DIVISION_DAY+' · '+rows.length+' scheduled divisions · 10 Courts';
 const head='<div class="division-courts"><div>Time</div>'+Array.from({length:10},(_,i)=>'<div>Court '+(i+1)+'</div>').join('')+'</div>';
 let out=head;
 for(const [session,minimum] of [['Morning',5],['Evening',6]]){
  const sessionRows=rows.filter(r=>r.session===session);
  const slots=Math.max(minimum,...sessionRows.map(r=>Number(r.start_slot||1)+Number(r.slot_span||1)-1));
  out+='<div class="division-session" style="grid-template-rows:repeat('+slots+',minmax(68px,auto))">';
  for(let slot=1;slot<=slots;slot++){
   out+='<div class="division-slot-time" style="grid-row:'+slot+';grid-column:1">'+(slot===1?esc(session):'')+'</div>';
   for(let court=1;court<=10;court++){
    out+='<div class="division-cell" style="grid-column:'+(court+1)+';grid-row:'+slot+'"></div>';
   }
  }
  for(const r of sessionRows){
   const court=Number(r.court),start=Number(r.start_slot),span=Number(r.slot_span);
   if(!Number.isInteger(court)||court<1||court>10||!Number.isInteger(start)||start<1||!Number.isInteger(span)||span<1)continue;
   out+='<div class="division-block" style="grid-column:'+(court+1)+';grid-row:'+start+' / span '+span+'">'+esc(r.division||'')+'</div>';
  }
  out+='</div>';
  if(session==='Morning')out+='<div class="division-lunch">LUNCH TIME</div>';
 }
 if(!rows.length)out+='<div class="division-empty">Chưa có lịch thi đấu cho Day '+DIVISION_DAY+'. Ban tổ chức sẽ cập nhật lịch tại đây.</div>';
 document.getElementById('divisionTimelineBoard').innerHTML=out;
}

// Open the existing Poomsae Judge application as a full page, same domain.
function openPoomsaeScore(){
  kRevokeWeighinOnLeave();
  window.location.assign(SIGMA_PUBLIC_PREFIX + '/Poomsae-score/');
}
let pendingRefereeTab='referee-list';
function requireLogin(which){if(!K_SESSION.authorized){pendingRefereeTab=which||'referee-list';kShowRefereeDialog();return false}return true}
let REF_ROWS=[];
let REF_ME=null;
async function loadRefereeList(){
 if(!requireLogin('referee-list'))return;
 const status=document.getElementById('refListStatus');status.textContent='Loading Referee Information...';
 try{
  const [me,roster]=await Promise.all([kGet('/api/referee/me'),kGet('/api/referees')]);
  REF_ME=me;REF_ROWS=roster;renderRefereeProfile();renderRefList();
  status.textContent='Total '+roster.length+' referees · Event '+(me.event_code||'');
 }catch(e){status.textContent='Không tải được Referee Information: '+e.message;}
}
function refereeFlagUrl(noc, large=false){
 const key=String(noc||'').toUpperCase().trim();
 const cc=REF_FLAG_MAP[key];
 return cc?'https://flagcdn.com/w'+(large?'320':'80')+'/'+cc+'.png':'';
}
function refereeFlag(noc){
 const key=String(noc||'').toUpperCase().trim();
 const url=refereeFlagUrl(key);
 return url?'<img class="ref-flag" loading="lazy" src="'+url+'" alt="'+esc(key)+' flag" onerror="this.style.display=\'none\'">':'<span class="ref-profile-flag-unknown">—</span>';
}
function renderRefereeProfile(){
 const p=REF_ME,box=document.getElementById('refereeProfile');
 if(!p){box.classList.add('hidden');return;}
 const noc=String(p.NOC||'—').trim();
 const flag=refereeFlagUrl(noc,true);
 const flagHtml=flag?'<img class="ref-profile-big-flag" src="'+flag+'" alt="'+esc(noc)+' flag" onerror="this.replaceWith(document.createTextNode(\'Flag unavailable\'))">':'<span class="ref-profile-flag-unknown">Flag unavailable</span>';
 const fields=[['No. Referee',p['No.Referee']],['ID. Referee',p['ID.Referee']],['BOD',p.BOD],['Gender',p.Gender]];
 const details=fields.map(([label,value])=>'<div class="ref-detail-field"><div class="ref-detail-label">'+esc(label)+'</div><div class="ref-detail-value">'+esc(value??'—')+'</div></div>').join('');
 box.innerHTML='<div class="ref-profile-heading"><div class="ref-profile-title">Referee Name: <span class="ref-profile-fullname">'+esc(p.Fullname||'—')+'</span></div><button class="btn-result ref-profile-refresh" type="button" onclick="loadRefereeList()">Refresh</button></div>'+
  '<div class="ref-profile-layout"><div class="ref-profile-country"><div class="ref-country-code">'+esc(noc)+'</div><div class="ref-country-flag-frame">'+flagHtml+'</div></div>'+
  '<div class="ref-profile-details">'+details+'</div></div>';
 box.classList.remove('hidden');
}
const REF_FLAG_MAP={VIE:'vn',KOR:'kr',INA:'id',CAM:'kh',LAO:'la',POR:'pt',PRT:'pt',THA:'th',JPN:'jp',CHN:'cn',TPE:'tw',USA:'us',MGL:'mn',PHI:'ph',MAS:'my',SGP:'sg',HKG:'hk',IRI:'ir',IRN:'ir',UZB:'uz',KAZ:'kz',IND:'in',GBR:'gb',FRA:'fr',GER:'de',AUS:'au',CAN:'ca',MYA:'mm',EGY:'eg',NEP:'np',AFG:'af',PAK:'pk',TUR:'tr',ESP:'es',ITA:'it',BRA:'br',MEX:'mx',UAE:'ae',KSA:'sa',QAT:'qa',JOR:'jo',IRQ:'iq',NZL:'nz'};
function renderRefList(){
 const wrap=document.getElementById('refRosterGroups');
 const rows=[...REF_ROWS].sort((a,b)=>Number(a['No.Referee']||0)-Number(b['No.Referee']||0));
 if(!rows.length){wrap.innerHTML='<div class="ref-status">Chưa có trọng tài trong giải này.</div>';return;}
 // Column-major order: 15 people => 1–5, 6–10, 11–15.
 const size=Math.ceil(rows.length/3);
 const groups=[rows.slice(0,size),rows.slice(size,size*2),rows.slice(size*2)];
 wrap.innerHTML=groups.map(group=>{
  const body=group.map(r=>'<tr><td>'+esc(r['No.Referee']??'')+'</td><td>'+refereeFlag(r.NOC)+'</td><td>'+esc(r.NOC||'')+'</td><td>'+esc(r.Fullname||'')+'</td></tr>').join('');
  return '<div class="ref-roster-card"><table><thead><tr><th>No.</th><th>Flag</th><th>NOC</th><th>Fullname</th></tr></thead><tbody>'+body+'</tbody></table></div>';
 }).join('');
}
async function loadMission(){if(!requireLogin('mission'))return;const el=document.getElementById('missionStatus');el.textContent='Loading assignments...';try{
 const d=await kGet('/api/mission');el.textContent=d.referee+' — '+d.message;
 document.getElementById('missionTable').innerHTML='<thead><tr><th>Assigned Role</th><th>Status</th></tr></thead><tbody>'+d.duties.map(x=>'<tr><td>'+esc(x.role)+'</td><td>'+esc(x.assigned)+'</td></tr>').join('')+'</tbody>';
 }catch(e){el.textContent=e.message}}
function tickClock(){const d=new Date(),tz={timeZone:'Asia/Ho_Chi_Minh'};document.getElementById('siteHeaderClockTime').textContent=d.toLocaleTimeString('en-GB',{...tz,hour12:false});document.getElementById('siteHeaderClockDate').textContent=d.toLocaleDateString('en-GB',tz)}tickClock();setInterval(tickClock,1000);

// Pointer drag scrolls the table horizontally; inputs and buttons retain normal behavior.
(function refInstallWeighDrag(){
 const area=document.querySelector('#kWeighView .k-w-wrap');
 if(!area)return;
 let state=null;
 area.addEventListener('pointerdown',ev=>{
  if(ev.button!==0 || ev.target.closest('input,button,select,a,textarea'))return;
  state={id:ev.pointerId,x:ev.clientX,y:ev.clientY,left:area.scrollLeft,top:area.scrollTop,moved:false};
 });
 area.addEventListener('pointermove',ev=>{
  if(!state||ev.pointerId!==state.id)return;
  const dx=ev.clientX-state.x,dy=ev.clientY-state.y;
  if(!state.moved && Math.hypot(dx,dy)<6)return;
  if(!state.moved){state.moved=true;area.classList.add('k-dragging');area.setPointerCapture(ev.pointerId);}
  area.scrollLeft=state.left-dx;
  area.scrollTop=state.top-dy;
  ev.preventDefault();
 });
 function finish(ev){if(!state||ev.pointerId!==state.id)return;state=null;area.classList.remove('k-dragging');if(area.hasPointerCapture(ev.pointerId))area.releasePointerCapture(ev.pointerId);}
 area.addEventListener('pointerup',finish);
 area.addEventListener('pointercancel',finish);
 area.addEventListener('lostpointercapture',()=>{state=null;area.classList.remove('k-dragging')});
})();

// ===== KYEORUGI WEIGH-IN =====
let K_ROWS = [];
let K_SESSION = {authorized:false, csrf:''};
// Auth is page-scoped: do not resurrect a previous referee login on reload.
function kRevokeWeighinOnLeave(){
    const csrf=K_SESSION.csrf;
    K_SESSION={authorized:false,csrf:'',page_token:''};
    K_LOCKS.clear();
    K_ROWS=[];REF_ROWS=[];REF_ME=null;
    document.getElementById('refereeProfile')?.classList.add('hidden');
    const roster=document.getElementById('refRosterGroups');if(roster)roster.innerHTML='';
    const mission=document.getElementById('missionTable');if(mission)mission.innerHTML='';
    const weigh=document.getElementById('kWeighTable');if(weigh)weigh.innerHTML='';
    const label=document.getElementById('kRefereeLabel');if(label)label.textContent='';
    const status=document.getElementById('kWeighStatus');if(status)status.textContent='';
    // Server also revokes the signed Flask session; a stale browser token cannot write.
    if(csrf){sigmaFetch('/api/kyeorugi/leave',{
      method:'POST',credentials:'same-origin',keepalive:true,
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({csrf})
    }).catch(()=>{});}
}
// pagehide fires for refresh, closing the tab and navigation to other pages.
window.addEventListener('pagehide',()=>{
    if(!K_SESSION.authorized || !K_SESSION.csrf) return;
    const csrf=K_SESSION.csrf;
    K_SESSION={authorized:false,csrf:'',page_token:''};
    K_LOCKS.clear();
    const data=new Blob([JSON.stringify({csrf})],{type:'application/json'});
    if(navigator.sendBeacon){
        navigator.sendBeacon(SIGMA_PUBLIC_PREFIX+'/api/kyeorugi/leave',data);
    }else{
        sigmaFetch('/api/kyeorugi/leave',{
            method:'POST',credentials:'same-origin',keepalive:true,
            headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
            body:JSON.stringify({csrf})
        }).catch(()=>{});
    }
});
const K_FIELD = ['Gen. Weigh-in 1', 'Gen. Weigh-in 2', 'Random Weigh-in'];
const kById = x => document.getElementById(x);
const kGet = async (url, options={}) => {
    const response = await sigmaFetch(url, {credentials:'same-origin',...options,headers:{'X-Weighin-Page':K_SESSION.page_token||'',...(options.headers||{})}});
    const data = await response.json();
    if(!response.ok) throw new Error(data.error || 'Server error');
    return data;
};
// Age is calculated from birth YEAR and current calendar YEAR (not Event_1..12).
// Each athlete belongs to exactly one band: <=9, 10-12, 13-14, 15-17, >=18.
function kBirthYear(row){
    const raw=String(row.DOB||'').trim();
    const m=raw.match(/^(\d{4})(?:[-/]\d{1,2}[-/]\d{1,2})?$/) ||
        raw.match(/^\d{1,2}[-/]\d{1,2}[-/](\d{4})$/);
    return m?Number(m[1]):null;
}
function kDOBBand(row){
    const birth=kBirthYear(row), currentYear=new Date().getFullYear();
    if(!birth||birth>currentYear||birth<1900)return '';
    const age=currentYear-birth;
    return age<=9?'U9':age<=12?'U12':age<=14?'Cadet (U14)':
           age<=17?'Junior (U17)':'Senior (O18)';
}
function kEventsForAge(row){
    const selected=kById('kAgeFilter').value;
    return (!selected||kDOBBand(row)===selected) ? (row._events||[]) : [];
}
function kWeightCategories(row){
    const values=kEventsForAge(row).filter(x=>x.division==='Individual' && x.weight)
        .map(x=>String(x.weight).replace(/\s+/g,' ').trim());
    return [...new Set(values)];
}
function kRenderFilters(){
    const unitEl=kById('kCountryFilter'), weightEl=kById('kWeightFilter');
    const previousUnit=unitEl.value, previousWeight=weightEl.value;
    const gender=kById('kGenderFilter').value;
    const age=kById('kAgeFilter').value;
    const units=[...new Set(K_ROWS.map(r=>`${r.NOC || ''} | ${r.Country || ''}`))].sort();
    unitEl.innerHTML='<option value="">All Units / Country</option>'+units.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
    unitEl.value=units.includes(previousUnit)?previousUnit:'';
    const subset=K_ROWS.filter(r=>(!unitEl.value || `${r.NOC || ''} | ${r.Country || ''}`===unitEl.value)
        && (!gender || String(r.Gender||'').toLowerCase()===gender.toLowerCase())
        && (!age || kEventsForAge(r).length>0));
    const weights=[...new Set(subset.flatMap(kWeightCategories))].sort((a,b)=>{
        const aa=parseFloat(a.replace(/[^0-9.]/g,'')), bb=parseFloat(b.replace(/[^0-9.]/g,''));
        return aa-bb || a.localeCompare(b);
    });
    weightEl.innerHTML='<option value="">All Weights</option>'+weights.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
    weightEl.value=weights.includes(previousWeight)?previousWeight:'';
}
function kDivisionSummary(row){
    // Combine the registered Kyeorugi events into compact labels, preserving order.
    // Individual: -38 / +38; Team 3/4/5: T3/T4/T5; Mixed Team: MT.
    const labels=[];
    for(const event of kEventsForAge(row)){
        const division=String(event.division || '').trim();
        let value='';
        if(division === 'Individual'){
            const match=String(event.weight || '').match(/([+-])\s*(\d+(?:[.,]\d+)?)/);
            if(match) value=match[1]+match[2].replace(',', '.');
        }else if(division === 'Mixed Team'){
            value='MT';
        }else{
            const team=division.match(/^Team\s*([345])$/i);
            if(team) value='T'+team[1];
        }
        if(value && !labels.includes(value)) labels.push(value);
    }
    return labels.join('|');
}
// Weigh-in is assessed only for Individual categories. Team entries do not
// create additional weight limits. Age cohorts must not share a weight chain.
function kAgeGroup(row, event){
    return kDOBBand(row) || 'Open';
}
function kWeightClass(row, event){
    return String(row.Gender||'').toLowerCase()+'|'+kAgeGroup(row,event);
}
function kJudgeGeneralWeight(row, value){
    if(value===null || value===undefined || value==='') return null;
    const grams=Math.round(Number(String(value).replace(',','.'))*1000);
    if(!Number.isFinite(grams) || grams<=0) return null;
    const selected=kById('kWeightFilter').value;
    const events=kEventsForAge(row).filter(e=>e.division==='Individual' &&
        (!selected || String(e.weight||'').replace(/\s+/g,' ').trim()===selected));
    // If an athlete competes in multiple distinct weight categories, require a
    // selected Weight filter before showing a pass/fail verdict.
    const distinct=[...new Set(events.map(e=>String(e.weight||'').replace(/\s+/g,'').toLowerCase()))];
    if(distinct.length!==1) return null;
    const event=events[0];
    const weight=distinct[0].match(/^([+-])(\d+)(?:kg)?$/);
    if(!weight) return null;
    const ceiling=Number(weight[2]);
    const cohort=kWeightClass(row,event);
    const all=K_ROWS.flatMap(r=>(r._events||[])
        .filter(e=>e.division==='Individual' && kWeightClass(r,e)===cohort)
        .map(e=>String(e.weight||'').replace(/\s+/g,'').toLowerCase()));
    const limits=[...new Set(all.map(v=>v.match(/^-(\d+)(?:kg)?$/))
        .filter(Boolean).map(m=>Number(m[1])))].sort((a,b)=>a-b);
    if(weight[1]==='+'){
        // +38 kg begins at 38.100 kg, above the -38 kg class.
        return grams>=ceiling*1000+100;
    }
    const prior=limits.filter(n=>n<ceiling).pop();
    const minimum=prior===undefined?0:prior*1000+100;
    const maximum=ceiling*1000+99;
    return grams>=minimum && grams<=maximum;
}
function kCanEnterSecond(row){
    const first=row[K_FIELD[0]];
    return first!==null && first!==undefined && first!=='' &&
        kJudgeGeneralWeight(row,first)===false;
}
function kRenderRows(){
    const table=kById('kWeighTable');
    const unit=kById('kCountryFilter').value;
    const age=kById('kAgeFilter').value;
    const gender=kById('kGenderFilter').value;
    const weight=kById('kWeightFilter').value;
    const search=kById('kSearchFilter').value.trim().toUpperCase();
    const rows=K_ROWS.filter(r=>{
        if(unit && `${r.NOC || ''} | ${r.Country || ''}` !== unit) return false;
        if(age && !kEventsForAge(r).length) return false;
        if(gender && String(r.Gender||'').toLowerCase() !== gender.toLowerCase()) return false;
        if(weight && !kWeightCategories(r).includes(weight)) return false;
        if(search && ![r['GAL/ID ATHLETE'],r['Full name'],r.NOC,r.Country].some(v=>String(v||'').toUpperCase().includes(search))) return false;
        return true;
    });
    const headings=['GAL/ID ATHLETE','Full name','DOB','Gender','NOC','Country','Division',...K_FIELD];
    const active = K_SESSION.authorized && K_SESSION.weighin;
    table.innerHTML = `<thead><tr>${headings.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead>`+
      `<tbody>${rows.map((r,i)=>{
        let cells=headings.slice(0,6).map(h=>`<td>${esc(r[h]||'')}</td>`).join('');
        cells+=`<td class="k-division-cell" title="${esc(kDivisionSummary(r))}">${esc(kDivisionSummary(r)) || '—'}</td>`;
        K_FIELD.forEach((field,j)=>{
            const raw=r[field];
            const hasValue=raw!==null && raw!==undefined && raw!=='';
            const val=hasValue?Number(raw).toFixed(3).replace('.',','):'';
            const verdict=j<2 && hasValue?kJudgeGeneralWeight(r,raw):null;
            const color=verdict===true?' k-pass':verdict===false?' k-fail':'';
            const key=String(r['GAL/ID ATHLETE']||'')+'|'+field;
            // Existing recorded weights are readonly after reload unless this
            // browser retains a live correction window from a successful save.
            const editable=active && (j!==1 || kCanEnterSecond(r)) &&
                (!hasValue || (K_LOCKS.has(key) && Date.now()<K_LOCKS.get(key)));
            const hint=j===1 && !kCanEnterSecond(r)
                ? 'Only available when General Weigh-in 1 is outside the registered category'
                : verdict===true?'Within registered category':verdict===false?'Outside registered category':'';
            cells+=`<td><input class="k-weight-input${color}" data-row="${i}" data-field="${j}" type="text" inputmode="decimal" value="${esc(val)}" ${editable?'':'disabled'} maxlength="7" title="${esc(hint)}" aria-label="${esc(field)}" /></td>`;
        });
        return `<tr>${cells}</tr>`;
      }).join('')}</tbody>`;
    [...table.querySelectorAll('.k-weight-input')].forEach(input=>{
        const row=rows[Number(input.dataset.row)];
        const field=K_FIELD[Number(input.dataset.field)];
        input.dataset.old=input.value;
        const submit=()=>kSaveCell(input,row,field);
        input.addEventListener('blur',submit);
        input.addEventListener('keydown',ev=>{
            if(ev.key!=='Enter' && ev.key!=='Tab') return;
            // Enter/Tab goes DOWN the same weigh-in column, not across fields.
            // Shift+Tab goes up. Skip readonly/locked cells.
            ev.preventDefault();
            const direction=ev.shiftKey?-1:1;
            const column=input.dataset.field;
            const start=Number(input.dataset.row);
            const candidates=[...table.querySelectorAll('.k-weight-input')];
            const next=candidates.find(el=>!el.disabled &&
                el.dataset.field===column && Number(el.dataset.row)===start+direction) ||
                candidates.filter(el=>!el.disabled && el.dataset.field===column &&
                    (Number(el.dataset.row)-start)*direction>0)
                    .sort((a,b)=>(Number(a.dataset.row)-Number(b.dataset.row))*direction)[0];
            if(next){next.focus();next.select();next.scrollIntoView({block:'nearest'});}
            else input.blur();
        });
    });
    kById('kWeighStatus').textContent=`${rows.length} athletes / ${K_ROWS.length} Kyeorugi athletes${active ? ' | Authorized' : ' | Read-only'}`;
}
const K_LOCKS = new Map();
async function kSaveCell(input,row,field){
    if(!K_SESSION.authorized || !K_SESSION.weighin || input.disabled || input.dataset.pending==='1') return;
    if(field===K_FIELD[1] && !kCanEnterSecond(row)) return;
    const value=input.value.trim().replace(',','.');
    // Blank means SKIP. Never post an empty weight to Supabase on Tab/Enter.
    if(!value){
        if(input.dataset.old) input.value=input.dataset.old;
        return;
    }
    if(value===input.dataset.old.replace(',','.')) return;
    if(!/^\d{1,3}(\.\d{1,3})?$/.test(value)|| Number(value)<=0){
        input.value=input.dataset.old; alert('Số cân không hợp lệ (ví dụ 58,890).');return;
    }
    const id=String(row['GAL/ID ATHLETE']||'').trim();
    const key=id+'|'+field;
    if(K_LOCKS.has(key)&&Date.now()>K_LOCKS.get(key)){
        input.value=input.dataset.old;input.disabled=true;alert('Đã hết 5 giây chỉnh sửa. Liên hệ Admin để mở khóa.');return;
    }
    input.dataset.pending='1';input.classList.add('pending');
    try{
        const saveResult=await kGet('/api/kyeorugi/weigh-in', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':K_SESSION.csrf,'X-Weighin-Page':K_SESSION.page_token},body:JSON.stringify({athlete_id:id,field,weight:value})});
        input.dataset.old=Number(value).toFixed(3).replace('.',',');
        input.value=input.dataset.old;
        row[field]=Number(value);
        input.classList.remove('pending');input.classList.add('saved');
        // Five seconds counted from first successful save, not reset on corrections.
        if(!K_LOCKS.has(key)) K_LOCKS.set(key,Date.now()+Math.max(0,Number(saveResult.seconds_remaining||0))*1000);
        const remain=Math.max(0,K_LOCKS.get(key)-Date.now());
        setTimeout(()=>{if(input.isConnected){input.disabled=true;input.classList.remove('saved');input.title='Locked after 5 seconds';}},remain);
        // Preserve keyboard position when rerendering after an async save.
        // Otherwise the active input is destroyed and Enter/Tab navigation stops.
        const focused=document.activeElement;
        const restore=focused?.classList?.contains('k-weight-input')
            ? [focused.dataset.row,focused.dataset.field,focused.value,focused.dataset.old] : null;
        // Failed first weigh-in unlocks second; passing first keeps it disabled.
        kRenderRows();
        if(restore){
            const target=[...kById('kWeighTable').querySelectorAll('.k-weight-input')]
                .find(el=>el.dataset.row===restore[0] && el.dataset.field===restore[1] && !el.disabled);
            if(target){
                // Keep a partially typed weight in the next row.
                if(restore[2]!==restore[3]) target.value=restore[2];
                target.focus({preventScroll:true});
                if(restore[2]===restore[3]) target.select();
            }
        }
    }catch(error){
        input.value=input.dataset.old;input.classList.remove('pending');alert(error.message);
        if(/5 giây|locked|khóa/i.test(error.message)) input.disabled=true;
    }finally{input.dataset.pending='0';}
}
async function kReload(){
    kById('kWeighStatus').textContent='Loading Kyeorugi Information Lists...';
    try{ K_ROWS=await kGet('/api/kyeorugi/athletes');kRenderFilters();kRenderRows();}
    catch(error){kById('kWeighStatus').textContent=error.message;}
}
async function kRefreshSession(){
    // Only the current document's explicit login can activate weigh-in.
    const token=K_SESSION.page_token;
    try{
        const data=await kGet('/api/kyeorugi/session');
        K_SESSION=(data.authorized && token)
            ? {...data,page_token:token}
            : {authorized:false,csrf:'',page_token:''};
    }catch(error){K_SESSION={authorized:false,csrf:'',page_token:''};}
    kById('kRefereeLabel').textContent=K_SESSION.authorized?'Referee: '+K_SESSION.referee:'';
}
function kShowRefereeDialog(){
    kById('kLoginStatus').textContent='';
    kById('kRefId').value='';
    kById('kLoginBackdrop').classList.remove('hidden');
    kById('kLoginBox').classList.remove('hidden');
    kById('kRefId').focus();
}
function kHideRefereeDialog(){
    kById('kLoginBackdrop').classList.add('hidden');
    kById('kLoginBox').classList.add('hidden');
}
async function kOpenAuthorizedView(){
    kHideRefereeDialog();
    await kRefreshSession();
    const tab=pendingRefereeTab;
    showRefereeTab(tab,false);
    if(tab==='weigh-in')await kReload();
    else if(tab==='referee-list')await loadRefereeList();
    else if(tab==='mission')await loadMission();
}

async function kOpenView(){
    // Changing into Weigh-in never reuses a login from another menu.
    if(CURRENT_REFEREE_TAB!=='weigh-in')kRevokeWeighinOnLeave();
    if(K_SESSION.authorized && K_SESSION.weighin){showRefereeTab('weigh-in',false);await kReload();return;}
    pendingRefereeTab='weigh-in';
    kShowRefereeDialog();
}
kById('btnKyeorugiWeighin').addEventListener('click',kOpenView);
kById('kReloadBtn').addEventListener('click',kReload);
kById('kLoginClose').addEventListener('click',kHideRefereeDialog);
kById('kLoginBackdrop').addEventListener('click',kHideRefereeDialog);
kById('kRefId').addEventListener('keydown',e=>{
    if(e.key==='Enter') kById('kLoginBtn').click();
    if(e.key==='Escape') kHideRefereeDialog();
});
kById('kLoginBtn').addEventListener('click',async()=>{
    kById('kLoginStatus').textContent='Verifying...';
    try{
        K_SESSION=await kGet('/api/kyeorugi/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({referee_id:kById('kRefId').value,purpose:pendingRefereeTab})});
        if(pendingRefereeTab==='weigh-in' && !K_SESSION.weighin){throw new Error('Trọng tài chưa được phân công Weigh-in cho giải đấu này.');}
        kHideRefereeDialog();
        await kRefreshSession();await kOpenAuthorizedView();
    }catch(e){kById('kLoginStatus').textContent=e.message;}
});
['kCountryFilter','kAgeFilter','kGenderFilter'].forEach(id=>{
    kById(id).addEventListener('change',()=>{kRenderFilters();kRenderRows();});
});
['kWeightFilter','kSearchFilter'].forEach(id=>{
    kById(id).addEventListener(id==='kSearchFilter'?'input':'change',kRenderRows);
});


</script></body></html>'''

HOME_HTML = r"""<!doctype html>
<html lang="vi"><head>
<link rel="icon" type="image/png" sizes="any" href="/referee-logo?v=20261118" />
<link rel="apple-touch-icon" href="/referee-logo?v=20261118" /><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Taekwondo Referee Portal — Select Event</title>
<style>
*{box-sizing:border-box}
:root{--gold:#ffd34d;--navy:#07111f;--blue:#123c67;--line:#315879}
html{min-height:100%}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;background:var(--navy);color:#f2f8ff;font-family:Arial,Helvetica,sans-serif}
/* Matches Sigma View's background treatment: full artwork visible at 50% opacity. */
body:before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background-image:url("{{ url_for('referee_background') }}");background-size:cover;background-position:center;background-repeat:no-repeat;opacity:.50}
body>header,body>main,body>footer{position:relative;z-index:1}
header{min-height:116px;background:linear-gradient(100deg,#03152d,#084b8a,#03152d);border-bottom:3px solid var(--gold);padding:14px 22px;display:grid;grid-template-columns:150px minmax(0,1fr) 150px;align-items:center;gap:12px}
.logo-wrap{display:flex;align-items:center;justify-content:flex-start;width:124px;min-height:76px}.logo{max-width:124px;max-height:78px;object-fit:contain;background:#fff;border-radius:5px;padding:3px}
.logo-fallback{font-size:29px;font-weight:900;font-style:italic;letter-spacing:-3px;color:white;text-shadow:2px 2px #f53c3c}
h1{margin:0;color:#ffae39;text-shadow:0 2px 0 #081e3c,0 4px 12px #0008;letter-spacing:.8px;font-size:clamp(20px,2.5vw,36px);text-align:center}
.clock{border:5px solid var(--gold);border-radius:18px;text-align:center;padding:9px 6px;font-weight:800;background:#162e4c;font-size:20px;font-variant-numeric:tabular-nums}
main{width:100%;max-width:1230px;margin:30px auto 0;padding:0 18px;flex:1}
.section-top{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:18px;flex-wrap:wrap}
.page-title{color:var(--gold);font-size:29px;font-weight:900;margin:0 0 7px;letter-spacing:.4px}
.subtitle{margin:0;color:#cee4fa;font-size:14px;letter-spacing:.2px}
.portal-chip{display:inline-flex;align-items:center;gap:9px;color:#fbe29b;background:rgba(9,35,59,.90);padding:9px 15px;border:1px solid #7d6729;border-radius:999px;font-size:12px;font-weight:800;letter-spacing:1.2px}
.portal-chip:before{content:"";width:8px;height:8px;border-radius:50%;background:#ffd34d;box-shadow:0 0 12px #ffd34d}
.event-list{display:flex;flex-direction:column;gap:15px}
.event-card{position:relative;isolation:isolate;display:grid;grid-template-columns:minmax(0,1fr) 205px;gap:12px;min-height:145px;overflow:hidden;border:1px solid #3a607e;border-radius:16px;background:linear-gradient(108deg,rgba(10,32,56,.98) 0%,rgba(13,43,72,.98) 62%,rgba(17,53,86,.94) 100%);box-shadow:0 9px 24px #0006;transition:transform .2s,border-color .2s,box-shadow .2s}
.event-card:hover,.event-card:focus-within{transform:translateY(-3px);border-color:var(--gold);box-shadow:0 14px 35px #0009,0 0 0 1px #ffdf6766}
.event-card:before{content:"";position:absolute;pointer-events:none;inset:0;z-index:-1;background:radial-gradient(ellipse at 80% 25%,rgba(45,139,232,.22),transparent 42%)}
.event-card:after{content:"";position:absolute;pointer-events:none;top:-48px;right:135px;width:245px;height:245px;z-index:-1;background:url("{{ url_for('referee_logo') }}") center/contain no-repeat;opacity:.075;filter:grayscale(1) brightness(2)}
.event-accent{position:absolute;top:0;left:0;bottom:0;width:5px;background:linear-gradient(180deg,#fff5ab,#ffd34d,#a47e1e)}
.event-info{min-width:0;padding:21px 16px 20px 25px;display:flex;flex-direction:column;justify-content:center;gap:10px}
.event-name{margin:0;font-size:clamp(19px,2vw,25px);color:#ffd34d;font-weight:900;line-height:1.22;overflow-wrap:anywhere;text-shadow:0 2px 6px #0009}
.event-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px 19px;color:#d5e5f6;font-size:14px;line-height:1.5}
.event-meta span{display:inline-flex;align-items:baseline;gap:7px;min-width:0}.event-meta .sym{color:#ffc85a}
.event-code{font-size:11px;color:#8fc4ea;font-weight:800;letter-spacing:1px}
.event-side{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:14px;padding:18px 21px 18px 0;z-index:1}
.status{display:flex;align-items:center;justify-content:center;min-height:42px;text-align:center;border-radius:11px;padding:9px 12px;font-size:13px;font-weight:900;line-height:1.2;letter-spacing:.1px;box-shadow:0 2px 9px #0003}
.status-upcoming{background:#fff2a4;color:#624300}.status-open{background:#c5f4cc;color:#08612e}.status-closed{background:#fbd0d3;color:#8e2028}.status-ongoing{background:#ffdbac;color:#8c420a}.status-finished{background:#d8dce1;color:#424b54}
.enter{display:flex;align-items:center;justify-content:center;min-height:43px;gap:8px;padding:10px 12px;border:1px solid #ffe784;border-radius:11px;background:linear-gradient(180deg,#ffe783,#f6be37);color:#091d31;font-size:13px;font-weight:900;text-decoration:none;white-space:nowrap;box-shadow:0 3px 10px #0003;transition:filter .15s,transform .15s}
.enter:hover{filter:brightness(1.1)}.enter:active{transform:translateY(1px)}.enter:focus-visible{outline:3px solid #fff;outline-offset:3px}
.empty{padding:25px;border:1px solid #345875;border-radius:10px;background:#0d2238;color:#b8d0e6}
footer{margin-top:28px;text-align:center;color:#b9d1e8;font-size:13px;padding:15px 15px;background:linear-gradient(180deg,#081b30,#061323);border-top:1px solid #315879;line-height:1.0}
.footer-name{font-size:17px;font-weight:900;color:#ffd34d;letter-spacing:.7px}.footer-quote{font-size:14px;color:#d9eaff;margin:6px auto;max-width:740px}
.footer-contact{margin-top:7px}.footer-contact a{color:#83d7ff;text-decoration:none;font-weight:700;overflow-wrap:anywhere}.footer-contact a:hover{text-decoration:underline}.footer-bottom{margin-top:9px;color:#829fb9;font-size:11px}
@media(max-width:700px){header{grid-template-columns:70px minmax(0,1fr) 102px;min-height:90px;padding:9px 8px;gap:4px}.logo-wrap{width:62px;min-height:44px}.logo{max-width:62px;max-height:50px}.logo-fallback{font-size:20px}.clock{font-size:11px;padding:7px 3px}h1{font-size:clamp(15px,4vw,23px)}main{margin-top:18px}.page-title{font-size:23px}.event-card{grid-template-columns:1fr;gap:0}.event-info{padding:19px 18px 11px 23px}.event-side{padding:0 18px 18px 23px;display:grid;grid-template-columns:1fr 1fr;gap:10px}.enter{min-width:0}.event-card:after{right:-25px;top:-42px}}
@media(max-width:420px){.event-side{grid-template-columns:1fr}.portal-chip{display:none}.event-meta{display:block}.event-meta span{display:flex;margin-top:4px}}
@media(prefers-reduced-motion:reduce){.event-card,.enter{transition:none}}
</style></head><body>
<header><div class="logo-wrap"><img class="logo" src="{{ url_for('referee_logo') }}" alt="NTP" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"><span class="logo-fallback" style="display:none">NTP</span></div>
<h1>TAEKWONDO REFEREE PORTAL</h1><div class="clock"><div id="date">--/--/----</div><div id="time">--:--:--</div></div></header>
<main><div class="section-top"><div><h2 class="page-title">SELECT EVENT</h2><p class="subtitle">Referee List · Mission · Weigh-in · Poomsae Score</p></div><div class="portal-chip">OFFICIAL REFEREE PORTAL</div></div>
<div class="event-list">
{% for e in events %}
<article class="event-card"><span class="event-accent" aria-hidden="true"></span>
<div class="event-info"><h3 class="event-name">{{ e.name }}</h3><div class="event-meta"><span><b class="sym">▣</b> {{ e.dates }}</span>{% if e.location %}<span><b class="sym">⌖</b> {{ e.location }}</span>{% endif %}</div><div class="event-code">EVENT CODE: {{ e.code }}</div></div>
<div class="event-side"><div class="status status-{{ e.status_key }}">{{ e.status }}</div><a class="enter" href="/{{ e.ref_slug }}" aria-label="Enter Referee Portal for {{ e.name }}">ENTER PORTAL <span aria-hidden="true">➜</span></a></div>
</article>
{% else %}<div class="empty">Chưa tải được danh sách giải đấu từ Supabase. Kiểm tra kết nối và quyền truy cập.</div>{% endfor %}
</div></main>
<footer role="contentinfo"><div class="footer-name">Taekwondo Event &amp; Competition Management Platform</div>
<div class="footer-quote">Connecting Passion · Honoring Sportsmanship · Shaping Taekwondo’s Future</div>
<div>Created and Designed by <strong style="color:#fff">Nguyễn Thiên Phụng</strong></div>
<div class="footer-contact">Contact: <a href="mailto:nhoctotokute93@gmail.com">nhoctotokute93@gmail.com</a></div></footer>
<script>
function tick(){const d=new Date();document.getElementById('date').textContent=new Intl.DateTimeFormat('vi-VN',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'Asia/Ho_Chi_Minh'}).format(d);document.getElementById('time').textContent=new Intl.DateTimeFormat('vi-VN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Ho_Chi_Minh'}).format(d)}tick();setInterval(tick,1000);
</script></body></html>"""
_LOGIN_ATTEMPTS = {}

# ===== REFEREE EVENT PORTAL =====
EVENT_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
def _all_public_events():
    if supabase is None: return []
    try:
        response = (supabase.table('sigma_events')
                    .select('event_code,event_name,setup,is_active,updated_at,registration_open_at,registration_close_at')
                    .order('updated_at', desc=True).limit(200).execute())
        return response.data or []
    except Exception as exc:
        app.logger.warning('Load referee events failed: %s', type(exc).__name__)
        return []

def _public_slug(row):
    setup=row.get('setup') if isinstance(row.get('setup'),dict) else {}
    raw=str(setup.get('public_slug') or row.get('event_code') or '').strip().lower()
    return raw if EVENT_SLUG_RE.fullmatch(raw) else ''

def _find_public_event(slug):
    if not EVENT_SLUG_RE.fullmatch(slug or ''): return None
    return next((row for row in _all_public_events() if _public_slug(row)==slug),None)

def get_legacy_event_code():
    return os.environ.get('LEGACY_SIGMA_EVENT_CODE','TRE_TQ_2026').strip()

@app.get('/referee-background')
def referee_background():
    # Use local static assets; never expose .env or arbitrary project files.
    for folder,name in [('icons','TKD_web.png'),('icon','TKD_web.png')]:
        path=_PROJECT_ROOT/folder/name
        if path.is_file(): return send_from_directory(str(path.parent),path.name)
    abort(404)

@app.get('/referee-logo')
def referee_logo():
    # Same lookup policy as Sigma portal; do not expose arbitrary files.
    for folder,name in (
        ('icon','NTP.png'), ('icons','NTP.png'),
        ('icon','ntp.png'), ('icons','ntp.png'),
        ('icons','main.png'), ('icon','main.png'),
        ('icons','logo.png'), ('icon','logo.png'),
        ('icons','Logo.png'), ('icon','Logo.png'),
        ('icon','NTP.jpg'), ('icons','NTP.jpg'),
    ):
        path=_PROJECT_ROOT/folder/name
        if path.is_file(): return send_from_directory(str(path.parent),path.name)
    abort(404)

@app.get('/api/referees')
@_require_referee
def referee_list_api():
    try:
        db=_kyeorugi_client()
        rows=[];offset=0
        while True:
            batch=(db.table(K_REFEREES).select('"No.Referee","ID.Referee",Fullname,NOC,event_code')
                   .eq('event_code',_request_event_code()).order('"No.Referee"')
                   .range(offset,offset+499).execute().data or [])
            rows.extend(batch)
            if len(batch)<500:break
            offset+=500
        # Return only public roster fields, never DOB, access credentials or duty flags.
        return jsonify([{'No.Referee':r.get('No.Referee'), 'NOC':r.get('NOC') or '',
                         'Fullname':r.get('Fullname') or ''} for r in rows])
    except Exception as exc:
        app.logger.warning('Referee roster failed: %s',type(exc).__name__)
        return _api_error('Không tải được danh sách trọng tài.',503)

@app.get('/api/referee/me')
@_require_referee
def referee_me_api():
    try:
        rows=(_kyeorugi_client().table(K_REFEREES).select('"No.Referee","ID.Referee",Fullname,BOD,Gender,NOC,event_code')
              .eq('event_code',_request_event_code())
              .eq('"ID.Referee"',session['k_referee_id']).limit(1).execute().data or [])
        if not rows:return _api_error('Không tìm thấy trọng tài trong giải.',404)
        return jsonify(rows[0])
    except Exception as exc:
        app.logger.warning('Referee profile failed: %s',type(exc).__name__)
        return _api_error('Không đọc được thông tin trọng tài.',503)

@app.get('/api/mission')
@_require_referee
def mission_api():
    # Current schema exposes duty flags, not a confirmed per-day assignment table.
    # Do not fabricate Day/Court/Match or claim daily assignments exist.
    try:
        rows=(_kyeorugi_client().table(K_REFEREES).select('*')
              .eq('"ID.Referee"',session.get('k_referee_id')).eq('event_code',_request_event_code()).limit(1).execute().data or [])
        if not rows: return _api_error('Không tìm thấy trọng tài.',404)
        referee=rows[0]
        duties=[{'role':role,'assigned':referee.get(role)} for role in K_ROLES
                if str(referee.get(role) or '').strip().casefold() in ('ok','x','v')]
        return jsonify({'referee':session.get('k_referee_name'),'duties':duties,
                        'daily_schedule_available':False,
                        'message':'Chưa có dữ liệu phân công theo từng ngày; hiện hiển thị nhiệm vụ được giao trong Referee Lists.'})
    except Exception as exc:
        app.logger.warning('Mission failed: %s',type(exc).__name__)
        return _api_error('Không tải được Mission.',503)

# ===== Division Timeline Admin: server-side authorization, JSON document and schedule catalog =====
import hmac
import hashlib

DT_TABLE = 'division_timeline_admin'
DT_LOGIN_ATTEMPTS = {}  # per client, in-memory throttling; production requires shared rate limiting
DT_ADMIN_ID = 'Nguyenthienphung93'
DT_PASSWORD_HASH = '1f34337b46cefd52e80a494b56442d686b6952999ab4dd7bdf90dd7905019248'  # PBKDF2-SHA256 (240k iterations); plaintext password not saved
DT_PASSWORD_SALT = b'ntp.timeline.admin.v1'
DT_PHASE_RANK = {'R128':0,'R64':1,'R32':2,'R16':3,'QUAR':4,'SEMI':5,'FINAL':6}


def _dt_authorized():
    return session.get('dt_admin_event') == _request_event_code() and bool(session.get('dt_admin_csrf'))


def _dt_admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _dt_authorized():
            return _api_error('Yêu cầu đăng nhập Admin Division Timelines.', 401)
        if request.method != 'GET' and request.headers.get('X-Timeline-CSRF') != session.get('dt_admin_csrf'):
            return _api_error('CSRF token không hợp lệ.', 403)
        return fn(*args, **kwargs)
    return wrapped


def _dt_read():
    rows = (_kyeorugi_client().table(DT_TABLE)
            .select('event_code,day_count,slots,revision')
            .eq('event_code', _request_event_code()).limit(1).execute().data or [])
    return rows[0] if rows else None


def _dt_phase(raw):
    txt = re.sub(r'[^A-Z0-9]+', '', str(raw or '').upper())
    aliases = {'ROUND128':'R128','ROUND64':'R64','ROUND32':'R32','ROUND16':'R16',
               'QUARTER':'QUAR','QUARTERFINAL':'QUAR','QUARTERFINALS':'QUAR',
               'QUAR':'QUAR','QF':'QUAR','SEMIFINAL':'SEMI','SEMIFINALS':'SEMI',
               'SF':'SEMI','FINALS':'FINAL','F':'FINAL'}
    return aliases.get(txt, txt if txt in DT_PHASE_RANK else '')


def _dt_code_parts(row):
    # A match code is DAY digit + COURT digit + 3-digit sequence, such as 11001.
    match = str(row.get('Match') or row.get('Match No') or row.get('Match Number') or '').strip()
    m = re.fullmatch(r'(\d)(\d)(\d{3})',match)
    if m and int(m.group(1))>0 and int(m.group(2))>0:
        return int(m.group(1)),int(m.group(2)),match
    try:
        d = int(row.get('Day') or row.get('Days') or 0)
        c = int(row.get('Court') or 0)
        if 1<=d<=31 and 1<=c<=10: return d,c,match
    except (ValueError,TypeError): pass
    return None


def _dt_catalog():
    # Only real scheduled brackets are selectable. No fabricated division or phase.
    grouped = {}
    for discipline, table in [('Kyorugi','Kyeorugi Matchs'),('Poomsae',POOMSAE_MATCH_TABLE)]:
        db = _kyeorugi_client() if discipline=='Kyorugi' else (supabase or _kyeorugi_client())
        offset=0
        while offset < 100000:
            batch=(db.table(table).select('*').eq('event_code',_request_event_code())
                   .range(offset,offset+499).execute().data or [])
            for row in batch:
                div=str(row.get('Division') or row.get('division') or '').strip()
                phase=_dt_phase(row.get('Phase') or row.get('phase'))
                loc=_dt_code_parts(row)
                if not div or not phase or not loc: continue
                day,court,code=loc
                key=(discipline,div,phase,day,court)
                raw_style=str(row.get('Poomsae Type') or row.get('Poomsae type') or row.get('Type') or '').lower()
                display_type=('Kyorugi' if discipline=='Kyorugi' else
                              'Freestyle' if 'freestyle' in (raw_style+' '+div.lower()) else
                              'Demonstration' if 'demonstration' in (raw_style+' '+div.lower()) else
                              'Poomsae')
                info=grouped.setdefault(key,{'discipline':discipline,'display_type':display_type,'division':div,'phase':phase,
                                             'day':day,'court':court,'matches':[]})
                if code: info['matches'].append(code)
            if len(batch)<500:break
            offset+=500
    items=list(grouped.values())
    for item in items:
        item['matches']=sorted(set(item['matches']))
        item['label']=item['phase']+' · '+item['division']+' ('+item['discipline']+')'
    return sorted(items,key=lambda x:(x['day'],x['court'],DT_PHASE_RANK[x['phase']],x['division']))


@app.post('/api/division-timelines/admin/login')
def division_timeline_admin_login():
    body=request.get_json(silent=True) or {}
    who=str(request.remote_addr or '?')
    now=time.monotonic()
    tries=[t for t in DT_LOGIN_ATTEMPTS.get(who,[]) if now-t<900]
    DT_LOGIN_ATTEMPTS[who]=tries
    if len(tries)>=8:return _api_error('Đăng nhập nhiều lần. Vui lòng thử sau.',429)
    user=str(body.get('id') or '')
    password=str(body.get('password') or '')
    valid=hmac.compare_digest(user,DT_ADMIN_ID) and hmac.compare_digest(
        hashlib.pbkdf2_hmac('sha256',password.encode(),DT_PASSWORD_SALT,240000).hex(),DT_PASSWORD_HASH)
    if not valid:
        tries.append(now)
        return _api_error('ID hoặc mật khẩu không đúng.',403)
    # Do not log out any Referee session: separate admin permissions inside the same cookie.
    session['dt_admin_event']=_request_event_code()
    session['dt_admin_csrf']=token_urlsafe(32)
    return jsonify({'ok':True,'csrf':session['dt_admin_csrf']})


@app.post('/api/division-timelines/admin/logout')
@_dt_admin_required
def division_timeline_admin_logout():
    session.pop('dt_admin_event',None);session.pop('dt_admin_csrf',None)
    return jsonify({'ok':True})


@app.get('/api/division-timelines/admin/catalog')
@_dt_admin_required
def division_timeline_admin_catalog():
    try:return jsonify({'items':_dt_catalog()})
    except Exception as exc:
        app.logger.exception('Timeline scheduled matches query failed')
        return _api_error('Không tải được lịch trận. Kiểm tra event_code và bảng Matchs.',503)


def _dt_validate(body, catalog):
    count=body.get('day_count')
    if type(count) is not int or not 1<=count<=31: raise ValueError('Số Day phải từ 1 đến 31.')
    slots=body.get('slots')
    if not isinstance(slots,list) or len(slots)>600:raise ValueError('Số khung thời gian không hợp lệ.')
    known={(r['discipline'],r['division'],r['phase'],r['day'],r['court']) for r in catalog}
    seen=set(); assigned_keys=set(); result=[]; phases={}
    for slot in slots:
        if not isinstance(slot,dict):raise ValueError('Khung giờ không hợp lệ.')
        sid=str(slot.get('id') or '')
        day=slot.get('day'); section=slot.get('session')
        start=str(slot.get('start') or ''); end=str(slot.get('end') or '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{4,60}',sid) or sid in seen:raise ValueError('Mã khung giờ trùng hoặc không hợp lệ.')
        seen.add(sid)
        if type(day) is not int or not 1<=day<=count:raise ValueError('Day không hợp lệ.')
        kind=str(slot.get('kind') or 'match')
        if kind not in ('match','lunch','break'):raise ValueError('Loại khung giờ không hợp lệ.')
        if section not in ('Morning','Evening','Break'):raise ValueError('Morning/Evening/Break không hợp lệ.')
        if kind in ('lunch','break') and section!='Break':raise ValueError('Lunch/Break phải thuộc loại Break.')
        if kind=='match' and section not in ('Morning','Evening'):raise ValueError('Time Slot phải thuộc Morning/Evening.')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',start) or not (re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',end) or end=='24:00') or start>=end:
            raise ValueError('Giờ bắt đầu/kết thúc không hợp lệ.')
        assigned=slot.get('assignments') or {}
        if not isinstance(assigned,dict) or len(assigned)>10:raise ValueError('Số sân không hợp lệ.')
        if kind in ('lunch','break') and assigned:raise ValueError('Lunch/Break không được chứa lịch trận.')
        cleaned={}
        for court_key,val in assigned.items():
            if not str(court_key).isdigit() or not 1<=int(court_key)<=10:raise ValueError('Court phải từ 1 đến 10.')
            # Backward compatible: legacy single dict, or new list of divisions.
            values=val if isinstance(val,list) else [val]
            if not values or len(values)>100:raise ValueError('Số nội dung trong một ô không hợp lệ.')
            clean_values=[]
            for item in values:
                if not isinstance(item,dict):raise ValueError('Nội dung không hợp lệ.')
                discipline=str(item.get('discipline') or '')
                division=str(item.get('division') or '')
                phase=_dt_phase(item.get('phase'))
                key=(discipline,division,phase,day,int(court_key))
                if key not in known:
                    raise ValueError('Nội dung/Vòng chưa được set số trận cho đúng Day và Court.')
                if key in assigned_keys:
                    raise ValueError('Nội dung '+division+' / '+phase+' đã được xếp trong một Time Slot khác.')
                assigned_keys.add(key)
                # Trust the scheduled catalog, not a client-supplied display type.
                scheduled=next((x for x in catalog if (x['discipline'],x['division'],x['phase'],x['day'],x['court'])==key),None)
                clean_values.append({'discipline':discipline,'division':division,'phase':phase,
                                     'display_type':(scheduled or {}).get('display_type',discipline)})
                phases.setdefault((discipline,division),[]).append((day,start,end,phase))
            cleaned[str(int(court_key))]=clean_values
        result.append({'id':sid,'day':day,'kind':kind,'session':section,'start':start,'end':end,'assignments':cleaned})
    # A division's later rounds must not precede an earlier round in the published plan.
    for key,items in phases.items():
        for idx,(day,st,en,phase) in enumerate(items):
            for d2,s2,e2,p2 in items[idx+1:]:
                rank1,rank2=DT_PHASE_RANK[phase],DT_PHASE_RANK[p2]
                if rank1==rank2:continue
                if rank1<rank2 and (day, en)>(d2,s2):
                    raise ValueError('Sai thứ tự vòng: '+key[1]+' '+phase+' phải kết thúc trước '+p2)
                if rank2<rank1 and (d2,e2)>(day,st):
                    raise ValueError('Sai thứ tự vòng: '+key[1]+' '+p2+' phải kết thúc trước '+phase)
    for day in range(1,count+1):
        if sum(1 for s in result if s['day']==day and s['kind']=='lunch')>1:
            raise ValueError('Mỗi Day chỉ được có một Lunch Time.')
        lunch=next((s for s in result if s['day']==day and s['kind']=='lunch'),None)
        if lunch:
            for slot in result:
                if slot['day']!=day or slot['kind']!='match':continue
                slot['session']='Evening' if slot['start']>=lunch['end'] else 'Morning'
        byday=sorted((s for s in result if s['day']==day),key=lambda x:x['start'])
        for a,b in zip(byday,byday[1:]):
            if a['end']>b['start']:raise ValueError('Khung giờ Day '+str(day)+' bị chồng thời gian.')
    return count,result


@app.put('/api/division-timelines/admin/save')
@_dt_admin_required
def division_timeline_admin_save():
    body=request.get_json(silent=True) or {}
    try:
        catalog=_dt_catalog()
        count,slots=_dt_validate(body,catalog)
        db=_kyeorugi_client()
        existing=_dt_read()
        expected=body.get('revision')
        if expected != (existing['revision'] if existing else 0):
            return _api_error('Lịch đã được thay đổi ở một phiên khác. Tải lại trước khi lưu.',409)
        payload={'day_count':count,'slots':slots,'revision':(existing['revision']+1 if existing else 1)}
        if existing:
            rows=(db.table(DT_TABLE).update(payload).eq('event_code',_request_event_code())
                  .eq('revision',expected).execute().data or [])
        else:
            rows=(db.table(DT_TABLE).insert({'event_code':_request_event_code(),**payload}).execute().data or [])
        if len(rows)!=1:return _api_error('Lịch vừa thay đổi. Vui lòng tải lại.',409)
        return jsonify({'ok':True,'revision':payload['revision']})
    except ValueError as exc:return _api_error(str(exc),400)
    except Exception as exc:
        app.logger.error('Timeline save failed: %s',type(exc).__name__)
        return _api_error('Không lưu được Division Timelines. Cần chạy file SQL và kiểm tra service-role Supabase.',503)


def _dt_day_court_max():
    # Determine the highest Court actually scheduled for each day; skipped Courts
    # remain visible inside that range (Court 1,3,4 => show 1..4).
    try:
        result={}
        for x in _dt_catalog():
            day=str(x['day'])
            result[day]=max(result.get(day,1),int(x['court']))
        return result
    except Exception:
        app.logger.warning('Court count lookup unavailable; using published slots')
        return {}


@app.get('/api/division-timelines')
def division_timelines_api():
    """Public read-only; admins use the same content after saving."""
    try:
        event_code=_request_event_code()
        managed=_dt_read()
        if managed:
            return jsonify({'mode':'managed','days':managed['day_count'],
                            'slots':managed['slots'] or [],'revision':managed['revision'],
                            'event_code':event_code,
                            'court_max_by_day':_dt_day_court_max()})
        # Legacy schedule remains visible until an admin publishes a new schedule.
        event=_find_public_event(getattr(g,'event_prefix','/').strip('/'))
        setup=(event or {}).get('setup') or {}
        start=parse_event_date(setup.get('date_from') or setup.get('start_date'))
        end=parse_event_date(setup.get('date_to') or setup.get('end_date'))
        days=max(1,min(31,(end-start).days+1)) if start and end and end>=start else 1
        all_rows=[];offset=0
        while True:
            batch=(supabase.table('division_timelines').select('day_number,session,court,start_slot,slot_span,division')
                   .eq('event_code',event_code).order('day_number').order('session').order('court').order('start_slot')
                   .range(offset,offset+499).execute().data or [])
            all_rows.extend(batch)
            if len(batch)<500:break
            offset+=500
        if all_rows:days=min(31,max(days,max(int(r['day_number']) for r in all_rows)))
        return jsonify({'mode':'legacy','days':days,'items':all_rows,'event_code':event_code,'revision':0})
    except Exception as exc:
        app.logger.warning('Division Timelines error: %s',type(exc).__name__)
        return _api_error('Không đọc được Division Timelines. Chạy SQL migration trước.',503)


@app.before_request
def scope_referee_event():
    if request.path.startswith('/api/'):
        return jsonify({'error':'Chọn Event trước khi sử dụng.'}),403

# Event-scoped APIs only, registered to original functions with no duplication.
for idx,rule in enumerate(list(app.url_map.iter_rules())):
    if not (rule.rule.startswith('/api/') and rule.endpoint in app.view_functions):
        continue
    original=app.view_functions[rule.endpoint]
    def scoped(event_slug,_original=original,**kwargs):
        event=_find_public_event(event_slug)
        if not event: return jsonify({'error':'Event Code không tồn tại.'}),404
        g.event_code=str(event['event_code'])
        g.event_prefix='/'+event_slug
        return _original(**kwargs)
    methods=[m for m in rule.methods if m not in ('OPTIONS','HEAD')]
    app.add_url_rule('/<event_slug>'+rule.rule,'ref_scoped_'+str(idx),scoped,methods=methods)


REFEREE_TZ = ZoneInfo('Asia/Ho_Chi_Minh')

def _referee_datetime(value, *, end_of_day=False):
    if isinstance(value, datetime):
        result=value
    else:
        raw=str(value or '').strip()
        if not raw: return None
        try: result=datetime.fromisoformat(raw.replace('Z','+00:00'))
        except ValueError: result=None
        if result is None:
            for fmt in ('%d/%m/%Y %H:%M','%d/%m/%Y %H:%M:%S',
                        '%Y-%m-%d %H:%M','%d/%m/%Y','%d-%m-%Y','%Y-%m-%d'):
                try:
                    result=datetime.strptime(raw,fmt)
                    break
                except ValueError: continue
        if result is None: return None
        if end_of_day and len(raw)<=10: result=result.replace(hour=23,minute=59,second=59)
    return result.replace(tzinfo=REFEREE_TZ) if result.tzinfo is None else result.astimezone(REFEREE_TZ)


def _referee_status(now,start,end,registration_open,registration_close):
    # Same status priority as the Sigma home.
    if end and now>end: return 'finished','Finished'
    if start and end and start<=now<=end: return 'ongoing','In Progress'
    if registration_open and registration_close and registration_open<=now<=registration_close: return 'open','Registration Open'
    if registration_close and now>registration_close: return 'closed','Registration Closed'
    return 'upcoming','Upcoming'

@app.get('/')
def referee_home():
    now=datetime.now(REFEREE_TZ)
    events=[]
    for row in _all_public_events():
        s=row.get('setup') if isinstance(row.get('setup'),dict) else {}
        start_raw=s.get('date_from') or s.get('start_date') or ''
        end_raw=s.get('date_to') or s.get('end_date') or ''
        start=_referee_datetime(start_raw)
        end=_referee_datetime(end_raw,end_of_day=True)
        registration_open=_referee_datetime(row.get('registration_open_at') or s.get('registration_open_at') or s.get('registration_open'))
        registration_close=_referee_datetime(row.get('registration_close_at') or s.get('registration_close_at') or s.get('registration_close'))
        key,label=_referee_status(now,start,end,registration_open,registration_close)
        if not _public_slug(row):
            app.logger.warning('Skipping event with invalid public slug: %s',str(row.get('event_code'))[:80])
            continue
        events.append({
            'name':row.get('event_name') or 'Giải chưa đặt tên',
            'ref_slug':_public_slug(row), 'code':row.get('event_code') or '',
            'dates':format_event_date_range(start_raw,end_raw) or 'Chưa thiết lập',
            'location':s.get('location') or s.get('venue') or '',
            'status_key':key,'status':label,
            '_sort':(1 if end and now>end+timedelta(days=3) else 0,
                     start or datetime.max.replace(tzinfo=REFEREE_TZ),str(row.get('event_name') or ''))
        })
    events.sort(key=lambda e:e['_sort'])
    return render_template_string(HOME_HTML,events=events)

# The Judge WSGI router below serves this URL DIRECTLY (no external domain).
# This endpoint is a helpful error page when the Judge module failed to initialize.
@app.get('/<event_slug>/Poomsae-score')
@app.get('/<event_slug>/Poomsae-score/')
def referee_poomsae_score(event_slug):
    if not _find_public_event(event_slug):
        abort(404)
    return ("Poomsae Judge could not start. Check Sublime console for "
            "[POOMSAE-JUDGE] initialization error.", 503)

@app.get('/<event_slug>')
def referee_event(event_slug):
    event=_find_public_event(event_slug)
    if not event: abort(404)
    g.event_code=str(event['event_code']);g.event_prefix='/'+event_slug
    setup=event.get('setup') if isinstance(event.get('setup'),dict) else {}
    date_from=setup.get('date_from') or setup.get('start_date')
    date_to=setup.get('date_to') or setup.get('end_date')
    return render_template_string(REFEREE_HTML,event_name=setup.get('event') or event.get('event_name') or 'TAEKWONDO',
        event_date=format_event_date_range(date_from,date_to),
        event_location=setup.get('location') or '',event_code=event['event_code'],
        event_slug=event_slug,event_prefix='/'+event_slug)

# ==========================================================================
# POOMSAE JUDGE: SAME DOMAIN, SAME PORT, EVENT-SCOPED ROUTES
# Browser: /tre_tq_2026/Poomsae-score/   (no redirect to score.*)
# WSGI SCRIPT_NAME makes original Judge /login, /api/* and static paths work.
# ==========================================================================
_JUDGE_MODULE = None
_JUDGE_APP = None
_JUDGE_ERROR = None

def _mount_poomsae_judge():
    global _JUDGE_MODULE, _JUDGE_APP, _JUDGE_ERROR
    import importlib.util
    import traceback
    judge_path = _PROJECT_ROOT / 'poomsae modules' / 'app_web.py'
    try:
        if not judge_path.is_file():
            raise FileNotFoundError(f'Missing Judge Web file: {judge_path}')
        spec = importlib.util.spec_from_file_location('poomsae_judge_embedded', str(judge_path))
        if spec is None or spec.loader is None:
            raise RuntimeError('Unable to load Judge Web module')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        judge_app = module.app
        judge_app.config['SESSION_COOKIE_NAME'] = 'poomsae_judge_session'
        # Keep the Judge cookie separate from the Referee Portal cookie.
        judge_app.config['SESSION_COOKIE_PATH'] = '/'

        @judge_app.before_request
        def _enforce_referee_event_scope():
            target = request.environ.get('REFEREE_JUDGE_EVENT_CODE')
            if not target:
                return None
            if request.path == '/login' and request.method == 'POST':
                if str(request.form.get('event_code', '')).casefold() != target.casefold():
                    abort(403, description='Judge Event Code mismatch')
            logged = str(session.get('event_code') or '')
            if logged and logged.casefold() != target.casefold():
                session.clear()
            return None

        _JUDGE_MODULE, _JUDGE_APP = module, judge_app
        print('[POOMSAE-JUDGE] Ready: event-scoped /<event>/Poomsae-score/ and /Poomsae-score/')
        return True
    except Exception as exc:
        _JUDGE_ERROR = f'{type(exc).__name__}: {exc}'
        print(f'[POOMSAE-JUDGE] Initialization FAILED: {_JUDGE_ERROR}')
        traceback.print_exc()
        return False


_MOUNTED_POOMSAE_JUDGE = _mount_poomsae_judge()

# Verification endpoint for localhost development, no credentials disclosed.
@app.get('/__judge_status')
def referee_judge_status():
    return jsonify({'version': 'V9', 'judge_integrated': bool(_JUDGE_APP),
                    'error': _JUDGE_ERROR if _JUDGE_APP is None else None,
                    'example': '/tre_tq_2026/Poomsae-score/'})

class _JudgeEventRouter:
    def __init__(self, portal_wsgi):
        self.portal_wsgi = portal_wsgi
        self.event_cache = {}

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        # A path with explicit Event slug, e.g. /tre_tq_2026/Poomsae-score/api/time
        match = re.match(r'^/([a-z0-9][a-z0-9_-]{0,79})/Poomsae-score(?=/|$)', path)
        if match:
            if _JUDGE_APP is None:
                return self.portal_wsgi(environ, start_response)
            slug = match.group(1)
            prefix = '/' + slug + '/Poomsae-score'
            suffix = path[len(prefix):] or '/'
            code = self.event_cache.get(slug)
            # Check the selected event on the FIRST page visit, not on every scoring API poll.
            if suffix == '/' and environ.get('REQUEST_METHOD') == 'GET':
                event = _find_public_event(slug)
                if not event:
                    return self.portal_wsgi(environ, start_response)
                code = str(event['event_code'])
                self.event_cache[slug] = code
            if not code:
                # Works after a service restart or direct form POST as well.
                event = _find_public_event(slug)
                if not event:
                    return self.portal_wsgi(environ, start_response)
                code = str(event['event_code'])
                self.event_cache[slug] = code
            request_env = environ.copy()
            request_env['SCRIPT_NAME'] = (environ.get('SCRIPT_NAME') or '') + prefix
            request_env['PATH_INFO'] = suffix
            request_env['REFEREE_JUDGE_EVENT_CODE'] = code
            if suffix == '/' and request_env.get('REQUEST_METHOD') == 'GET':
                from urllib.parse import parse_qsl, urlencode
                qs = dict(parse_qsl(request_env.get('QUERY_STRING', ''), keep_blank_values=True))
                qs['event_code'] = code
                request_env['QUERY_STRING'] = urlencode(qs)
            return _JUDGE_APP.wsgi_app(request_env, start_response)

        # Optional unscoped path for direct application testing.
        if path == '/Poomsae-score' or path.startswith('/Poomsae-score/'):
            if _JUDGE_APP is None:
                return self.portal_wsgi(environ, start_response)
            prefix = '/Poomsae-score'
            request_env = environ.copy()
            request_env['SCRIPT_NAME'] = (environ.get('SCRIPT_NAME') or '') + prefix
            request_env['PATH_INFO'] = path[len(prefix):] or '/'
            return _JUDGE_APP.wsgi_app(request_env, start_response)
        return self.portal_wsgi(environ, start_response)

# The router remains effective under Flask's built-in server AND under Gunicorn.
app.wsgi_app = _JudgeEventRouter(app.wsgi_app)

if __name__=='__main__':
    print('[REFEREE-V9] Running Referee + Judge Web on one port, 5080')
    print('[REFEREE-V9] Check http://127.0.0.1:5080/__judge_status')
    app.run(host='127.0.0.1', port=int(os.environ.get('REFEREE_PORT',5080)), debug=False)
