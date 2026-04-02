from __future__ import annotations

import json
import os
import random
import secrets
import smtplib
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, jsonify
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
APP_ENV = os.environ.get('APP_ENV', 'development').lower()
SECRET_KEY = os.environ.get('SECRET_KEY', 'aoe4it-dev-local-key')
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get('ADMIN_EMAILS', '').split(',') if e.strip()}
BETA_INVITE_CODE = os.environ.get('INVITE_CODE', '').strip()
ENABLE_SHADOW_LAB = os.environ.get('ENABLE_SHADOW_LAB', 'false').lower() == 'true'
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5055').rstrip('/')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
SMTP_HOST = os.environ.get('SMTP_HOST', '').strip()
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '').strip()
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
MAIL_FROM = os.environ.get('MAIL_FROM', SMTP_USERNAME or 'no-reply@aoe4it.local').strip()
RESET_TOKEN_MAX_AGE = int(os.environ.get('RESET_TOKEN_MAX_AGE', '3600'))

def _can_use_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.aoe4it-write-test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def resolve_db_path() -> Path:
    configured = os.environ.get('DATABASE_PATH', '').strip()
    if configured:
        db_path = Path(configured)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    if APP_ENV == 'production' or os.environ.get('RENDER'):
        candidates: list[Path] = []
        render_disk_path = os.environ.get('RENDER_DISK_PATH', '').strip()
        if render_disk_path:
            candidates.append(Path(render_disk_path))

        candidates.extend([
            Path('/var/data'),
            BASE_DIR / '.render_data',
            Path(os.environ.get('TMPDIR', '/tmp')) / 'aoe4it',
        ])

        for data_root in candidates:
            if _can_use_directory(data_root):
                return data_root / 'aoe4it.db'

        raise RuntimeError(
            'Could not find a writable database directory. '
            'Set DATABASE_PATH or RENDER_DISK_PATH to a writable location.'
        )

    return BASE_DIR / 'aoe4it.db'

DB_PATH = resolve_db_path()
DATABASE_PROVIDER = 'sqlite'
DATABASE_DECISION = 'sqlite_for_staging'
SUPPORTED_APP_ENVS = {'development', 'staging', 'production'}

PRACTICE_MAPS = [
    'Dry Arabia', 'Golden Heights', 'Lipany', 'Rocky River',
    'Cliffside', 'Himeyama', 'Canal', 'Prairie',
]

AOE4_CIVS = [
    'Abbasid Dynasty', 'Ayyubids', 'Byzantines', 'Chinese', 'Delhi Sultanate',
    'English', 'French', 'Golden Horde', 'House of Lancaster', 'HRE', 'Japanese',
    "Jeanne d'Arc", 'Knights Templar', 'Macedonian Dynasty', 'Malians', 'Mongols',
    'Order of the Dragon', 'Ottomans', 'Rus', 'Sengoku Daimyo', 'Tughlaq Dynasty',
    "Zhu Xi's Legacy",
]

CIV_FLAG_FILES = {
    'Abbasid Dynasty': 'Abbasid_Dynasty_AoE4.webp',
    'Ayyubids': 'Ayyubids_AoE4.webp',
    'Byzantines': 'Byzantines_AoE4.webp',
    'Chinese': 'Chinese_AoE4.webp',
    'Delhi Sultanate': 'Delhi_Sultanate_AoE4.webp',
    'English': 'English_AoE4.webp',
    'French': 'French_AoE4.webp',
    'Golden Horde': 'Golden_Horde_AoE4.webp',
    'House of Lancaster': 'House_of_Lancaster_AoE4.webp',
    'HRE': 'HRE_AoE4.webp',
    'Japanese': 'Japanese_AoE4.webp',
    "Jeanne d'Arc": 'Jeanne_d_Arc_AoE4.webp',
    'Knights Templar': 'Knights_Templar_AoE4.webp',
    'Macedonian Dynasty': 'Macedonian_Dynasty_AoE4.webp',
    'Malians': 'Malians_AoE4.webp',
    'Mongols': 'Mongols_AoE4.webp',
    'Order of the Dragon': 'Order_of_the_Dragon_AoE4.webp',
    'Ottomans': 'Ottomans_AoE4.webp',
    'Rus': 'Rus_AoE4.webp',
    'Sengoku Daimyo': 'Sengoku_Daimyo_AoE4.webp',
    'Tughlaq Dynasty': 'Tughlaq_Dynasty_AoE4.webp',
    "Zhu Xi's Legacy": 'Zhu_Xis_Legacy_AoE4.webp',
}

TEMPLATES_DIR = BASE_DIR / 'templates'
STATIC_DIR = BASE_DIR / 'static'

if not TEMPLATES_DIR.exists() or not STATIC_DIR.exists():
    raise RuntimeError(
        'Project files are missing. Do not run app.py directly from inside the ZIP preview. '
        'Extract the full archive first, then run the app from the extracted folder so templates/ and static/ are available.'
    )

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

IS_RENDER = bool(os.environ.get('RENDER'))
IS_PRODUCTION = APP_ENV == 'production'
IS_STAGING = APP_ENV == 'staging'
IS_DEPLOYED = IS_RENDER or IS_STAGING or IS_PRODUCTION
SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax').strip() or 'Lax'
if SESSION_COOKIE_SAMESITE not in {'Lax', 'Strict', 'None'}:
    SESSION_COOKIE_SAMESITE = 'Lax'
PERMANENT_SESSION_DAYS = max(1, int(os.environ.get('PERMANENT_SESSION_DAYS', '14')))
if IS_PRODUCTION and SECRET_KEY == 'aoe4it-dev-local-key':
    raise RuntimeError('SECRET_KEY must be set to a strong value in production.')

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE=IS_DEPLOYED,
    PERMANENT_SESSION_LIFETIME=timedelta(days=PERMANENT_SESSION_DAYS),
    PREFERRED_URL_SCHEME='https' if IS_DEPLOYED else 'http',
)

SAFE_HTTP_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}
CSRF_SESSION_KEY = '_csrf_token'


def password_reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config['SECRET_KEY'])


def email_enabled() -> bool:
    return bool((RESEND_API_KEY and MAIL_FROM) or (SMTP_HOST and MAIL_FROM))


def current_email_provider() -> str:
    if RESEND_API_KEY and MAIL_FROM:
        return 'resend'
    if SMTP_HOST and MAIL_FROM:
        return 'smtp'
    return 'disabled'


def can_write_to_directory(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.aoe4it-write-test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def runtime_config_summary() -> dict:
    warnings: list[str] = []
    errors: list[str] = []

    if APP_ENV not in SUPPORTED_APP_ENVS:
        warnings.append(f"APP_ENV '{APP_ENV}' is not one of development, staging, or production.")
    if IS_DEPLOYED and not APP_BASE_URL.startswith('https://'):
        if IS_PRODUCTION:
            errors.append('APP_BASE_URL must use https in production.')
        else:
            warnings.append('APP_BASE_URL should use https on staging/Render so reset links and cookies match the deployed site.')
    if not MAIL_FROM and current_email_provider() != 'disabled':
        errors.append('MAIL_FROM is required when email delivery is enabled.')
    if IS_DEPLOYED and SECRET_KEY == 'aoe4it-dev-local-key':
        warnings.append('SECRET_KEY is still set to the local development default. Replace it before wider testing.')
    if not ADMIN_EMAILS:
        warnings.append('ADMIN_EMAILS is empty, so no account will auto-receive admin access by email.')
    writable, detail = can_write_to_directory(DB_PATH.parent)
    if not writable:
        errors.append(f'Database directory is not writable: {detail}')
    elif not DB_PATH.exists():
        warnings.append('Database file does not exist yet. It will be created on first successful startup.')
    if IS_RENDER and not os.environ.get('RENDER_DISK_PATH') and not os.environ.get('DATABASE_PATH'):
        warnings.append('Render disk path is not explicitly set. The app will use the first writable location from /var/data, the project .render_data folder, or /tmp.')

    return {
        'app_env': APP_ENV,
        'is_deployed': IS_DEPLOYED,
        'database_provider': DATABASE_PROVIDER,
        'database_decision': DATABASE_DECISION,
        'db_path': str(DB_PATH),
        'email_provider': current_email_provider(),
        'warnings': warnings,
        'errors': errors,
    }


def log_audit_event(*, event_type: str, detail: str, user_id: Optional[int] = None, player_id: Optional[int] = None, tournament_id: Optional[int] = None) -> None:
    execute(
        'INSERT INTO audit_log (user_id, player_id, tournament_id, event_type, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, player_id, tournament_id, event_type, detail, now_str()),
    )


def log_email_delivery(*, event_type: str, to_email: str, subject: str, body: str, delivery_status: str, detail: str = '', user_id: Optional[int] = None, player_id: Optional[int] = None, tournament_id: Optional[int] = None, provider: Optional[str] = None) -> None:
    execute(
        'INSERT INTO email_delivery_log (user_id, player_id, tournament_id, event_type, provider, delivery_status, detail, to_email, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, player_id, tournament_id, event_type, provider or current_email_provider(), delivery_status, detail[:2000], to_email, subject, body, now_str()),
    )



def client_ip() -> str:
    forwarded = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
    if forwarded:
        return forwarded
    return (request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown').strip()


def ensure_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_input() -> Markup:
    return Markup(f'<input type="hidden" name="_csrf_token" value="{escape(ensure_csrf_token())}">')


def rotate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def is_same_origin_url(target: Optional[str]) -> bool:
    if not target:
        return False
    try:
        parsed = urlparse(target)
    except ValueError:
        return False
    if not parsed.netloc:
        return parsed.path.startswith('/')
    request_host = request.host.split(':')[0].lower()
    return parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.hostname.lower() == request_host


def request_get_fallback() -> str:
    if request.url_rule and 'GET' in (request.url_rule.methods or set()):
        return request.path
    return url_for('dashboard') if get_current_user() else url_for('login')


def csrf_failure_response():
    flash('Your session expired or the form token was invalid. Refresh and try again.', 'error')
    fallback = request_get_fallback()
    target = request.referrer if is_same_origin_url(request.referrer) else fallback
    return redirect(target)


def validate_csrf_or_reject():
    if request.method in SAFE_HTTP_METHODS or request.endpoint == 'static':
        return None
    sent_token = request.form.get('_csrf_token', '') or request.headers.get('X-CSRF-Token', '')
    expected_token = session.get(CSRF_SESSION_KEY, '')
    if not expected_token or not sent_token or not secrets.compare_digest(str(sent_token), str(expected_token)):
        return csrf_failure_response()
    return None


def rate_limit_bucket(route_key: str, *, scope: str = 'ip', key_fields: Optional[list[str]] = None) -> str:
    parts = [route_key]
    if scope in {'ip', 'ip_user'}:
        parts.append(f'ip:{client_ip()}')
    if scope in {'user', 'ip_user'}:
        user = get_current_user()
        parts.append(f'user:{user["id"]}' if user else 'user:anon')
    for field in key_fields or []:
        raw_value = (request.form.get(field) or request.args.get(field) or '').strip().lower()
        if raw_value:
            parts.append(f'{field}:{raw_value[:120]}')
    return '|'.join(parts)


def check_rate_limit(route_key: str, *, limit: int, window_seconds: int, scope: str = 'ip', key_fields: Optional[list[str]] = None) -> Optional[int]:
    now_ts = int(time.time())
    bucket = rate_limit_bucket(route_key, scope=scope, key_fields=key_fields)
    db = get_db()
    db.execute('DELETE FROM rate_limit_events WHERE expires_at <= ?', (now_ts,))
    count_row = db.execute(
        'SELECT COUNT(*) AS c, MIN(expires_at) AS earliest_expiry FROM rate_limit_events WHERE route_key = ? AND bucket_key = ? AND expires_at > ?',
        (route_key, bucket, now_ts),
    ).fetchone()
    if count_row['c'] >= limit:
        earliest_expiry = count_row['earliest_expiry'] or now_ts + window_seconds
        return max(1, int(earliest_expiry) - now_ts)
    db.execute(
        'INSERT INTO rate_limit_events (route_key, bucket_key, created_at, expires_at) VALUES (?, ?, ?, ?)',
        (route_key, bucket, now_ts, now_ts + window_seconds),
    )
    db.commit()
    return None


def rate_limit(limit: int, window_seconds: int, *, scope: str = 'ip', key_fields: Optional[list[str]] = None, methods: tuple[str, ...] = ('POST',)):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method in methods:
                retry_after = check_rate_limit(view.__name__, limit=limit, window_seconds=window_seconds, scope=scope, key_fields=key_fields)
                if retry_after is not None:
                    flash(f'Too many attempts. Please wait about {retry_after} seconds and try again.', 'error')
                    fallback = request_get_fallback()
                    target = request.referrer if is_same_origin_url(request.referrer) else fallback
                    return redirect(target)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def send_app_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    if RESEND_API_KEY and MAIL_FROM:
        payload = json.dumps({
            'from': MAIL_FROM,
            'to': [to_email],
            'subject': subject,
            'text': body,
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.resend.com/emails',
            data=payload,
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status in (200, 201, 202), str(resp.status)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            app.logger.exception('Resend API send failed: %s', detail)
            return False, detail or str(exc)
        except Exception as exc:
            app.logger.exception('Resend API send failed: %s', exc)
            return False, str(exc)
    if not (SMTP_HOST and MAIL_FROM):
        return False, 'Email delivery is not configured.'
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = MAIL_FROM
    msg['To'] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            if SMTP_USE_TLS:
                smtp.starttls()
                smtp.ehlo()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True, 'sent'
    except Exception as exc:
        app.logger.exception('Email send failed: %s', exc)
        return False, str(exc)


def make_reset_token(user: sqlite3.Row) -> str:
    return password_reset_serializer().dumps({'uid': user['id'], 'ph': user['password_hash']}, salt='password-reset')


def verify_reset_token(token: str, max_age: int = RESET_TOKEN_MAX_AGE) -> Optional[sqlite3.Row]:
    try:
        payload = password_reset_serializer().loads(token, salt='password-reset', max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    user = query_one('SELECT * FROM users WHERE id = ?', (payload.get('uid'),))
    if not user or user['password_hash'] != payload.get('ph'):
        return None
    return user


def send_logged_email(*, event_type: str, to_email: str, subject: str, body: str, user_id: Optional[int] = None, player_id: Optional[int] = None, tournament_id: Optional[int] = None) -> tuple[bool, str]:
    if not email_enabled():
        log_email_delivery(
            event_type=event_type,
            to_email=to_email,
            subject=subject,
            body=body,
            delivery_status='skipped',
            detail='Email delivery is not configured.',
            user_id=user_id,
            player_id=player_id,
            tournament_id=tournament_id,
        )
        return False, 'Email delivery is not configured.'
    sent, detail = send_app_email(to_email, subject, body)
    log_email_delivery(
        event_type=event_type,
        to_email=to_email,
        subject=subject,
        body=body,
        delivery_status='sent' if sent else 'failed',
        detail=detail,
        user_id=user_id,
        player_id=player_id,
        tournament_id=tournament_id,
    )
    return sent, detail


def send_welcome_email(user: sqlite3.Row) -> tuple[bool, str]:
    login_url = f"{APP_BASE_URL}{url_for('login')}"
    body = (
        f"Welcome to Aoe4IT, {user['username']}.\n\n"
        f"Your account is ready. You can sign in here: {login_url}\n\n"
        "If you ever forget your password, use the reset link on the login page."
    )
    return send_logged_email(
        event_type='welcome',
        to_email=user['email'],
        subject='Welcome to Aoe4IT',
        body=body,
        user_id=user['id'],
    )


def send_password_reset_email(user: sqlite3.Row) -> tuple[bool, str]:
    token = make_reset_token(user)
    reset_url = f"{APP_BASE_URL}{url_for('reset_password', token=token)}"
    body = (
        f"A password reset was requested for your Aoe4IT account.\n\n"
        f"Reset your password here: {reset_url}\n\n"
        f"This link expires in {RESET_TOKEN_MAX_AGE // 60} minutes. If you did not request this, you can ignore this email."
    )
    return send_logged_email(
        event_type='password_reset',
        to_email=user['email'],
        subject='Aoe4IT password reset',
        body=body,
        user_id=user['id'],
    )


def send_tournament_registration_email(user: sqlite3.Row, player: sqlite3.Row, tournament: sqlite3.Row) -> tuple[bool, str]:
    detail_url = f"{APP_BASE_URL}{url_for('tournament_detail', tournament_id=tournament['id'])}"
    body = (
        f"Hi {player['display_name']},\n\n"
        f"You are registered for {tournament['name']}.\n"
        f"Tournament page: {detail_url}\n"
        f"Start time: {tournament['starts_at'] or 'TBA'}\n\n"
        "Keep this email as your confirmation that the registration was stored in Aoe4IT."
    )
    return send_logged_email(
        event_type='tournament_registration',
        to_email=user['email'],
        subject=f"Aoe4IT registration confirmed: {tournament['name']}",
        body=body,
        user_id=user['id'],
        player_id=player['id'],
        tournament_id=tournament['id'],
    )


# -------------------------
# Database
# -------------------------

def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop('db', None)
    if db is not None:
        db.close()


def query_all(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def db_scalar(sql: str, params: tuple = (), default=0):
    row = query_one(sql, params)
    if row is None:
        return default
    if isinstance(row, sqlite3.Row):
        keys = row.keys()
        if not keys:
            return default
        return row[keys[0]]
    return row[0] if row else default


def upsert_runtime_state(key: str, value: str) -> None:
    db = get_db()
    current_ts = now_str()
    db.execute(
        """
        INSERT INTO app_runtime_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, current_ts),
    )
    db.commit()


def now() -> datetime:
    return datetime.now()


def now_str() -> str:
    return now().strftime('%Y-%m-%d %H:%M:%S')


def safe_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def normalize_queue_type(queue_type: Optional[str]) -> str:
    return 'bo3' if queue_type == 'bo3' else 'bo1'


def validate_series_result(*, winner_id: Optional[int], score1: Optional[int], score2: Optional[int], player1_id: Optional[int], player2_id: Optional[int], best_of: int) -> Optional[str]:
    if winner_id is None or score1 is None or score2 is None:
        return 'Winner and scores must be whole numbers.'
    if winner_id not in {player1_id, player2_id}:
        return 'Winner must be one of the two players in the series.'
    if score1 < 0 or score2 < 0:
        return 'Scores cannot be negative.'

    target_wins = 2 if best_of == 3 else 1
    winner_score = score1 if winner_id == player1_id else score2
    loser_score = score2 if winner_id == player1_id else score1

    if winner_score != target_wins:
        return f'The winner must have exactly {target_wins} win' + ('s.' if target_wins != 1 else '.')
    if loser_score >= target_wins:
        return 'The losing score is too high for this series format.'
    if winner_id == player1_id and score1 <= score2:
        return 'Winner selection does not match the scoreline.'
    if winner_id == player2_id and score2 <= score1:
        return 'Winner selection does not match the scoreline.'
    return None


def parse_db_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def ensure_column(table: str, column: str, definition: str) -> None:
    cols = {row['name'] for row in query_all(f'PRAGMA table_info({table})')}
    if column not in cols:
        get_db().execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
        get_db().commit()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            player_id INTEGER,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            display_name TEXT NOT NULL UNIQUE,
            country TEXT,
            main_civ TEXT,
            bio TEXT,
            rating INTEGER NOT NULL DEFAULT 1500,
            trust_score INTEGER NOT NULL DEFAULT 100,
            is_shadow INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            starts_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            format_desc TEXT NOT NULL DEFAULT 'BO1 opening rounds, BO3 semifinals, BO3 final',
            created_at TEXT NOT NULL,
            winner_id INTEGER,
            FOREIGN KEY (winner_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            seed INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(tournament_id, player_id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            round_label TEXT NOT NULL,
            bracket_index INTEGER NOT NULL,
            best_of INTEGER NOT NULL DEFAULT 1,
            player1_id INTEGER,
            player2_id INTEGER,
            winner_id INTEGER,
            score1 INTEGER,
            score2 INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            next_match_id INTEGER,
            slot_in_next INTEGER,
            provisional_winner_id INTEGER,
            player1_report_winner_id INTEGER,
            player1_report_score1 INTEGER,
            player1_report_score2 INTEGER,
            player2_report_winner_id INTEGER,
            player2_report_score1 INTEGER,
            player2_report_score2 INTEGER,
            admin_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
            FOREIGN KEY (player1_id) REFERENCES players(id),
            FOREIGN KEY (player2_id) REFERENCES players(id),
            FOREIGN KEY (winner_id) REFERENCES players(id),
            FOREIGN KEY (provisional_winner_id) REFERENCES players(id),
            FOREIGN KEY (next_match_id) REFERENCES matches(id)
        );

        CREATE TABLE IF NOT EXISTS practice_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL UNIQUE,
            queue_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS practice_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_type TEXT NOT NULL,
            best_of INTEGER NOT NULL DEFAULT 1,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_accept',
            accepted1 INTEGER NOT NULL DEFAULT 0,
            accepted2 INTEGER NOT NULL DEFAULT 0,
            ready_expires_at TEXT,
            chosen_map TEXT,
            winner_id INTEGER,
            score1 INTEGER,
            score2 INTEGER,
            provisional_winner_id INTEGER,
            player1_report_winner_id INTEGER,
            player1_report_score1 INTEGER,
            player1_report_score2 INTEGER,
            player2_report_winner_id INTEGER,
            player2_report_score1 INTEGER,
            player2_report_score2 INTEGER,
            admin_note TEXT,
            cancellation_reason TEXT,
            is_hidden INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (player1_id) REFERENCES players(id),
            FOREIGN KEY (player2_id) REFERENCES players(id),
            FOREIGN KEY (winner_id) REFERENCES players(id),
            FOREIGN KEY (provisional_winner_id) REFERENCES players(id)
        );
        """
    )
    db.commit()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS practice_map_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            turn_number INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            map_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES practice_rooms(id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS practice_civ_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            turn_number INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            civ_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES practice_rooms(id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );
        

        CREATE TABLE IF NOT EXISTS premium_waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS email_delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            player_id INTEGER,
            tournament_id INTEGER,
            event_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            delivery_status TEXT NOT NULL,
            detail TEXT,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            player_id INTEGER,
            tournament_id INTEGER,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        );

        CREATE TABLE IF NOT EXISTS rate_limit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_key TEXT NOT NULL,
            bucket_key TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_registrations_tournament_id ON registrations (tournament_id);
        CREATE INDEX IF NOT EXISTS idx_registrations_player_id ON registrations (player_id);
        CREATE INDEX IF NOT EXISTS idx_matches_tournament_status ON matches (tournament_id, status);
        CREATE INDEX IF NOT EXISTS idx_matches_round_bracket ON matches (round_number, bracket_index);
        CREATE INDEX IF NOT EXISTS idx_practice_queue_type_updated ON practice_queue (queue_type, updated_at);
        CREATE INDEX IF NOT EXISTS idx_practice_rooms_status_updated ON practice_rooms (status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log (created_at);
        CREATE INDEX IF NOT EXISTS idx_email_delivery_created_at ON email_delivery_log (created_at);
        CREATE INDEX IF NOT EXISTS idx_rate_limit_route_bucket_expires ON rate_limit_events (route_key, bucket_key, expires_at);
        """
    )
    db.commit()

    ensure_column('users', 'is_admin', 'is_admin INTEGER NOT NULL DEFAULT 0')
    ensure_column('players', 'user_id', 'user_id INTEGER')
    ensure_column('players', 'is_shadow', 'is_shadow INTEGER NOT NULL DEFAULT 0')
    ensure_column('matches', 'provisional_winner_id', 'provisional_winner_id INTEGER')
    ensure_column('practice_rooms', 'provisional_winner_id', 'provisional_winner_id INTEGER')
    ensure_column('practice_rooms', 'is_hidden', 'is_hidden INTEGER NOT NULL DEFAULT 0')
    ensure_column('practice_rooms', 'map_first_player_id', 'map_first_player_id INTEGER')
    ensure_column('practice_rooms', 'civ_first_player_id', 'civ_first_player_id INTEGER')
    ensure_column('practice_rooms', 'snipe_first_player_id', 'snipe_first_player_id INTEGER')
    ensure_column('practice_rooms', 'player1_final_civ', 'player1_final_civ TEXT')
    ensure_column('practice_rooms', 'player2_final_civ', 'player2_final_civ TEXT')


@app.before_request
def apply_request_security():
    session.permanent = True
    csrf_result = validate_csrf_or_reject()
    if csrf_result is not None:
        return csrf_result


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    response.headers.setdefault('X-App-Env', APP_ENV)
    if IS_STAGING:
        response.headers.setdefault('X-Robots-Tag', 'noindex, nofollow, noarchive')
    if request.path.startswith('/ops/'):
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


# -------------------------
# Auth and permissions
# -------------------------

def get_current_user() -> Optional[sqlite3.Row]:
    uid = session.get('user_id')
    if not uid:
        return None
    return query_one('SELECT * FROM users WHERE id = ?', (uid,))


def get_current_player() -> Optional[sqlite3.Row]:
    user = get_current_user()
    if not user or not user['player_id']:
        return None
    return query_one('SELECT * FROM players WHERE id = ?', (user['player_id'],))


def ensure_hidden_lab_profiles():
    hidden_profiles = [
        ('Aoe4IT Lab Alpha', 'SE', 'English', 'Hidden draft lab test profile.'),
        ('Aoe4IT Lab Beta', 'SE', 'French', 'Hidden draft lab test profile.'),
    ]
    ids = []
    for display_name, country, main_civ, bio in hidden_profiles:
        player = query_one('SELECT * FROM players WHERE display_name = ?', (display_name,))
        if player:
            if player['is_shadow'] != 1:
                execute('UPDATE players SET is_shadow = 1, country = ?, main_civ = ?, bio = ? WHERE id = ?', (country, main_civ, bio, player['id']))
            ids.append(player['id'])
            continue
        player_id = execute(
            'INSERT INTO players (display_name, country, main_civ, bio, rating, trust_score, is_shadow, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (display_name, country, main_civ, bio, 1500, 100, 1, now_str()),
        )
        ids.append(player_id)
    return query_all('SELECT * FROM players WHERE COALESCE(is_shadow, 0) = 1 ORDER BY id ASC')

def is_admin() -> bool:
    user = get_current_user()
    return bool(user and user['is_admin'])

def shadow_lab_available() -> bool:
    return ENABLE_SHADOW_LAB or APP_ENV == 'development'


def shadow_lab_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not shadow_lab_available():
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            flash('Log in first.', 'error')
            return redirect(url_for('login'))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash('Admin access is required.', 'error')
            return redirect(url_for('dashboard'))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals():
    return {
        'current_user': get_current_user(),
        'current_player': get_current_player(),
        'admin_mode': is_admin(),
        'now_year': now().year,
        'beta_mode': bool(BETA_INVITE_CODE),
        'civ_flags': CIV_FLAG_FILES,
        'aoe4_civs': AOE4_CIVS,
        'email_enabled': email_enabled(),
        'shadow_lab_available': shadow_lab_available(),
        'csrf_token': ensure_csrf_token,
        'csrf_input': csrf_input,
        'is_production': IS_PRODUCTION,
        'app_env': APP_ENV,
        'database_provider': DATABASE_PROVIDER,
    }


# -------------------------
# Shared helpers
# -------------------------

def public_players():
    return query_all('SELECT * FROM players WHERE COALESCE(is_shadow, 0) = 0 ORDER BY rating DESC, display_name ASC')


def rating_band(rating: int) -> str:
    if rating >= 1900:
        return 'conqueror'
    if rating >= 1600:
        return 'diamond'
    if rating >= 1300:
        return 'platinum'
    return 'gold'


def filtered_public_players(search: str = '', country: str = '', civ: str = ''):
    sql = 'SELECT * FROM players WHERE COALESCE(is_shadow, 0) = 0'
    params: list[str] = []
    if search:
        like = f'%{search}%'
        sql += ' AND (display_name LIKE ? OR COALESCE(main_civ, "") LIKE ? OR COALESCE(country, "") LIKE ? OR COALESCE(bio, "") LIKE ?)'
        params.extend([like, like, like, like])
    if country:
        sql += ' AND COALESCE(country, "") = ?'
        params.append(country.upper())
    if civ:
        sql += ' AND COALESCE(main_civ, "") = ?'
        params.append(civ)
    sql += ' ORDER BY rating DESC, display_name ASC'
    return query_all(sql, tuple(params))


def featured_team_cards(limit: int = 6):
    players = public_players()[: max(limit * 2, 2)]
    cards = []
    pair_index = 0
    for idx in range(0, len(players), 2):
        duo = players[idx:idx + 2]
        if not duo:
            continue
        pair_index += 1
        avg_rating = round(sum(player['rating'] for player in duo) / len(duo))
        cards.append({
            'name': f'Warband {pair_index}',
            'players': duo,
            'avg_rating': avg_rating,
            'focus': duo[0]['main_civ'] or 'Open pool',
        })
        if len(cards) >= limit:
            break
    return cards


def friend_rows_for_player(player_id: Optional[int], limit: int = 6):
    if not player_id:
        return []
    player = query_one('SELECT * FROM players WHERE id = ?', (player_id,))
    if not player:
        return []
    rows = []
    seen = set()
    for row in player_history(player_id, limit=24):
        opponent_name = row['player2_name'] if row['player1_name'] == player['display_name'] else row['player1_name']
        if not opponent_name or opponent_name in seen:
            continue
        opponent = query_one('SELECT * FROM players WHERE display_name = ? AND COALESCE(is_shadow,0)=0', (opponent_name,))
        if not opponent:
            continue
        seen.add(opponent_name)
        rows.append({'player': opponent, 'context_name': row['context_name'], 'happened_at': row['happened_at']})
        if len(rows) >= limit:
            break
    return rows


def expected_score(rating_a: int, rating_b: int) -> float:
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def apply_rating_change(player1_id: int, player2_id: int, winner_id: int) -> None:
    p1 = query_one('SELECT * FROM players WHERE id = ?', (player1_id,))
    p2 = query_one('SELECT * FROM players WHERE id = ?', (player2_id,))
    if not p1 or not p2:
        return
    k = 24
    e1 = expected_score(p1['rating'], p2['rating'])
    e2 = expected_score(p2['rating'], p1['rating'])
    s1 = 1 if winner_id == player1_id else 0
    s2 = 1 if winner_id == player2_id else 0
    r1 = round(p1['rating'] + k * (s1 - e1))
    r2 = round(p2['rating'] + k * (s2 - e2))
    execute('UPDATE players SET rating = ? WHERE id = ?', (r1, player1_id))
    execute('UPDATE players SET rating = ? WHERE id = ?', (r2, player2_id))


def player_stats(player_id: int) -> dict:
    tour_played = query_one('SELECT COUNT(*) AS c FROM matches WHERE status = "completed" AND (player1_id = ? OR player2_id = ?)', (player_id, player_id))['c']
    tour_wins = query_one('SELECT COUNT(*) AS c FROM matches WHERE status = "completed" AND winner_id = ?', (player_id,))['c']
    practice_played = query_one('SELECT COUNT(*) AS c FROM practice_rooms WHERE status = "completed" AND COALESCE(is_hidden,0)=0 AND (player1_id = ? OR player2_id = ?)', (player_id, player_id))['c']
    practice_wins = query_one('SELECT COUNT(*) AS c FROM practice_rooms WHERE status = "completed" AND COALESCE(is_hidden,0)=0 AND winner_id = ?', (player_id,))['c']
    played = tour_played + practice_played
    wins = tour_wins + practice_wins
    return {
        'played': played,
        'wins': wins,
        'losses': max(0, played - wins),
        'practice_played': practice_played,
        'practice_wins': practice_wins,
    }


def get_player_active_room(player_id: int):
    return query_one(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        WHERE COALESCE(pr.is_hidden,0)=0
          AND pr.status IN ('awaiting_accept', 'map_draft', 'civ_ban', 'civ_draft', 'live', 'awaiting_confirmation', 'provisional')
          AND (pr.player1_id = ? OR pr.player2_id = ?)
        ORDER BY pr.updated_at DESC LIMIT 1
        """,
        (player_id, player_id),
    )


def waiting_queue_rows(queue_type: str):
    return query_all(
        """
        SELECT q.*, p.display_name, p.rating, p.main_civ
        FROM practice_queue q
        JOIN players p ON p.id = q.player_id
        WHERE q.queue_type = ?
        ORDER BY q.created_at ASC
        """,
        (queue_type,),
    )


def queue_waiting_preview(queue_type: str, limit: int = 8):
    rows = waiting_queue_rows(queue_type)[:limit]
    preview = []
    current_time = now()
    for idx, row in enumerate(rows, start=1):
        created = parse_db_time(row['created_at'])
        waited_seconds = max(0, int((current_time - created).total_seconds())) if created else 0
        if waited_seconds < 60:
            waited_label = 'Just joined'
        else:
            waited_label = f"{waited_seconds // 60}m in queue"
        preview.append({
            'slot': idx,
            'player_id': row['player_id'],
            'display_name': row['display_name'],
            'rating': row['rating'],
            'main_civ': row['main_civ'],
            'waited_label': waited_label,
        })
    return preview


def queue_eta_label(queue_type: str) -> str:
    count = len(waiting_queue_rows(queue_type))
    if count >= 1:
        return 'Instant match if one more joins'
    return 'Waiting for first player'


def recent_results(limit: int = 8):
    return query_all(
        """
        SELECT * FROM (
            SELECT 'practice' AS source_type, pr.id AS source_id, pr.updated_at AS happened_at,
                   pr.score1, pr.score2, pr.best_of, pr.status AS status,
                   p1.display_name AS player1_name, p2.display_name AS player2_name,
                   CASE pr.queue_type WHEN 'bo3' THEN 'Practice BO3' ELSE 'Practice BO1' END AS context_name,
                   pr.chosen_map AS chosen_map, pr.winner_id
            FROM practice_rooms pr
            LEFT JOIN players p1 ON p1.id = pr.player1_id
            LEFT JOIN players p2 ON p2.id = pr.player2_id
            WHERE pr.status = 'completed' AND COALESCE(pr.is_hidden,0)=0
            UNION ALL
            SELECT 'tournament' AS source_type, m.id AS source_id, m.updated_at AS happened_at,
                   m.score1, m.score2, m.best_of, m.status AS status,
                   p1.display_name AS player1_name, p2.display_name AS player2_name,
                   t.name AS context_name,
                   NULL AS chosen_map, m.winner_id
            FROM matches m
            LEFT JOIN players p1 ON p1.id = m.player1_id
            LEFT JOIN players p2 ON p2.id = m.player2_id
            LEFT JOIN tournaments t ON t.id = m.tournament_id
            WHERE m.status = 'completed'
        ) x
        ORDER BY happened_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def player_history(player_id: int, limit: int = 12):
    return query_all(
        """
        SELECT * FROM (
            SELECT 'practice' AS source_type, pr.id AS source_id, pr.updated_at AS happened_at,
                   pr.score1, pr.score2, pr.best_of, pr.status AS status,
                   p1.display_name AS player1_name, p2.display_name AS player2_name,
                   CASE pr.queue_type WHEN 'bo3' THEN 'Practice BO3' ELSE 'Practice BO1' END AS context_name,
                   pr.chosen_map AS chosen_map, pr.winner_id
            FROM practice_rooms pr
            LEFT JOIN players p1 ON p1.id = pr.player1_id
            LEFT JOIN players p2 ON p2.id = pr.player2_id
            WHERE COALESCE(pr.is_hidden,0)=0 AND (pr.player1_id = ? OR pr.player2_id = ?)
            UNION ALL
            SELECT 'tournament' AS source_type, m.id AS source_id, m.updated_at AS happened_at,
                   m.score1, m.score2, m.best_of, m.status AS status,
                   p1.display_name AS player1_name, p2.display_name AS player2_name,
                   t.name AS context_name,
                   NULL AS chosen_map, m.winner_id
            FROM matches m
            LEFT JOIN players p1 ON p1.id = m.player1_id
            LEFT JOIN players p2 ON p2.id = m.player2_id
            LEFT JOIN tournaments t ON t.id = m.tournament_id
            WHERE m.player1_id = ? OR m.player2_id = ?
        ) x
        ORDER BY happened_at DESC
        LIMIT ?
        """,
        (player_id, player_id, player_id, player_id, limit),
    )


def my_registrations(player_id: int):
    return query_all(
        """
        SELECT t.*, r.created_at AS registered_at
        FROM registrations r
        JOIN tournaments t ON t.id = r.tournament_id
        WHERE r.player_id = ?
        ORDER BY t.created_at DESC
        """,
        (player_id,),
    )


def player_rank_position(player_id: int) -> Optional[int]:
    for idx, row in enumerate(public_players(), start=1):
        if row['id'] == player_id:
            return idx
    return None


def build_recent_form(history_rows, player_id: int, limit: int = 5):
    form = []
    for row in history_rows:
        if row['status'] != 'completed' or row['winner_id'] is None:
            continue
        form.append({
            'result': 'W' if row['winner_id'] == player_id else 'L',
            'source_type': row['source_type'],
            'context_name': row['context_name'],
        })
        if len(form) >= limit:
            break
    return form


def active_tournaments():
    return query_all(
        """
        SELECT t.*, COUNT(r.id) AS reg_count, p.display_name AS winner_name
        FROM tournaments t
        LEFT JOIN registrations r ON r.tournament_id = t.id
        LEFT JOIN players p ON p.id = t.winner_id
        GROUP BY t.id
        ORDER BY CASE t.status WHEN 'live' THEN 0 WHEN 'open' THEN 1 ELSE 2 END, t.starts_at ASC
        """
    )


def queue_label(queue_type: str) -> str:
    return 'Practice BO3' if queue_type == 'bo3' else 'Practice BO1'


def create_weekly_tournament(name: str, starts_at: str) -> int:
    return execute(
        'INSERT INTO tournaments (name, starts_at, status, format_desc, created_at) VALUES (?, ?, ?, ?, ?)',
        (name, starts_at, 'open', 'BO1 opening rounds, BO3 semifinals, BO3 final', now_str()),
    )


# -------------------------
# Tournament helpers
# -------------------------

def tournament_registration_count(tournament_id: int) -> int:
    return query_one('SELECT COUNT(*) AS c FROM registrations WHERE tournament_id = ?', (tournament_id,))['c']


def tournament_player_registered(tournament_id: int, player_id: int) -> bool:
    return bool(query_one('SELECT 1 FROM registrations WHERE tournament_id = ? AND player_id = ?', (tournament_id, player_id)))


def next_power_of_two(value: int) -> int:
    p = 1
    while p < value:
        p *= 2
    return p


def bracket_positions(size: int) -> list[int]:
    positions = [1, 2]
    while len(positions) < size:
        mirror_sum = len(positions) * 2 + 1
        expanded = []
        for pos in positions:
            expanded.extend([pos, mirror_sum - pos])
        positions = expanded
    return positions


def format_round_label(matches_in_round: int) -> str:
    labels = {1: 'Final', 2: 'Semifinal', 4: 'Quarterfinal', 8: 'Round of 16', 16: 'Round of 32'}
    return labels.get(matches_in_round, f'Round of {matches_in_round * 2}')


def tournament_matches_grouped(tournament_id: int):
    rows = query_all(
        """
        SELECT m.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               w.display_name AS winner_name, pw.display_name AS provisional_winner_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        LEFT JOIN players w ON w.id = m.winner_id
        LEFT JOIN players pw ON pw.id = m.provisional_winner_id
        WHERE m.tournament_id = ?
        ORDER BY m.round_number ASC, m.bracket_index ASC
        """,
        (tournament_id,),
    )
    grouped = []
    current_label = None
    bucket = []
    for row in rows:
        if row['round_label'] != current_label:
            if bucket:
                grouped.append({'label': current_label, 'matches': bucket})
            current_label = row['round_label']
            bucket = [row]
        else:
            bucket.append(row)
    if bucket:
        grouped.append({'label': current_label, 'matches': bucket})
    return grouped


def generate_bracket(tournament_id: int) -> None:
    regs = query_all(
        """
        SELECT r.*, p.rating
        FROM registrations r
        JOIN players p ON p.id = r.player_id
        WHERE r.tournament_id = ?
        ORDER BY COALESCE(r.seed, 999), p.rating DESC, r.created_at ASC
        """,
        (tournament_id,),
    )
    if len(regs) < 2:
        raise ValueError('You need at least 2 players to start a cup.')
    if query_one('SELECT 1 FROM matches WHERE tournament_id = ? LIMIT 1', (tournament_id,)):
        raise ValueError('Bracket already exists for this cup.')

    size = next_power_of_two(len(regs))
    positions = bracket_positions(size)
    seeded = [None] * size
    for seed, reg in zip(positions, regs):
        seeded[seed - 1] = reg['player_id']

    rounds = []
    match_count = size // 2
    round_num = 1
    while match_count >= 1:
        rounds.append({'round_number': round_num, 'match_count': match_count, 'label': format_round_label(match_count)})
        match_count //= 2
        round_num += 1

    match_ids = {}
    for rd in rounds:
        for idx in range(rd['match_count']):
            best_of = 3 if rd['match_count'] <= 2 else 1
            match_id = execute(
                'INSERT INTO matches (tournament_id, round_number, round_label, bracket_index, best_of, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (tournament_id, rd['round_number'], rd['label'], idx + 1, best_of, 'pending', now_str(), now_str()),
            )
            match_ids[(rd['round_number'], idx + 1)] = match_id

    # wire next matches
    for rd in rounds[:-1]:
        for idx in range(1, rd['match_count'] + 1):
            next_idx = (idx + 1) // 2
            slot = 1 if idx % 2 == 1 else 2
            execute(
                'UPDATE matches SET next_match_id = ?, slot_in_next = ? WHERE id = ?',
                (match_ids[(rd['round_number'] + 1, next_idx)], slot, match_ids[(rd['round_number'], idx)]),
            )

    # first round players
    for i in range(0, size, 2):
        idx = i // 2 + 1
        match_id = match_ids[(1, idx)]
        p1 = seeded[i]
        p2 = seeded[i + 1] if i + 1 < len(seeded) else None
        if p1 and p2:
            execute('UPDATE matches SET player1_id = ?, player2_id = ?, status = ?, updated_at = ? WHERE id = ?', (p1, p2, 'live', now_str(), match_id))
        elif p1 or p2:
            bye_winner = p1 or p2
            execute('UPDATE matches SET player1_id = ?, player2_id = ?, winner_id = ?, status = ?, updated_at = ? WHERE id = ?', (p1, p2, bye_winner, 'completed', now_str(), match_id))
            advance_tournament_winner(match_id, bye_winner)

    execute('UPDATE tournaments SET status = ? WHERE id = ?', ('live', tournament_id))


def advance_tournament_winner(match_id: int, winner_id: int) -> None:
    match = query_one('SELECT * FROM matches WHERE id = ?', (match_id,))
    if not match or not match['next_match_id']:
        if match:
            execute('UPDATE tournaments SET status = ?, winner_id = ? WHERE id = ?', ('completed', winner_id, match['tournament_id']))
        return
    target = query_one('SELECT * FROM matches WHERE id = ?', (match['next_match_id'],))
    if not target:
        return
    field = 'player1_id' if match['slot_in_next'] == 1 else 'player2_id'
    execute(f'UPDATE matches SET {field} = ?, updated_at = ? WHERE id = ?', (winner_id, now_str(), target['id']))
    refreshed = query_one('SELECT * FROM matches WHERE id = ?', (target['id'],))
    if refreshed['player1_id'] and refreshed['player2_id'] and refreshed['status'] == 'pending':
        execute('UPDATE matches SET status = ?, updated_at = ? WHERE id = ?', ('live', now_str(), refreshed['id']))
    elif refreshed['player1_id'] and not refreshed['player2_id']:
        execute('UPDATE matches SET winner_id = ?, status = ?, updated_at = ? WHERE id = ?', (refreshed['player1_id'], 'completed', now_str(), refreshed['id']))
        advance_tournament_winner(refreshed['id'], refreshed['player1_id'])
    elif refreshed['player2_id'] and not refreshed['player1_id']:
        execute('UPDATE matches SET winner_id = ?, status = ?, updated_at = ? WHERE id = ?', (refreshed['player2_id'], 'completed', now_str(), refreshed['id']))
        advance_tournament_winner(refreshed['id'], refreshed['player2_id'])


def apply_tournament_provisional_result(match_id: int, winner_id: int) -> None:
    match = query_one('SELECT * FROM matches WHERE id = ?', (match_id,))
    if not match:
        return
    execute('UPDATE matches SET provisional_winner_id = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, 'provisional', now_str(), match_id))
    if winner_id:
        advance_tournament_provisional(match, winner_id)


def advance_tournament_provisional(match: sqlite3.Row, winner_id: int) -> None:
    if not match['next_match_id']:
        return
    target = query_one('SELECT * FROM matches WHERE id = ?', (match['next_match_id'],))
    if not target:
        return
    field = 'player1_id' if match['slot_in_next'] == 1 else 'player2_id'
    execute(f'UPDATE matches SET {field} = ?, updated_at = ? WHERE id = ?', (winner_id, now_str(), target['id']))
    refreshed = query_one('SELECT * FROM matches WHERE id = ?', (target['id'],))
    if refreshed['player1_id'] and refreshed['player2_id'] and refreshed['status'] == 'pending':
        execute('UPDATE matches SET status = ?, updated_at = ? WHERE id = ?', ('live', now_str(), refreshed['id']))


def finalize_tournament_match(match_id: int, winner_id: int, score1: int, score2: int, admin_note: str = '') -> None:
    match = query_one('SELECT * FROM matches WHERE id = ?', (match_id,))
    if not match:
        return
    execute(
        'UPDATE matches SET winner_id = ?, score1 = ?, score2 = ?, status = ?, provisional_winner_id = NULL, admin_note = ?, updated_at = ? WHERE id = ?',
        (winner_id, score1, score2, 'completed', admin_note, now_str(), match_id),
    )
    if match['player1_id'] and match['player2_id']:
        apply_rating_change(match['player1_id'], match['player2_id'], winner_id)
    advance_tournament_winner(match_id, winner_id)


def auto_start_due_tournaments() -> None:
    due = query_all('SELECT * FROM tournaments WHERE status = ? AND starts_at IS NOT NULL', ('open',))
    current = now()
    for t in due:
        try:
            starts = datetime.strptime(t['starts_at'], '%Y-%m-%d %H:%M')
        except ValueError:
            try:
                starts = datetime.strptime(t['starts_at'], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
        if starts <= current and tournament_registration_count(t['id']) >= 2:
            try:
                generate_bracket(t['id'])
            except ValueError:
                pass


def get_practice_room(room_id: int) -> Optional[sqlite3.Row]:
    return query_one(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               pw.display_name AS provisional_winner_name, w.display_name AS winner_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        LEFT JOIN players pw ON pw.id = pr.provisional_winner_id
        LEFT JOIN players w ON w.id = pr.winner_id
        WHERE pr.id = ?
        """,
        (room_id,),
    )

def get_hidden_practice_room(room_id: int) -> Optional[sqlite3.Row]:
    return query_one(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               pw.display_name AS provisional_winner_name, w.display_name AS winner_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        LEFT JOIN players pw ON pw.id = pr.provisional_winner_id
        LEFT JOIN players w ON w.id = pr.winner_id
        WHERE pr.id = ? AND COALESCE(pr.is_hidden, 0) = 1
        """,
        (room_id,),
    )

def recent_hidden_practice_rooms(limit: int = 12):
    return query_all(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        WHERE COALESCE(pr.is_hidden, 0) = 1
        ORDER BY pr.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )

def other_player_id(room: sqlite3.Row, player_id: int) -> int:
    return room['player2_id'] if player_id == room['player1_id'] else room['player1_id']

def ordered_pair(room: sqlite3.Row, starter_column: str):
    first = room[starter_column] or room['player1_id']
    second = room['player2_id'] if first == room['player1_id'] else room['player1_id']
    return first, second

def get_practice_map_actions(room_id: int):
    return query_all(
        """
        SELECT a.*, p.display_name AS player_name
        FROM practice_map_actions a
        LEFT JOIN players p ON p.id = a.player_id
        WHERE a.room_id = ?
        ORDER BY a.turn_number ASC, a.id ASC
        """,
        (room_id,),
    )

def get_practice_civ_actions(room_id: int):
    return query_all(
        """
        SELECT a.*, p.display_name AS player_name
        FROM practice_civ_actions a
        LEFT JOIN players p ON p.id = a.player_id
        WHERE a.room_id = ?
        ORDER BY a.turn_number ASC, a.id ASC
        """,
        (room_id,),
    )

def compute_map_draft(room: sqlite3.Row) -> dict:
    actions = get_practice_map_actions(room['id'])
    used_maps = [a['map_name'] for a in actions]
    available_maps = [m for m in PRACTICE_MAPS if m not in used_maps]
    first, second = ordered_pair(room, 'map_first_player_id')
    if room['best_of'] == 1:
        turn_players = [first, second, first, second, first, second, first]
        turn_actions = ['ban'] * len(turn_players)
        picked_maps = []
    else:
        turn_players = [first, second, first, second, first]
        turn_actions = ['ban', 'ban', 'pick', 'pick', 'pick']
        picked_maps = [a['map_name'] for a in actions if a['action_type'] == 'pick']

    completed = len(actions) >= len(turn_actions)
    current_player_id = None if completed else turn_players[len(actions)]
    current_action = None if completed else turn_actions[len(actions)]

    if room['best_of'] == 1:
        final_maps = available_maps if completed else []
        display_text = final_maps[0] if completed and final_maps else (room['chosen_map'] or '')
    else:
        final_maps = picked_maps
        display_text = ' / '.join(picked_maps) if picked_maps else (room['chosen_map'] or '')

    return {
        'actions': actions,
        'available_maps': available_maps,
        'picked_maps': picked_maps,
        'current_player_id': current_player_id,
        'current_action': current_action,
        'completed': completed,
        'final_maps': final_maps,
        'display_text': display_text,
    }

def start_map_draft(room_id: int) -> None:
    room = get_practice_room(room_id)
    if not room:
        return
    seed = room['id'] + room['player1_id'] + room['player2_id']
    starter = room['player1_id'] if seed % 2 == 0 else room['player2_id']
    execute(
        'UPDATE practice_rooms SET status = ?, map_first_player_id = ?, updated_at = ? WHERE id = ?',
        ('map_draft', starter, now_str(), room_id),
    )

def finish_map_draft(room_id: int) -> None:
    room = get_practice_room(room_id)
    if not room:
        return
    state = compute_map_draft(room)
    if not state['completed']:
        return
    chosen_map = state['final_maps'][0] if room['best_of'] == 1 else ' / '.join(state['final_maps'])
    civ_first = other_player_id(room, room['map_first_player_id'] or room['player1_id'])
    execute(
        'UPDATE practice_rooms SET chosen_map = ?, status = ?, civ_first_player_id = ?, snipe_first_player_id = ?, updated_at = ? WHERE id = ?',
        (chosen_map, 'civ_ban', civ_first, civ_first, now_str(), room_id),
    )

def compute_civ_draft(room: sqlite3.Row, viewer_id: Optional[int] = None) -> dict:
    actions = get_practice_civ_actions(room['id'])
    ban_actions = [a for a in actions if a['action_type'] == 'ban']
    hidden_actions = [a for a in actions if a['action_type'] == 'hidden_pick']
    snipe_actions = [a for a in actions if a['action_type'] == 'snipe']

    player1_bans = [a['civ_name'] for a in ban_actions if a['player_id'] == room['player1_id']]
    player2_bans = [a['civ_name'] for a in ban_actions if a['player_id'] == room['player2_id']]
    banned_civs = player1_bans + player2_bans

    p1_hidden = [a['civ_name'] for a in hidden_actions if a['player_id'] == room['player1_id']]
    p2_hidden = [a['civ_name'] for a in hidden_actions if a['player_id'] == room['player2_id']]

    my_hidden = []
    opp_hidden_count = 0
    if viewer_id == room['player1_id']:
        my_hidden = p1_hidden
        opp_hidden_count = len(p2_hidden)
    elif viewer_id == room['player2_id']:
        my_hidden = p2_hidden
        opp_hidden_count = len(p1_hidden)

    first, second = ordered_pair(room, 'civ_first_player_id')
    current_player_id = None
    current_action = None
    pending_snipe_player_ids = []

    if len(ban_actions) < 6:
        turn_players = [first, second, first, second, first, second]
        current_player_id = turn_players[len(ban_actions)]
        current_action = 'ban'
    elif len(hidden_actions) < 4:
        turn_players = [first, second, first, second]
        current_player_id = turn_players[len(hidden_actions)]
        current_action = 'hidden_pick'
    elif len(snipe_actions) < 2:
        current_action = 'hidden_snipe'
        submitted = {a['player_id'] for a in snipe_actions}
        pending_snipe_player_ids = [pid for pid in [room['player1_id'], room['player2_id']] if pid not in submitted]

    unavailable_for_picks = set(banned_civs + p1_hidden + p2_hidden)
    current_ban_options = [c for c in AOE4_CIVS if c not in banned_civs]
    current_pick_options = [c for c in AOE4_CIVS if c not in unavailable_for_picks]

    current_snipe_options = []
    can_submit_hidden_snipe = False
    if current_action == 'hidden_snipe' and viewer_id in pending_snipe_player_ids:
        can_submit_hidden_snipe = True
        if viewer_id == room['player1_id']:
            current_snipe_options = p2_hidden
        elif viewer_id == room['player2_id']:
            current_snipe_options = p1_hidden

    revealed_snipes = len(snipe_actions) >= 2
    if revealed_snipes:
        snipe_by_p1 = next((a['civ_name'] for a in snipe_actions if a['player_id'] == room['player1_id']), None)
        snipe_by_p2 = next((a['civ_name'] for a in snipe_actions if a['player_id'] == room['player2_id']), None)
    else:
        snipe_by_p1 = None
        snipe_by_p2 = None

    if revealed_snipes:
        p1_final = next((c for c in p1_hidden if c != snipe_by_p2), room['player1_final_civ'])
        p2_final = next((c for c in p2_hidden if c != snipe_by_p1), room['player2_final_civ'])
    else:
        p1_final = room['player1_final_civ']
        p2_final = room['player2_final_civ']

    return {
        'actions': actions,
        'ban_actions': ban_actions,
        'hidden_actions': hidden_actions,
        'snipe_actions': snipe_actions,
        'player1_bans': player1_bans,
        'player2_bans': player2_bans,
        'banned_civs': banned_civs,
        'player1_hidden': p1_hidden,
        'player2_hidden': p2_hidden,
        'my_hidden': my_hidden,
        'opp_hidden_count': opp_hidden_count,
        'current_player_id': current_player_id,
        'current_action': current_action,
        'current_ban_options': current_ban_options,
        'current_pick_options': current_pick_options,
        'current_snipe_options': current_snipe_options,
        'pending_snipe_player_ids': pending_snipe_player_ids,
        'can_submit_hidden_snipe': can_submit_hidden_snipe,
        'reveal_ready': len(hidden_actions) >= 4,
        'player1_snipe': snipe_by_p1,
        'player2_snipe': snipe_by_p2,
        'player1_final': p1_final,
        'player2_final': p2_final,
        'completed': bool(p1_final and p2_final),
    }

def finish_civ_ban(room_id: int) -> None:
    room = get_practice_room(room_id)
    if not room:
        return
    state = compute_civ_draft(room)
    if len(state['ban_actions']) < 6:
        return
    execute(
        'UPDATE practice_rooms SET status = ?, updated_at = ? WHERE id = ?',
        ('civ_draft', now_str(), room_id),
    )

def finish_civ_draft(room_id: int) -> None:
    room = get_practice_room(room_id)
    if not room:
        return
    state = compute_civ_draft(room)
    if not state['completed']:
        return
    execute(
        'UPDATE practice_rooms SET player1_final_civ = ?, player2_final_civ = ?, status = ?, updated_at = ? WHERE id = ?',
        (state['player1_final'], state['player2_final'], 'live', now_str(), room_id),
    )

def create_hidden_draft_room(player1_id: int, player2_id: int, best_of: int, starter_id: Optional[int] = None) -> int:
    queue_type = 'bo3' if best_of == 3 else 'bo1'
    room_id = execute(
        """
        INSERT INTO practice_rooms (
            queue_type, best_of, player1_id, player2_id, accepted1, accepted2,
            is_hidden, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, 1, 1, 'map_draft', ?, ?)
        """,
        (
            queue_type,
            3 if best_of == 3 else 1,
            player1_id,
            player2_id,
            now_str(),
            now_str(),
        ),
    )
    if starter_id in {player1_id, player2_id}:
        execute(
            'UPDATE practice_rooms SET map_first_player_id = ?, status = ?, updated_at = ? WHERE id = ?',
            (starter_id, 'map_draft', now_str(), room_id),
        )
    else:
        start_map_draft(room_id)
    return room_id

def reset_hidden_draft_room(room_id: int) -> None:
    room = get_hidden_practice_room(room_id)
    if not room:
        return
    execute('DELETE FROM practice_map_actions WHERE room_id = ?', (room_id,))
    execute('DELETE FROM practice_civ_actions WHERE room_id = ?', (room_id,))
    execute(
        """
        UPDATE practice_rooms
        SET accepted1 = 1,
            accepted2 = 1,
            ready_expires_at = NULL,
            chosen_map = NULL,
            map_first_player_id = NULL,
            civ_first_player_id = NULL,
            snipe_first_player_id = NULL,
            player1_final_civ = NULL,
            player2_final_civ = NULL,
            winner_id = NULL,
            score1 = NULL,
            score2 = NULL,
            player1_report_winner_id = NULL,
            player1_report_score1 = NULL,
            player1_report_score2 = NULL,
            player2_report_winner_id = NULL,
            player2_report_score1 = NULL,
            player2_report_score2 = NULL,
            provisional_winner_id = NULL,
            admin_note = NULL,
            cancellation_reason = NULL,
            status = 'map_draft',
            updated_at = ?
        WHERE id = ?
        """,
        (now_str(), room_id),
    )
    start_map_draft(room_id)


def choose_hidden_room_random_starter(room_id: int) -> Optional[int]:
    room = get_hidden_practice_room(room_id)
    if not room:
        return None
    reset_hidden_draft_room(room_id)
    starter_id = random.choice([room['player1_id'], room['player2_id']])
    execute(
        'UPDATE practice_rooms SET map_first_player_id = ?, updated_at = ? WHERE id = ?',
        (starter_id, now_str(), room_id),
    )
    return starter_id


def swap_hidden_draft_room_sides(room_id: int) -> bool:
    room = get_hidden_practice_room(room_id)
    if not room:
        return False
    execute(
        'UPDATE practice_rooms SET player1_id = ?, player2_id = ?, updated_at = ? WHERE id = ?',
        (room['player2_id'], room['player1_id'], now_str(), room_id),
    )
    reset_hidden_draft_room(room_id)
    return True


def rematch_hidden_draft_room(room_id: int, swap_sides: bool = False, random_starter: bool = False) -> Optional[int]:
    room = get_hidden_practice_room(room_id)
    if not room:
        return None
    p1, p2 = room['player1_id'], room['player2_id']
    if swap_sides:
        p1, p2 = p2, p1
    starter_id = random.choice([p1, p2]) if random_starter else None
    return create_hidden_draft_room(p1, p2, room['best_of'], starter_id=starter_id)

# -------------------------
# Practice queue helpers
# -------------------------

def ensure_not_in_queue(player_id: int) -> None:
    execute('DELETE FROM practice_queue WHERE player_id = ?', (player_id,))


def ensure_player_queued(player_id: int, queue_type: str) -> None:
    row = query_one('SELECT * FROM practice_queue WHERE player_id = ?', (player_id,))
    if row:
        execute('UPDATE practice_queue SET queue_type = ?, updated_at = ? WHERE player_id = ?', (queue_type, now_str(), player_id))
    else:
        execute('INSERT INTO practice_queue (player_id, queue_type, created_at, updated_at) VALUES (?, ?, ?, ?)', (player_id, queue_type, now_str(), now_str()))


def attempt_pairing(queue_type: str):
    waiting = waiting_queue_rows(queue_type)
    if len(waiting) < 2:
        return None
    p1 = waiting[0]
    p2 = waiting[1]
    ensure_not_in_queue(p1['player_id'])
    ensure_not_in_queue(p2['player_id'])
    best_of = 3 if queue_type == 'bo3' else 1
    room_id = execute(
        'INSERT INTO practice_rooms (queue_type, best_of, player1_id, player2_id, ready_expires_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (queue_type, best_of, p1['player_id'], p2['player_id'], (now() + timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'), 'awaiting_accept', now_str(), now_str()),
    )
    return room_id


def cancel_practice_room(room_id: int, reason: str) -> None:
    execute('UPDATE practice_rooms SET status = ?, cancellation_reason = ?, updated_at = ? WHERE id = ?', ('cancelled', reason, now_str(), room_id))


def activate_practice_room(room_id: int) -> None:
    execute('UPDATE practice_rooms SET status = ?, updated_at = ? WHERE id = ?', ('live', now_str(), room_id))


def expire_pending_practice_rooms() -> None:
    rows = query_all("SELECT * FROM practice_rooms WHERE status = 'awaiting_accept' AND ready_expires_at IS NOT NULL")
    current = now()
    for room in rows:
        expires_at = parse_db_time(room['ready_expires_at'])
        if not expires_at or expires_at > current:
            continue
        returned_any = False
        if room['accepted1'] and not room['accepted2']:
            ensure_player_queued(room['player1_id'], room['queue_type'])
            returned_any = True
        elif room['accepted2'] and not room['accepted1']:
            ensure_player_queued(room['player2_id'], room['queue_type'])
            returned_any = True
        cancel_practice_room(room['id'], 'Ready check expired.')
        if returned_any:
            attempt_pairing(room['queue_type'])


def pending_admin_practice_rooms():
    return query_all(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               pw.display_name AS provisional_winner_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        LEFT JOIN players pw ON pw.id = pr.provisional_winner_id
        WHERE pr.status IN ('awaiting_confirmation', 'provisional') AND COALESCE(pr.is_hidden,0)=0
        ORDER BY pr.updated_at DESC
        """
    )


def finalize_practice_room(room_id: int, winner_id: int, score1: int, score2: int, admin_note: str = '') -> None:
    room = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
    if not room:
        return
    execute(
        'UPDATE practice_rooms SET winner_id = ?, score1 = ?, score2 = ?, status = ?, provisional_winner_id = NULL, admin_note = ?, updated_at = ? WHERE id = ?',
        (winner_id, score1, score2, 'completed', admin_note, now_str(), room_id),
    )
    if room['player1_id'] and room['player2_id'] and not room['is_hidden']:
        apply_rating_change(room['player1_id'], room['player2_id'], winner_id)


def apply_practice_provisional_result(room_id: int, winner_id: int) -> None:
    execute('UPDATE practice_rooms SET provisional_winner_id = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, 'provisional', now_str(), room_id))


# -------------------------
# Routes
# -------------------------
@app.before_request
def _before_request() -> None:
    init_db()
    auto_start_due_tournaments()
    expire_pending_practice_rooms()


@app.route('/')
def dashboard():
    current_user = get_current_user()
    current_player = get_current_player()
    current_stats = player_stats(current_player['id']) if current_player else None
    active_room = get_player_active_room(current_player['id']) if current_player else None
    queue_row = query_one('SELECT * FROM practice_queue WHERE player_id = ?', (current_player['id'],)) if current_player else None
    featured_tournaments = active_tournaments()[:3]
    bo1_waiting = queue_waiting_preview('bo1')
    bo3_waiting = queue_waiting_preview('bo3')
    queue_snapshot = {
        'bo1': len(waiting_queue_rows('bo1')),
        'bo3': len(waiting_queue_rows('bo3')),
    }
    next_cup = featured_tournaments[0] if featured_tournaments else None
    leaderboard_preview = public_players()[:3]
    return render_template(
        'dashboard.html',
        current_user=current_user,
        current_player=current_player,
        current_stats=current_stats,
        active_room=active_room,
        queue_row=queue_row,
        recent=recent_results(8),
        tournaments=featured_tournaments,
        queue_snapshot=queue_snapshot,
        bo1_waiting=bo1_waiting,
        bo3_waiting=bo3_waiting,
        bo1_eta=queue_eta_label('bo1'),
        bo3_eta=queue_eta_label('bo3'),
        player_count=len(public_players()),
        next_cup=next_cup,
        leaderboard_preview=leaderboard_preview,
    )


@app.route('/play')
def play():
    current_user = get_current_user()
    current_player = get_current_player()
    current_stats = player_stats(current_player['id']) if current_player else None
    active_room = get_player_active_room(current_player['id']) if current_player else None
    queue_row = query_one('SELECT * FROM practice_queue WHERE player_id = ?', (current_player['id'],)) if current_player else None
    featured_tournaments = active_tournaments()[:3]
    next_cup = featured_tournaments[0] if featured_tournaments else None
    bo1_waiting = waiting_queue_rows('bo1')
    bo3_waiting = waiting_queue_rows('bo3')
    active_tab = request.args.get('tab', 'find').strip().lower()
    if active_tab not in {'find', 'party', 'custom'}:
        active_tab = 'find'
    return render_template(
        'play.html',
        current_user=current_user,
        current_player=current_player,
        current_stats=current_stats,
        active_room=active_room,
        queue_row=queue_row,
        tournaments=featured_tournaments,
        next_cup=next_cup,
        recent=recent_results(8),
        bo1_waiting=bo1_waiting,
        bo3_waiting=bo3_waiting,
        bo1_eta=queue_eta_label('bo1'),
        bo3_eta=queue_eta_label('bo3'),
        active_tab=active_tab,
    )


@app.route('/news')
def news():
    return render_template('news.html')


@app.route('/premium')
def premium():
    current_user = get_current_user()
    waitlist_count = query_one('SELECT COUNT(*) AS c FROM premium_waitlist')['c']
    has_waitlist = bool(current_user and query_one('SELECT id FROM premium_waitlist WHERE user_id = ?', (current_user['id'],)))
    return render_template('premium.html', has_waitlist=has_waitlist, waitlist_count=waitlist_count)


@app.route('/premium/waitlist', methods=['POST'])
@login_required
@rate_limit(5, 3600, scope='user')
def premium_waitlist():
    current_user = get_current_user()
    already_joined = query_one('SELECT id FROM premium_waitlist WHERE user_id = ?', (current_user['id'],))
    if already_joined:
        flash('You are already on the premium waitlist.', 'success')
        return redirect(url_for('premium'))
    execute('INSERT INTO premium_waitlist (user_id, created_at) VALUES (?, ?)', (current_user['id'], now_str()))
    flash('Premium interest saved. You are on the waitlist now.', 'success')
    return redirect(url_for('premium'))


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    player_rows = []
    tournament_rows = []
    match_rows = []
    if query:
        like = f'%{query}%'
        player_rows = query_all(
            """
            SELECT *
            FROM players
            WHERE COALESCE(is_shadow, 0) = 0
              AND (
                display_name LIKE ?
                OR COALESCE(main_civ, '') LIKE ?
                OR COALESCE(country, '') LIKE ?
                OR COALESCE(bio, '') LIKE ?
              )
            ORDER BY rating DESC, display_name ASC
            LIMIT 24
            """,
            (like, like, like, like),
        )
        tournament_rows = query_all(
            'SELECT t.*, (SELECT COUNT(*) FROM registrations r WHERE r.tournament_id = t.id) AS reg_count FROM tournaments t WHERE t.name LIKE ? ORDER BY CASE t.status WHEN "live" THEN 0 WHEN "open" THEN 1 ELSE 2 END, t.starts_at ASC LIMIT 12',
            (like,),
        )
        match_rows = query_all(
            '''
            SELECT m.*, p1.display_name AS player1_name, p2.display_name AS player2_name, t.name AS tournament_name
            FROM matches m
            LEFT JOIN players p1 ON p1.id = m.player1_id
            LEFT JOIN players p2 ON p2.id = m.player2_id
            LEFT JOIN tournaments t ON t.id = m.tournament_id
            WHERE COALESCE(p1.display_name, '') LIKE ? OR COALESCE(p2.display_name, '') LIKE ? OR COALESCE(t.name, '') LIKE ?
            ORDER BY CASE m.status WHEN "live" THEN 0 WHEN "pending_report" THEN 1 WHEN "scheduled" THEN 2 ELSE 3 END, COALESCE(m.updated_at, m.created_at) DESC
            LIMIT 12
            ''',
            (like, like, like),
        )
    return render_template('search.html', query=query, player_rows=player_rows, tournament_rows=tournament_rows, match_rows=match_rows)

@app.route('/register', methods=['GET', 'POST'])
@rate_limit(5, 900, scope='ip', key_fields=['email', 'username'])
def register_account():
    if get_current_user():
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        invite_code = request.form.get('invite_code', '').strip()
        if not username or not email or not password:
            flash('Fill in all required fields.', 'error')
        elif password != confirm:
            flash('Passwords do not match.', 'error')
        elif BETA_INVITE_CODE and invite_code != BETA_INVITE_CODE:
            flash('Invite code is required for this beta.', 'error')
        else:
            is_first = query_one('SELECT COUNT(*) AS c FROM users')['c'] == 0
            is_admin_user = 1 if is_first or email in ADMIN_EMAILS else 0
            try:
                uid = execute(
                    'INSERT INTO users (username, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)',
                    (username, email, generate_password_hash(password), is_admin_user, now_str()),
                )
                log_audit_event(event_type='account_created', detail=f'Account created for {username} <{email}>.', user_id=uid)
                session['user_id'] = uid
                rotate_csrf_token()
                new_user = query_one('SELECT * FROM users WHERE id = ?', (uid,))
                if new_user:
                    sent, _detail = send_welcome_email(new_user)
                    if sent:
                        flash('Account created. Welcome email sent and saved.', 'success')
                    elif email_enabled():
                        flash('Account created. Email delivery failed, but your account is saved.', 'error')
                    else:
                        flash('Account created and saved. Email is not configured yet.', 'success')
                else:
                    flash('Account created. Now create your player profile.', 'success')
                return redirect(url_for('players'))
            except sqlite3.IntegrityError:
                flash('Username or email already exists.', 'error')
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
@rate_limit(10, 600, scope='ip', key_fields=['identity', 'username', 'email'])
def login():
    if get_current_user():
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        identity = (
            request.form.get('identity', '')
            or request.form.get('username', '')
            or request.form.get('email', '')
        ).strip().lower()
        password = request.form.get('password', '')
        user = query_one('SELECT * FROM users WHERE lower(username) = ? OR lower(email) = ?', (identity, identity))
        if not user or not check_password_hash(user['password_hash'], password):
            flash('Invalid login.', 'error')
        else:
            session.clear()
            session['user_id'] = user['id']
            rotate_csrf_token()
            return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
@rate_limit(5, 1800, scope='ip', key_fields=['email'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = query_one('SELECT * FROM users WHERE lower(email) = ?', (email,)) if email else None
        if user and email_enabled():
            send_password_reset_email(user)
        flash('If that email exists, a reset link has been sent.', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@rate_limit(10, 1800, scope='ip')
def reset_password(token: str):
    user = verify_reset_token(token)
    if not user:
        flash('This reset link is invalid or expired.', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not password:
            flash('Enter a new password.', 'error')
        elif password != confirm:
            flash('Passwords do not match.', 'error')
        else:
            execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(password), user['id']))
            flash('Password updated. You can log in now.', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)


@app.route('/logout', methods=['POST'])
@login_required
@rate_limit(12, 300, scope='user')
def logout():
    session.clear()
    session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return redirect(url_for('dashboard'))


@app.route('/queue/join', methods=['POST'])
@login_required
@rate_limit(30, 60, scope='user')
def join_queue():
    current = get_current_player()
    if not current:
        flash('Create your player profile before queueing.', 'error')
        return redirect(url_for('players'))
    if get_player_active_room(current['id']):
        flash('You already have an active room.', 'error')
        return redirect(url_for('dashboard'))
    queue_type = normalize_queue_type(request.form.get('queue_type', 'bo1'))
    ensure_player_queued(current['id'], queue_type)
    room_id = attempt_pairing(queue_type)
    if room_id:
        flash('Match found. Head to your room and accept.', 'success')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    flash(f'Joined {queue_label(queue_type)} queue.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/queue/leave', methods=['POST'])
@login_required
@rate_limit(30, 60, scope='user')
def leave_queue():
    current = get_current_player()
    if not current:
        return redirect(url_for('dashboard'))
    ensure_not_in_queue(current['id'])
    flash('You left the queue.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/practice/<int:room_id>', methods=['GET', 'POST'])
@login_required
@rate_limit(20, 600, scope='user')
def practice_room_detail(room_id: int):
    room = query_one(
        """
        SELECT pr.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               w.display_name AS winner_name, pw.display_name AS provisional_winner_name
        FROM practice_rooms pr
        LEFT JOIN players p1 ON p1.id = pr.player1_id
        LEFT JOIN players p2 ON p2.id = pr.player2_id
        LEFT JOIN players w ON w.id = pr.winner_id
        LEFT JOIN players pw ON pw.id = pr.provisional_winner_id
        WHERE pr.id = ?
        """,
        (room_id,),
    )
    if not room:
        flash('Practice room not found.', 'error')
        return redirect(url_for('dashboard'))
    current = get_current_player()
    can_interact = bool(current and current['id'] in {room['player1_id'], room['player2_id']}) or is_admin()
    if request.method == 'POST':
        if not can_interact:
            flash('You are not part of this room.', 'error')
            return redirect(url_for('dashboard'))
        winner_id = safe_int(request.form.get('winner_id'))
        score1 = safe_int(request.form.get('score1'))
        score2 = safe_int(request.form.get('score2'))
        validation_error = validate_series_result(
            winner_id=winner_id,
            score1=score1,
            score2=score2,
            player1_id=room['player1_id'],
            player2_id=room['player2_id'],
            best_of=room['best_of'],
        )
        if validation_error:
            flash(validation_error, 'error')
            return redirect(url_for('practice_room_detail', room_id=room_id))
        admin_note = request.form.get('admin_note', '').strip()
        if is_admin():
            finalize_practice_room(room_id, winner_id, score1, score2, admin_note or 'Admin override')
            flash('Practice room finalized by admin.', 'success')
            return redirect(url_for('practice_room_detail', room_id=room_id))
        if current['id'] == room['player1_id']:
            execute('UPDATE practice_rooms SET player1_report_winner_id = ?, player1_report_score1 = ?, player1_report_score2 = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, score1, score2, 'awaiting_confirmation', now_str(), room_id))
        elif current['id'] == room['player2_id']:
            execute('UPDATE practice_rooms SET player2_report_winner_id = ?, player2_report_score1 = ?, player2_report_score2 = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, score1, score2, 'awaiting_confirmation', now_str(), room_id))
        fresh = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
        p1r = fresh['player1_report_winner_id'] is not None
        p2r = fresh['player2_report_winner_id'] is not None
        if p1r and p2r:
            same = fresh['player1_report_winner_id'] == fresh['player2_report_winner_id'] and fresh['player1_report_score1'] == fresh['player2_report_score1'] and fresh['player1_report_score2'] == fresh['player2_report_score2']
            if same:
                finalize_practice_room(room_id, fresh['player1_report_winner_id'], fresh['player1_report_score1'], fresh['player1_report_score2'], 'Confirmed by both players.')
                flash('Both players confirmed the same result.', 'success')
            else:
                apply_practice_provisional_result(room_id, fresh['player1_report_winner_id'])
                flash('Conflicting reports saved. Provisional winner applied until admin review.', 'error')
        else:
            flash('Result submitted. Waiting for opponent confirmation.', 'success')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    map_draft = compute_map_draft(room) if room['status'] in ('map_draft', 'civ_ban', 'civ_draft', 'live', 'awaiting_confirmation', 'provisional', 'completed') else {'actions': [], 'available_maps': [], 'picked_maps': [], 'completed': False, 'display_text': '', 'current_player_id': None, 'current_action': None, 'final_maps': []}
    civ_draft = compute_civ_draft(room, current['id'] if current else None) if room['status'] in ('civ_ban', 'civ_draft', 'live', 'awaiting_confirmation', 'provisional', 'completed') else {'actions': [], 'ban_actions': [], 'hidden_actions': [], 'snipe_actions': [], 'player1_bans': [], 'player2_bans': [], 'banned_civs': [], 'player1_hidden': [], 'player2_hidden': [], 'reveal_ready': False, 'current_action': None, 'current_player_id': None, 'current_ban_options': [], 'current_pick_options': [], 'current_snipe_options': [], 'pending_snipe_player_ids': [], 'can_submit_hidden_snipe': False, 'player1_snipe': None, 'player2_snipe': None, 'player1_final': room['player1_final_civ'], 'player2_final': room['player2_final_civ'], 'completed': False}
    return render_template('practice_room.html', room=room, can_interact=can_interact, now_ts=now().timestamp(), map_draft=map_draft, civ_draft=civ_draft)


@app.route('/practice/<int:room_id>/accept', methods=['POST'])
@login_required
@rate_limit(20, 300, scope='user')
def accept_practice_room(room_id: int):
    room = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
    current = get_current_player()
    if not room or not current:
        return redirect(url_for('dashboard'))
    if current['id'] not in {room['player1_id'], room['player2_id']} and not is_admin():
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    if room['status'] != 'awaiting_accept':
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if current['id'] == room['player1_id']:
        execute('UPDATE practice_rooms SET accepted1 = 1, updated_at = ? WHERE id = ?', (now_str(), room_id))
    else:
        execute('UPDATE practice_rooms SET accepted2 = 1, updated_at = ? WHERE id = ?', (now_str(), room_id))
    fresh = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
    if fresh['accepted1'] and fresh['accepted2']:
        start_map_draft(room_id)
        flash('Both players accepted. Map draft started.', 'success')
    else:
        flash('Accepted. Waiting for the other player.', 'success')
    return redirect(url_for('practice_room_detail', room_id=room_id))


@app.route('/practice/<int:room_id>/decline', methods=['POST'])
@login_required
@rate_limit(20, 300, scope='user')
def decline_practice_room(room_id: int):
    room = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
    current = get_current_player()
    if not room or not current:
        return redirect(url_for('dashboard'))
    if current['id'] not in {room['player1_id'], room['player2_id']} and not is_admin():
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    other_id = room['player2_id'] if current['id'] == room['player1_id'] else room['player1_id']
    ensure_player_queued(other_id, room['queue_type'])
    cancel_practice_room(room_id, f'{current["display_name"]} declined the ready check.')
    attempt_pairing(room['queue_type'])
    flash('Ready check declined. The other player was returned to queue.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/practice/<int:room_id>/map-action', methods=['POST'])
@rate_limit(60, 300, scope='user')
def submit_map_action(room_id: int):
    room = get_practice_room(room_id)
    current = get_current_player()
    if not room or not current:
        flash('Missing room or player.', 'error')
        return redirect(url_for('dashboard'))
    if room['status'] != 'map_draft':
        flash('Map draft is not active for this room.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if current['id'] not in {room['player1_id'], room['player2_id']}:
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    state = compute_map_draft(room)
    if current['id'] != state['current_player_id']:
        flash('It is not your turn in the map draft.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    map_name = request.form.get('map_name', '').strip()
    if map_name not in state['available_maps']:
        flash('That map is no longer available.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    execute(
        'INSERT INTO practice_map_actions (room_id, turn_number, player_id, action_type, map_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, current['id'], state['current_action'], map_name, now_str()),
    )
    fresh_room = get_practice_room(room_id)
    fresh_state = compute_map_draft(fresh_room)
    if fresh_state['completed']:
        finish_map_draft(room_id)
        flash('Map draft finished. Civ draft started.', 'success')
    else:
        flash(f'{state["current_action"].title()} locked: {map_name}.', 'success')
    return redirect(url_for('practice_room_detail', room_id=room_id))


@app.route('/practice/<int:room_id>/civ-ban', methods=['POST'])
@rate_limit(60, 300, scope='user')
def submit_civ_ban(room_id: int):
    room = get_practice_room(room_id)
    current = get_current_player()
    if not room or not current:
        flash('Missing room or player.', 'error')
        return redirect(url_for('dashboard'))
    if room['status'] != 'civ_ban':
        flash('Civ bans are not active for this room.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if current['id'] not in {room['player1_id'], room['player2_id']}:
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    state = compute_civ_draft(room, current['id'])
    if state['current_action'] != 'ban' or current['id'] != state['current_player_id']:
        flash('It is not your turn to ban.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    if civ_name not in state['current_ban_options']:
        flash('That civilisation cannot be banned right now.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, current['id'], 'ban', civ_name, now_str()),
    )
    fresh_room = get_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, current['id'])
    if len(fresh_state['ban_actions']) >= 6:
        finish_civ_ban(room_id)
        flash('Civilisation bans finished. Hidden picks started.', 'success')
    else:
        flash(f'Ban locked: {civ_name}.', 'success')
    return redirect(url_for('practice_room_detail', room_id=room_id))


@app.route('/practice/<int:room_id>/civ-pick', methods=['POST'])
@rate_limit(60, 300, scope='user')
def submit_hidden_civ_pick(room_id: int):
    room = get_practice_room(room_id)
    current = get_current_player()
    if not room or not current:
        flash('Missing room or player.', 'error')
        return redirect(url_for('dashboard'))
    if room['status'] != 'civ_draft':
        flash('Civ draft is not active for this room.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if current['id'] not in {room['player1_id'], room['player2_id']}:
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    state = compute_civ_draft(room, current['id'])
    if state['current_action'] != 'hidden_pick' or current['id'] != state['current_player_id']:
        flash('It is not your turn to lock a hidden civ.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    own_hidden = state['player1_hidden'] if current['id'] == room['player1_id'] else state['player2_hidden']
    if civ_name in own_hidden:
        flash('You already locked that civ in this draft.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if civ_name not in AOE4_CIVS:
        flash('Unknown civilisation.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if civ_name not in state['current_pick_options']:
        flash('That civilisation is unavailable in this draft.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, current['id'], 'hidden_pick', civ_name, now_str()),
    )
    fresh_room = get_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, current['id'])
    if fresh_state['reveal_ready'] and fresh_state['current_action'] == 'hidden_snipe':
        flash('Hidden civs locked. Picks are revealed. Start the snipe phase.', 'success')
    else:
        flash('Hidden civ locked.', 'success')
    return redirect(url_for('practice_room_detail', room_id=room_id))


@app.route('/practice/<int:room_id>/civ-snipe', methods=['POST'])
@rate_limit(60, 300, scope='user')
def submit_civ_snipe(room_id: int):
    room = get_practice_room(room_id)
    current = get_current_player()
    if not room or not current:
        flash('Missing room or player.', 'error')
        return redirect(url_for('dashboard'))
    if room['status'] != 'civ_draft':
        flash('Civ draft is not active for this room.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    if current['id'] not in {room['player1_id'], room['player2_id']}:
        flash('You are not part of this room.', 'error')
        return redirect(url_for('dashboard'))
    state = compute_civ_draft(room, current['id'])
    if state['current_action'] != 'hidden_snipe' or not state['can_submit_hidden_snipe']:
        flash('Secret snipes are not open for you right now.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    if civ_name not in state['current_snipe_options']:
        flash('That civilisation cannot be sniped right now.', 'error')
        return redirect(url_for('practice_room_detail', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, current['id'], 'snipe', civ_name, now_str()),
    )
    fresh_room = get_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, current['id'])
    if fresh_state['completed']:
        finish_civ_draft(room_id)
        flash('Civ draft finished. The practice room is now live.', 'success')
    else:
        flash('Secret snipe locked. Waiting for the other player.', 'success')
    return redirect(url_for('practice_room_detail', room_id=room_id))


@app.route('/practice/<int:room_id>/state')
def practice_room_state(room_id: int):
    room = get_practice_room(room_id)
    if not room:
        return jsonify({'ok': False, 'missing': True}), 404
    map_count = query_one('SELECT COUNT(*) AS c FROM practice_map_actions WHERE room_id = ?', (room_id,))['c']
    civ_count = query_one('SELECT COUNT(*) AS c FROM practice_civ_actions WHERE room_id = ?', (room_id,))['c']
    return jsonify({
        'ok': True,
        'room_id': room_id,
        'status': room['status'],
        'updated_at': room['updated_at'],
        'map_actions': map_count,
        'civ_actions': civ_count,
        'accepted1': room['accepted1'],
        'accepted2': room['accepted2'],
        'winner_id': room['winner_id'],
        'provisional_winner_id': room['provisional_winner_id'],
        'chosen_map': room['chosen_map'],
        'player1_final_civ': room['player1_final_civ'],
        'player2_final_civ': room['player2_final_civ'],
    })


@app.route('/players', methods=['GET', 'POST'])
@rate_limit(10, 600, scope='user')
def players():
    current_user = get_current_user()
    current_player = get_current_player()
    search = request.args.get('q', '').strip()
    active_tab = request.args.get('tab', 'players').strip().lower()
    if active_tab not in {'players', 'teams', 'streamers', 'friends'}:
        active_tab = 'players'
    region = request.args.get('region', 'global').strip().upper() or 'GLOBAL'
    civ_filter = request.args.get('civ', '').strip()
    if request.method == 'POST':
        if not current_user:
            flash('Create an account or log in first.', 'error')
            return redirect(url_for('register_account'))
        if current_player:
            flash('Your account already has a player profile.', 'error')
            return redirect(url_for('player_detail', player_id=current_player['id']))
        display_name = request.form.get('display_name', '').strip()
        country = request.form.get('country', '').strip().upper()
        main_civ = request.form.get('main_civ', '').strip()
        bio = request.form.get('bio', '').strip()
        if not display_name:
            flash('Display name is required.', 'error')
        else:
            try:
                player_id = execute('INSERT INTO players (user_id, display_name, country, main_civ, bio, created_at) VALUES (?, ?, ?, ?, ?, ?)', (current_user['id'], display_name, country, main_civ, bio, now_str()))
                execute('UPDATE users SET player_id = ? WHERE id = ?', (player_id, current_user['id']))
                log_audit_event(event_type='player_profile_created', detail=f'Player profile created for {display_name}.', user_id=current_user['id'], player_id=player_id)
                flash('Player profile created.', 'success')
                return redirect(url_for('player_detail', player_id=player_id))
            except sqlite3.IntegrityError:
                flash('That display name already exists.', 'error')
    country_filter = '' if region == 'GLOBAL' else region
    rows = filtered_public_players(search=search, country=country_filter, civ=civ_filter)
    ladder_rows = []
    for idx, row in enumerate(rows, start=1):
        ladder_rows.append({'rank': idx, 'player': row, 'stats': player_stats(row['id'])})
    most_active_rows = sorted(ladder_rows, key=lambda item: (item['stats']['played'], item['player']['rating']), reverse=True)[:4]
    return render_template(
        'players.html',
        ladder_rows=ladder_rows,
        most_active_rows=most_active_rows,
        search=search,
        active_tab=active_tab,
        region=region,
        civ_filter=civ_filter,
        featured_teams=featured_team_cards(),
        streamer_rows=ladder_rows[:6],
        friend_rows=friend_rows_for_player(current_player['id'] if current_player else None),
    )


@app.route('/player/<int:player_id>', methods=['GET', 'POST'])
@rate_limit(20, 600, scope='user')
def player_detail(player_id: int):
    player = query_one('SELECT * FROM players WHERE id = ? AND COALESCE(is_shadow,0)=0', (player_id,))
    if not player:
        flash('Player not found.', 'error')
        return redirect(url_for('players'))
    current = get_current_player()
    if request.method == 'POST':
        if not current or (current['id'] != player_id and not is_admin()):
            flash('You can only edit your own profile.', 'error')
            return redirect(url_for('player_detail', player_id=player_id))
        trust_score = safe_int(request.form.get('trust_score', player['trust_score']))
        if trust_score is None:
            flash('Trust score must be a whole number.', 'error')
            return redirect(url_for('player_detail', player_id=player_id))
        trust_score = clamp(trust_score, 0, 100)
        execute('UPDATE players SET country = ?, main_civ = ?, bio = ?, trust_score = ? WHERE id = ?', (request.form.get('country', '').strip().upper(), request.form.get('main_civ', '').strip(), request.form.get('bio', '').strip(), trust_score, player_id))
        flash('Profile updated.', 'success')
        return redirect(url_for('player_detail', player_id=player_id))
    stats = player_stats(player_id)
    history = player_history(player_id)
    player_tournaments = my_registrations(player_id)
    current_room = get_player_active_room(player_id)
    rank_position = player_rank_position(player_id)
    tournament_wins = query_one('SELECT COUNT(*) AS c FROM tournaments WHERE winner_id = ?', (player_id,))['c']
    recent_form = build_recent_form(history, player_id)
    return render_template(
        'player_detail.html',
        player=player,
        stats=stats,
        history=history,
        player_tournaments=player_tournaments,
        current_room=current_room,
        rank_position=rank_position,
        tournament_wins=tournament_wins,
        recent_form=recent_form,
        has_edit_access=bool(current and (current['id'] == player_id or is_admin())),
    )


@app.route('/ladder')
def ladder():
    mode = request.args.get('mode', '1v1').strip().lower()
    if mode not in {'1v1', '2v2', 'team', 'season'}:
        mode = '1v1'
    view = request.args.get('view', 'leaderboard').strip().lower()
    if view not in {'leaderboard', 'my-rank', 'rewards', 'rules'}:
        view = 'leaderboard'
    search = request.args.get('q', '').strip()
    region = request.args.get('region', 'global').strip().upper() or 'GLOBAL'
    rank_filter = request.args.get('rank', 'all').strip().lower() or 'all'
    rows = filtered_public_players(search=search, country='' if region == 'GLOBAL' else region)
    if rank_filter != 'all':
        rows = [row for row in rows if rating_band(row['rating']) == rank_filter]
    ladder_rows = []
    for idx, row in enumerate(rows, start=1):
        ladder_rows.append({'rank': idx, 'player': row, 'stats': player_stats(row['id'])})
    current_player = get_current_player()
    current_player_row = next((row for row in ladder_rows if current_player and row['player']['id'] == current_player['id']), None)
    season_window = {
        'name': 'Spring Clash 2026',
        'phase': 'Mid season',
        'ends_at': '2026-05-31 23:59',
    }
    return render_template(
        'ladder.html',
        ladder_rows=ladder_rows,
        recent=recent_results(6),
        mode=mode,
        view=view,
        search=search,
        region=region,
        rank_filter=rank_filter,
        current_player_row=current_player_row,
        season_window=season_window,
    )


@app.route('/tournaments', methods=['GET', 'POST'])
@rate_limit(10, 3600, scope='user')
def tournaments():
    active_tab = request.args.get('tab', 'tournaments').strip().lower()
    if active_tab not in {'tournaments', 'teams', 'streamers'}:
        active_tab = 'tournaments'
    search = request.args.get('q', '').strip()
    status_filter = request.args.get('status', 'all').strip().lower()
    if request.method == 'POST':
        if not is_admin():
            flash('Admin access is required to create cups.', 'error')
            return redirect(url_for('tournaments', tab=active_tab, q=search, status=status_filter))
        name = request.form.get('name', '').strip() or f'Aoe4IT Weekly Cup {now().strftime("%Y-%m-%d")}'
        starts_at = request.form.get('starts_at', '').strip() or (now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        tid = create_weekly_tournament(name, starts_at)
        flash('Weekly cup created.', 'success')
        return redirect(url_for('tournament_detail', tournament_id=tid))
    rows = active_tournaments()
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in (row['name'] or '').lower() or needle in (row['winner_name'] or '').lower()]
    if status_filter in {'open', 'live', 'completed'}:
        rows = [row for row in rows if row['status'] == status_filter]
    featured = next((r for r in rows if r['status'] in ('live', 'open')), rows[0] if rows else None)
    summary = {
        'open': sum(1 for r in rows if r['status'] == 'open'),
        'live': sum(1 for r in rows if r['status'] == 'live'),
        'completed': sum(1 for r in rows if r['status'] == 'completed'),
        'registrations': sum((r['reg_count'] or 0) for r in rows),
    }
    return render_template(
        'tournaments.html',
        tournaments=rows,
        featured=featured,
        summary=summary,
        active_tab=active_tab,
        search=search,
        status_filter=status_filter,
        featured_teams=featured_team_cards(4),
        streamer_rows=public_players()[:6],
    )


@app.route('/tournament/<int:tournament_id>')
def tournament_detail(tournament_id: int):
    tournament = query_one('SELECT t.*, p.display_name AS winner_name FROM tournaments t LEFT JOIN players p ON p.id = t.winner_id WHERE t.id = ?', (tournament_id,))
    if not tournament:
        flash('Tournament not found.', 'error')
        return redirect(url_for('tournaments'))
    registrations = query_all(
        'SELECT p.*, r.seed, r.created_at AS registered_at FROM registrations r JOIN players p ON p.id = r.player_id WHERE r.tournament_id = ? ORDER BY COALESCE(r.seed,999), p.rating DESC, p.display_name ASC',
        (tournament_id,),
    )
    current = get_current_player()
    is_registered = bool(current and tournament_player_registered(tournament_id, current['id']))
    groups = tournament_matches_grouped(tournament_id)
    summary = {
        'registered': len(registrations),
        'live_matches': query_one("SELECT COUNT(*) AS c FROM matches WHERE tournament_id = ? AND status = 'live'", (tournament_id,))['c'],
        'completed_matches': query_one("SELECT COUNT(*) AS c FROM matches WHERE tournament_id = ? AND status = 'completed'", (tournament_id,))['c'],
        'pending_matches': query_one("SELECT COUNT(*) AS c FROM matches WHERE tournament_id = ? AND status IN ('pending','awaiting_confirmation','provisional')", (tournament_id,))['c'],
    }
    all_matches = [match for group in groups for match in group['matches']]
    total_matches = len(all_matches)
    completion_pct = round((summary['completed_matches'] / total_matches) * 100) if total_matches else 0
    round_total = max((match['round_number'] for match in all_matches), default=0)
    next_match = next((match for match in all_matches if match['status'] in ('live', 'pending', 'awaiting_confirmation', 'provisional')), None)
    slot_target = next_power_of_two(len(registrations)) if len(registrations) > 1 else 8
    return render_template(
        'tournament_detail.html',
        tournament=tournament,
        registrations=registrations,
        reg_count=len(registrations),
        groups=groups,
        is_registered=is_registered,
        summary=summary,
        total_matches=total_matches,
        completion_pct=completion_pct,
        round_total=round_total,
        slot_target=slot_target,
        next_match=next_match,
    )


@app.route('/tournament/<int:tournament_id>/register', methods=['POST'])
@login_required
@rate_limit(12, 600, scope='user')
def register_tournament(tournament_id: int):
    current = get_current_player()
    tournament = query_one('SELECT * FROM tournaments WHERE id = ?', (tournament_id,))
    if not current:
        flash('Create your player profile before joining a cup.', 'error')
        return redirect(url_for('players'))
    if not tournament or tournament['status'] != 'open':
        flash('Registration is closed.', 'error')
        return redirect(url_for('tournament_detail', tournament_id=tournament_id))
    if tournament_player_registered(tournament_id, current['id']):
        flash('You are already registered.', 'error')
    else:
        execute('INSERT INTO registrations (tournament_id, player_id, seed, created_at) VALUES (?, ?, ?, ?)', (tournament_id, current['id'], tournament_registration_count(tournament_id) + 1, now_str()))
        current_user = get_current_user()
        linked_user = query_one('SELECT * FROM users WHERE id = ?', (current_user['id'],)) if current_user else query_one('SELECT * FROM users WHERE player_id = ?', (current['id'],))
        log_audit_event(event_type='tournament_registered', detail=f"Registered for tournament: {tournament['name']}.", user_id=(linked_user['id'] if linked_user else None), player_id=current['id'], tournament_id=tournament_id)
        email_note = ''
        if linked_user:
            sent, _detail = send_tournament_registration_email(linked_user, current, tournament)
            if sent:
                email_note = ' Confirmation email sent.'
            elif email_enabled():
                email_note = ' Registration saved, but confirmation email failed.'
            else:
                email_note = ' Registration saved. Email is not configured yet.'
        flash(f'Registration stored.{email_note}', 'success' if 'failed' not in email_note.lower() else 'error')
    return redirect(url_for('tournament_detail', tournament_id=tournament_id))


@app.route('/tournament/<int:tournament_id>/withdraw', methods=['POST'])
@login_required
@rate_limit(12, 600, scope='user')
def withdraw_tournament(tournament_id: int):
    current = get_current_player()
    tournament = query_one('SELECT * FROM tournaments WHERE id = ?', (tournament_id,))
    if not current or not tournament:
        flash('Missing player or tournament.', 'error')
        return redirect(url_for('tournaments'))
    if tournament['status'] != 'open':
        flash('You cannot withdraw after the bracket starts.', 'error')
    else:
        execute('DELETE FROM registrations WHERE tournament_id = ? AND player_id = ?', (tournament_id, current['id']))
        current_user = get_current_user()
        log_audit_event(event_type='tournament_withdrew', detail=f"Withdrew from tournament: {tournament['name']}.", user_id=(current_user['id'] if current_user else None), player_id=current['id'], tournament_id=tournament_id)
        flash('You withdrew from the weekly cup.', 'success')
    return redirect(url_for('tournament_detail', tournament_id=tournament_id))


@app.route('/tournament/<int:tournament_id>/start', methods=['POST'])
@admin_required
@rate_limit(6, 3600, scope='user')
def start_tournament(tournament_id: int):
    try:
        generate_bracket(tournament_id)
        flash('Bracket generated. Cup is now live.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('tournament_detail', tournament_id=tournament_id))


@app.route('/match/<int:match_id>', methods=['GET', 'POST'])
@rate_limit(20, 600, scope='user')
def match_detail(match_id: int):
    match = query_one(
        """
        SELECT m.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               pw.display_name AS provisional_winner_name, t.name AS tournament_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        LEFT JOIN players pw ON pw.id = m.provisional_winner_id
        LEFT JOIN tournaments t ON t.id = m.tournament_id
        WHERE m.id = ?
        """,
        (match_id,),
    )
    if not match:
        flash('Match not found.', 'error')
        return redirect(url_for('tournaments'))
    current = get_current_player()
    can_report = bool(current and current['id'] in {match['player1_id'], match['player2_id']}) or is_admin()
    if request.method == 'POST':
        if not can_report:
            flash('You are not allowed to report this match.', 'error')
            return redirect(url_for('match_detail', match_id=match_id))
        winner_id = safe_int(request.form.get('winner_id'))
        score1 = safe_int(request.form.get('score1'))
        score2 = safe_int(request.form.get('score2'))
        validation_error = validate_series_result(
            winner_id=winner_id,
            score1=score1,
            score2=score2,
            player1_id=match['player1_id'],
            player2_id=match['player2_id'],
            best_of=match['best_of'],
        )
        if validation_error:
            flash(validation_error, 'error')
            return redirect(url_for('match_detail', match_id=match_id))
        admin_note = request.form.get('admin_note', '').strip()
        if is_admin():
            finalize_tournament_match(match_id, winner_id, score1, score2, admin_note or 'Admin override result.')
            flash('Tournament match finalized by admin.', 'success')
            return redirect(url_for('match_detail', match_id=match_id))
        if current['id'] == match['player1_id']:
            execute('UPDATE matches SET player1_report_winner_id = ?, player1_report_score1 = ?, player1_report_score2 = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, score1, score2, 'awaiting_confirmation', now_str(), match_id))
        elif current['id'] == match['player2_id']:
            execute('UPDATE matches SET player2_report_winner_id = ?, player2_report_score1 = ?, player2_report_score2 = ?, status = ?, updated_at = ? WHERE id = ?', (winner_id, score1, score2, 'awaiting_confirmation', now_str(), match_id))
        fresh = query_one('SELECT * FROM matches WHERE id = ?', (match_id,))
        p1r = fresh['player1_report_winner_id'] is not None
        p2r = fresh['player2_report_winner_id'] is not None
        if p1r and p2r:
            same = fresh['player1_report_winner_id'] == fresh['player2_report_winner_id'] and fresh['player1_report_score1'] == fresh['player2_report_score1'] and fresh['player1_report_score2'] == fresh['player2_report_score2']
            if same:
                finalize_tournament_match(match_id, fresh['player1_report_winner_id'], fresh['player1_report_score1'], fresh['player1_report_score2'], 'Confirmed by both players.')
                flash('Both players confirmed the same result.', 'success')
            else:
                apply_tournament_provisional_result(match_id, fresh['player1_report_winner_id'])
                flash('Conflicting reports saved. Provisional winner applied until admin review.', 'error')
        else:
            flash('Result submitted. Waiting for opponent confirmation.', 'success')
        return redirect(url_for('match_detail', match_id=match_id))
    p1_stats = player_stats(match['player1_id']) if match['player1_id'] else None
    p2_stats = player_stats(match['player2_id']) if match['player2_id'] else None
    next_match = query_one(
        """
        SELECT m.*, p1.display_name AS player1_name, p2.display_name AS player2_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        WHERE m.id = ?
        """,
        (match['next_match_id'],),
    ) if match['next_match_id'] else None
    round_total_row = query_one('SELECT MAX(round_number) AS max_round FROM matches WHERE tournament_id = ?', (match['tournament_id'],))
    round_total = round_total_row['max_round'] if round_total_row and round_total_row['max_round'] else match['round_number']
    my_reported = False
    opponent_reported = False
    if current and current['id'] == match['player1_id']:
        my_reported = match['player1_report_winner_id'] is not None
        opponent_reported = match['player2_report_winner_id'] is not None
    elif current and current['id'] == match['player2_id']:
        my_reported = match['player2_report_winner_id'] is not None
        opponent_reported = match['player1_report_winner_id'] is not None
    return render_template(
        'match_detail.html',
        match=match,
        can_report=can_report,
        p1_stats=p1_stats,
        p2_stats=p2_stats,
        next_match=next_match,
        round_total=round_total,
        my_reported=my_reported,
        opponent_reported=opponent_reported,
    )


@app.route('/admin')
@admin_required
def admin():
    pending_matches = query_all(
        """
        SELECT m.*, p1.display_name AS player1_name, p2.display_name AS player2_name,
               pw.display_name AS provisional_winner_name, t.name AS tournament_name
        FROM matches m
        LEFT JOIN players p1 ON p1.id = m.player1_id
        LEFT JOIN players p2 ON p2.id = m.player2_id
        LEFT JOIN players pw ON pw.id = m.provisional_winner_id
        LEFT JOIN tournaments t ON t.id = m.tournament_id
        WHERE m.status IN ('awaiting_confirmation', 'provisional')
        ORDER BY m.updated_at DESC
        """
    )
    recent_signups = query_all('SELECT id, username, email, created_at, player_id FROM users ORDER BY id DESC LIMIT 12')
    recent_events = query_all(
        """
        SELECT a.*, u.username, p.display_name AS player_name, t.name AS tournament_name
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        LEFT JOIN players p ON p.id = a.player_id
        LEFT JOIN tournaments t ON t.id = a.tournament_id
        ORDER BY a.id DESC
        LIMIT 16
        """
    )
    email_log = query_all(
        """
        SELECT e.*, u.username, p.display_name AS player_name, t.name AS tournament_name
        FROM email_delivery_log e
        LEFT JOIN users u ON u.id = e.user_id
        LEFT JOIN players p ON p.id = e.player_id
        LEFT JOIN tournaments t ON t.id = e.tournament_id
        ORDER BY e.id DESC
        LIMIT 20
        """
    )
    return render_template(
        'admin.html',
        pending_matches=pending_matches,
        pending_practice=pending_admin_practice_rooms(),
        tournaments=active_tournaments(),
        recent_signups=recent_signups,
        recent_events=recent_events,
        email_log=email_log,
        email_enabled_now=email_enabled(),
        email_provider=current_email_provider(),
        runtime_summary=runtime_config_summary(),
    )


@app.route('/admin/match/<int:match_id>/resolve', methods=['POST'])
@admin_required
@rate_limit(20, 600, scope='user')
def admin_resolve_match(match_id: int):
    match = query_one('SELECT * FROM matches WHERE id = ?', (match_id,))
    if not match:
        flash('Match not found.', 'error')
        return redirect(url_for('admin'))
    winner_id = safe_int(request.form.get('winner_id'))
    score1 = safe_int(request.form.get('score1'))
    score2 = safe_int(request.form.get('score2'))
    validation_error = validate_series_result(
        winner_id=winner_id,
        score1=score1,
        score2=score2,
        player1_id=match['player1_id'],
        player2_id=match['player2_id'],
        best_of=match['best_of'],
    )
    if validation_error:
        flash(validation_error, 'error')
        return redirect(url_for('admin'))
    admin_note = request.form.get('admin_note', '').strip() or 'Resolved by admin.'
    finalize_tournament_match(match_id, winner_id, score1, score2, admin_note)
    flash('Tournament match resolved.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/practice/<int:room_id>/resolve', methods=['POST'])
@admin_required
@rate_limit(20, 600, scope='user')
def admin_resolve_practice(room_id: int):
    room = query_one('SELECT * FROM practice_rooms WHERE id = ?', (room_id,))
    if not room:
        flash('Practice room not found.', 'error')
        return redirect(url_for('admin'))
    winner_id = safe_int(request.form.get('winner_id'))
    score1 = safe_int(request.form.get('score1'))
    score2 = safe_int(request.form.get('score2'))
    validation_error = validate_series_result(
        winner_id=winner_id,
        score1=score1,
        score2=score2,
        player1_id=room['player1_id'],
        player2_id=room['player2_id'],
        best_of=room['best_of'],
    )
    if validation_error:
        flash(validation_error, 'error')
        return redirect(url_for('admin'))
    admin_note = request.form.get('admin_note', '').strip() or 'Resolved by admin.'
    finalize_practice_room(room_id, winner_id, score1, score2, admin_note)
    flash('Practice room resolved.', 'success')
    return redirect(url_for('admin'))


@app.route('/settings', methods=['GET', 'POST'])
@login_required
@rate_limit(6, 3600, scope='user')
def settings():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'reset_demo' and is_admin():
            db = get_db()
            db.close()
            g.pop('db', None)
            if DB_PATH.exists():
                DB_PATH.unlink()
            init_db()
            session.clear()
            flash('Database reset complete.', 'success')
            return redirect(url_for('login'))
    return render_template('settings.html')


# -------------------------
# Optional shadow lab
# -------------------------
@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/state')
@shadow_lab_required
def hidden_draft_room_state(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        return jsonify({'ok': False, 'missing': True}), 404
    map_count = query_one('SELECT COUNT(*) AS c FROM practice_map_actions WHERE room_id = ?', (room_id,))['c']
    civ_count = query_one('SELECT COUNT(*) AS c FROM practice_civ_actions WHERE room_id = ?', (room_id,))['c']
    return jsonify({
        'ok': True,
        'room_id': room_id,
        'status': room['status'],
        'updated_at': room['updated_at'],
        'map_actions': map_count,
        'civ_actions': civ_count,
        'chosen_map': room['chosen_map'],
        'player1_final_civ': room['player1_final_civ'],
        'player2_final_civ': room['player2_final_civ'],
    })


@app.route('/_shadow/ice-raven-draft-lab', methods=['GET', 'POST'])
@shadow_lab_required
@rate_limit(20, 600, scope='ip')
def hidden_draft_lab():
    players = ensure_hidden_lab_profiles()
    hidden_rooms = recent_hidden_practice_rooms(12)
    defaults = {'alpha': None, 'beta': None}
    for player in players:
        if player['display_name'] == 'Aoe4IT Lab Alpha':
            defaults['alpha'] = player['id']
        elif player['display_name'] == 'Aoe4IT Lab Beta':
            defaults['beta'] = player['id']
    if request.method == 'POST':
        player1_id = safe_int(request.form.get('player1_id'), 0) or 0
        player2_id = safe_int(request.form.get('player2_id'), 0) or 0
        best_of = safe_int(request.form.get('best_of'), 1) or 1
        random_starter = request.form.get('random_starter') == '1'
        if player1_id == 0 or player2_id == 0 or player1_id == player2_id:
            flash('Pick two different players for the lab room.', 'error')
            return redirect(url_for('hidden_draft_lab'))
        if best_of not in (1, 3):
            best_of = 1
        starter_id = random.choice([player1_id, player2_id]) if random_starter else None
        room_id = create_hidden_draft_room(player1_id, player2_id, best_of, starter_id=starter_id)
        flash('Draft room ready.', 'success')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    return render_template('hidden_draft_lab.html', players=players, hidden_rooms=hidden_rooms, defaults=defaults)


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>')
def hidden_draft_room(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Hidden draft lab room not found.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    map_draft = compute_map_draft(room)
    civ_draft = compute_civ_draft(room, None)
    return render_template(
        'hidden_practice_room.html',
        room=room,
        map_draft=map_draft,
        civ_draft=civ_draft,
        aoe4_civs=AOE4_CIVS,
        civ_flags=CIV_FLAG_FILES,
    )


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/swap-sides', methods=['POST'])
@shadow_lab_required
@rate_limit(30, 300, scope='ip')
def hidden_swap_sides(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Hidden draft room not found.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    swap_hidden_draft_room_sides(room_id)
    flash('Sides swapped and room reset.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/rematch', methods=['POST'])
@shadow_lab_required
@rate_limit(30, 300, scope='ip')
def hidden_rematch(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Hidden draft room not found.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    swap_sides = request.form.get('swap_sides') == '1'
    random_starter = request.form.get('random_starter') == '1'
    new_room_id = rematch_hidden_draft_room(room_id, swap_sides=swap_sides, random_starter=random_starter)
    if not new_room_id:
        flash('Could not create rematch.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    flash('Rematch room created.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=new_room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/random-first', methods=['POST'])
@shadow_lab_required
@rate_limit(30, 300, scope='ip')
def hidden_random_first(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Hidden draft room not found.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    starter_id = choose_hidden_room_random_starter(room_id)
    fresh = get_hidden_practice_room(room_id)
    if not starter_id or not fresh:
        flash('Could not randomize the opener.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    starter_name = fresh['player1_name'] if starter_id == fresh['player1_id'] else fresh['player2_name']
    flash(f'{starter_name} opens the draft.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/map-action', methods=['POST'])
@shadow_lab_required
@rate_limit(60, 300, scope='ip')
def hidden_submit_map_action(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Missing hidden draft room.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    if room['status'] != 'map_draft':
        flash('Map draft is not active for this hidden room.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    actor_id = safe_int(request.form.get('actor_id'), 0) or 0
    state = compute_map_draft(room)
    if actor_id != state['current_player_id']:
        flash('That is not the current map draft turn.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    map_name = request.form.get('map_name', '').strip()
    if map_name not in state['available_maps']:
        flash('That map is no longer available.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    execute(
        'INSERT INTO practice_map_actions (room_id, turn_number, player_id, action_type, map_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, actor_id, state['current_action'], map_name, now_str()),
    )
    fresh_room = get_hidden_practice_room(room_id)
    fresh_state = compute_map_draft(fresh_room)
    if fresh_state['completed']:
        finish_map_draft(room_id)
        flash('Hidden lab map draft finished. Civ draft started.', 'success')
    else:
        flash(f'{state["current_action"].title()} locked: {map_name}.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/civ-ban', methods=['POST'])
@shadow_lab_required
@rate_limit(60, 300, scope='ip')
def hidden_submit_civ_ban(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Missing hidden draft room.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    if room['status'] != 'civ_ban':
        flash('Civ bans are not active for this hidden room.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    actor_id = safe_int(request.form.get('actor_id'), 0) or 0
    state = compute_civ_draft(room, actor_id)
    if state['current_action'] != 'ban' or actor_id != state['current_player_id']:
        flash('That is not the current civ ban turn.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    if civ_name not in state['current_ban_options']:
        flash('That civilisation cannot be banned right now.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, actor_id, 'ban', civ_name, now_str()),
    )
    fresh_room = get_hidden_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, actor_id)
    if len(fresh_state['ban_actions']) >= 6:
        finish_civ_ban(room_id)
        flash('Hidden lab civ bans finished. Hidden picks started.', 'success')
    else:
        flash(f'Ban locked: {civ_name}.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/civ-pick', methods=['POST'])
@shadow_lab_required
@rate_limit(60, 300, scope='ip')
def hidden_submit_civ_pick(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Missing hidden draft room.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    if room['status'] != 'civ_draft':
        flash('Civ draft is not active for this hidden room.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    actor_id = safe_int(request.form.get('actor_id'), 0) or 0
    state = compute_civ_draft(room, actor_id)
    if state['current_action'] != 'hidden_pick' or actor_id != state['current_player_id']:
        flash('That is not the current hidden pick turn.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    own_hidden = state['player1_hidden'] if actor_id == room['player1_id'] else state['player2_hidden']
    if civ_name in own_hidden:
        flash('That civ is already locked by this side.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    if civ_name not in AOE4_CIVS:
        flash('Unknown civilisation.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    if civ_name not in state['current_pick_options']:
        flash('That civilisation is unavailable in this hidden draft.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, actor_id, 'hidden_pick', civ_name, now_str()),
    )
    fresh_room = get_hidden_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, actor_id)
    if fresh_state['reveal_ready'] and fresh_state['current_action'] == 'hidden_snipe':
        flash('All hidden civs are locked. Picks are revealed. Secret snipes are open.', 'success')
    else:
        flash('Hidden civ locked in the lab room.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/civ-snipe', methods=['POST'])
@shadow_lab_required
@rate_limit(60, 300, scope='ip')
def hidden_submit_civ_snipe(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Missing hidden draft room.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    if room['status'] != 'civ_draft':
        flash('Civ draft is not active for this hidden room.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    actor_id = safe_int(request.form.get('actor_id'), 0) or 0
    state = compute_civ_draft(room, actor_id)
    if state['current_action'] != 'hidden_snipe' or actor_id not in state['pending_snipe_player_ids']:
        flash('Secret snipes are not open for that side right now.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    civ_name = request.form.get('civ_name', '').strip()
    if civ_name not in state['current_snipe_options']:
        flash('That civilisation cannot be sniped right now.', 'error')
        return redirect(url_for('hidden_draft_room', room_id=room_id))
    execute(
        'INSERT INTO practice_civ_actions (room_id, turn_number, player_id, action_type, civ_name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room_id, len(state['actions']) + 1, actor_id, 'snipe', civ_name, now_str()),
    )
    fresh_room = get_hidden_practice_room(room_id)
    fresh_state = compute_civ_draft(fresh_room, actor_id)
    if fresh_state['completed']:
        finish_civ_draft(room_id)
        flash('Hidden lab civ draft finished. Final civs are locked.', 'success')
    else:
        flash('Snipe locked in the hidden lab room.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


@app.route('/_shadow/ice-raven-draft-lab/<int:room_id>/reset', methods=['POST'])
@shadow_lab_required
@rate_limit(30, 300, scope='ip')
def hidden_reset_draft(room_id: int):
    room = get_hidden_practice_room(room_id)
    if not room:
        flash('Hidden draft lab room not found.', 'error')
        return redirect(url_for('hidden_draft_lab'))
    reset_hidden_draft_room(room_id)
    flash('Hidden draft lab room reset.', 'success')
    return redirect(url_for('hidden_draft_room', room_id=room_id))


def runtime_status_snapshot() -> dict:
    summary = runtime_config_summary()
    return {
        **summary,
        'db_exists': DB_PATH.exists(),
        'counts': {
            'users': db_scalar('SELECT COUNT(*) AS c FROM users'),
            'players': db_scalar('SELECT COUNT(*) AS c FROM players'),
            'tournaments': db_scalar('SELECT COUNT(*) AS c FROM tournaments'),
            'matches': db_scalar('SELECT COUNT(*) AS c FROM matches'),
        },
    }



@app.errorhandler(404)
def not_found(_error):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_error(error):
    if hasattr(g, '_database'):
        try:
            g._database.rollback()
        except Exception:
            pass
    app.logger.exception('Unhandled application error: %s', error)
    return render_template('500.html'), 500


@app.route('/healthz')
def healthz():
    try:
        query_one('SELECT 1 AS ok')
        summary = runtime_config_summary()
        return {
            'ok': True,
            'app_env': APP_ENV,
            'database_provider': DATABASE_PROVIDER,
            'db_path': str(DB_PATH),
            'warnings': summary['warnings'],
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}, 500


@app.route('/readyz')
def readyz():
    try:
        query_one('SELECT 1 AS ok')
        upsert_runtime_state('last_readyz', now_str())
        summary = runtime_config_summary()
        if summary['errors']:
            return {'ok': False, 'errors': summary['errors'], 'warnings': summary['warnings']}, 500
        return {
            'ok': True,
            'app_env': APP_ENV,
            'database_decision': DATABASE_DECISION,
            'database_provider': DATABASE_PROVIDER,
            'db_path': str(DB_PATH),
            'warnings': summary['warnings'],
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}, 500


@app.route('/ops/runtime')
@admin_required
def ops_runtime():
    return jsonify(runtime_status_snapshot())


with app.app_context():
    init_db()


if __name__ == '__main__':
    debug_mode = APP_ENV == 'development'
    app.run(host='127.0.0.1', port=5055, debug=debug_mode)
