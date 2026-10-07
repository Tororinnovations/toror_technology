import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import smtplib
import sqlite3
import zipfile
import re
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import qrcode
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg
    REPORTING_AVAILABLE = True
except ImportError:  # pragma: no cover - deployment dependency guard
    REPORTING_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / os.environ.get('TOROR_DATA_DIR', 'data')
UPLOAD_DIR = BASE_DIR / 'static' / 'uploads'
DB_PATH = Path(os.environ.get('TOROR_DB_PATH', DATA_DIR / 'toror.db'))
STORE_UPLOAD_DIR = UPLOAD_DIR / 'store'
STORE_STAGING_DIR = DATA_DIR / 'store_staging'
STORE_APK_DIR = STORE_UPLOAD_DIR / 'apks'
STORE_WEB_DIR = STORE_UPLOAD_DIR / 'websites'
STORE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STORE_APK_DIR.mkdir(parents=True, exist_ok=True)
STORE_WEB_DIR.mkdir(parents=True, exist_ok=True)
STORE_STAGING_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=650 * 1024 * 1024,
)

ALLOWED_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
ALLOWED_DOC_EXTS = {'.pdf', '.xlsx', '.xls', '.docx', '.pptx', '.txt'}
ALLOWED_VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v'}
ALLOWED_PROJECT_EXTS = ALLOWED_IMAGE_EXTS | ALLOWED_DOC_EXTS | ALLOWED_VIDEO_EXTS
ALLOWED_APK_EXTS = {'.apk'}
ALLOWED_WEBSITE_EXTS = {'.zip', '.html', '.htm'}
MAX_STORE_ARTIFACT_BYTES = 500 * 1024 * 1024
MAX_WEBSITE_UNCOMPRESSED_BYTES = 700 * 1024 * 1024
MAX_WEBSITE_FILES = 12000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if 'db' not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _env_value(name, *fallbacks):
    """Read a Render environment variable, tolerating accidental surrounding quotes."""
    for key in (name, *fallbacks):
        value = os.environ.get(key)
        if value is None:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            return value
    return ''


def get_admin_name():
    return _env_value('ADMIN_NAME', 'ADMIN_USERNAME', 'ADMIN_USER')


def get_admin_password():
    return _env_value('ADMIN_PASSWORD', 'ADMIN_PASS', 'RENDER_ENV_PASSWORD')


def query_one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def query_all(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


def init_db():
    schema = '''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        company TEXT,
        subject TEXT,
        message TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'New',
        source TEXT NOT NULL DEFAULT 'Admin',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        link TEXT,
        status TEXT NOT NULL DEFAULT 'Active',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        original_name TEXT NOT NULL,
        mime_type TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS vault_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        mime_type TEXT,
        description TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS store_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL DEFAULT 'apk',
        short_description TEXT,
        category TEXT NOT NULL DEFAULT 'Other',
        description TEXT NOT NULL,
        price_kes REAL NOT NULL DEFAULT 0,
        payment_required INTEGER NOT NULL DEFAULT 0,
        premium_enabled INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        icon_path TEXT,
        access_instructions TEXT,
        current_version_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS store_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        version_label TEXT NOT NULL,
        artifact_path TEXT NOT NULL,
        original_name TEXT NOT NULL,
        mime_type TEXT,
        file_size INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT,
        release_notes TEXT,
        external_url TEXT,
        website_root TEXT,
        website_entry TEXT,
        is_current INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS store_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_code TEXT NOT NULL UNIQUE,
        buyer_name TEXT NOT NULL,
        buyer_phone TEXT NOT NULL,
        buyer_email TEXT,
        amount_expected REAL NOT NULL DEFAULT 0,
        amount_entered REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        manual_note TEXT,
        payment_code TEXT,
        payment_received_at TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS store_order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        version_id INTEGER,
        product_name_snapshot TEXT NOT NULL,
        unit_price REAL NOT NULL DEFAULT 0,
        access_token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payment_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        gateway_id TEXT,
        sim_device TEXT,
        sender TEXT,
        payer_name TEXT,
        payer_phone TEXT,
        amount REAL,
        transaction_code TEXT,
        raw_message TEXT NOT NULL,
        classification TEXT NOT NULL DEFAULT 'M-PESA CANDIDATE',
        delivery TEXT,
        matched_order_id INTEGER,
        match_reason TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS store_download_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        order_item_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        version_id INTEGER NOT NULL,
        product_name_snapshot TEXT NOT NULL,
        buyer_name TEXT NOT NULL,
        buyer_phone TEXT NOT NULL,
        buyer_email TEXT,
        version_label TEXT NOT NULL,
        download_kind TEXT NOT NULL DEFAULT 'initial',
        current_version_sent_by_client TEXT,
        user_agent TEXT,
        downloaded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_store_download_product_time ON store_download_events(product_id, downloaded_at);
    CREATE INDEX IF NOT EXISTS idx_store_download_order_item ON store_download_events(order_item_id, downloaded_at);
    CREATE TABLE IF NOT EXISTS accounting_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        product_id INTEGER,
        amount REAL NOT NULL DEFAULT 0,
        entry_type TEXT NOT NULL DEFAULT 'sale',
        reference TEXT,
        memo TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(order_id, product_id)
    );
    CREATE INDEX IF NOT EXISTS idx_store_products_active ON store_products(active, id);
    CREATE INDEX IF NOT EXISTS idx_store_versions_product ON store_versions(product_id, id);
    CREATE INDEX IF NOT EXISTS idx_store_orders_status ON store_orders(status, id);
    CREATE INDEX IF NOT EXISTS idx_payment_receipts_received ON payment_receipts(id);
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'visitor',
        owner_user_id INTEGER,
        owner_email TEXT,
        message TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        edited_at TEXT,
        edited INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS login_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS certificates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        serial TEXT NOT NULL UNIQUE,
        verification_sig TEXT NOT NULL,
        recipient_name TEXT NOT NULL,
        business_name TEXT NOT NULL,
        software_name TEXT NOT NULL,
        award_title TEXT NOT NULL,
        awarded_by TEXT NOT NULL,
        issuer_name TEXT,
        issuer_title TEXT,
        issuer_signature_path TEXT,
        award_date TEXT NOT NULL,
        notes TEXT,
        pdf_filename TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        location_text TEXT,
        location_lat REAL,
        location_lng REAL,
        device_info TEXT,
        ip_address TEXT,
        created_at TEXT NOT NULL,
        last_login_at TEXT,
        last_seen_at TEXT,
        is_active INTEGER NOT NULL DEFAULT 1
    );
    '''
    defaults = {
        'site_name': 'Toror Technology Company Ltd',
        'developer_name': get_admin_name() or 'Toror Technology Company Ltd',
        'tagline': 'Technology that turns ideas into working products.',
        'primary_email': os.environ.get('PRIMARY_EMAIL', ''),
        'primary_phone': os.environ.get('PRIMARY_PHONE', ''),
        'hero_text': 'We design, build, deploy, and support practical digital systems for organisations, businesses, and communities.',
        'hero_kicker': 'Technology • Software • Digital Solutions',
        'about_text': 'Toror Technology Company Ltd is a technology company focused on practical software, digital platforms, automation, and technology services that help organisations work better and serve people more effectively.',
        'history_text': 'Our history is built around learning by doing: understanding real operational problems, turning them into clear digital products, and continuing to improve those products as the needs of our clients grow.',
        'mission_text': 'To build useful, dependable technology that solves real problems and creates lasting value.',
        'vision_text': 'To become a trusted technology partner for organisations that want to modernise, simplify, and grow.',
        'service_text': 'Software development, web platforms, business systems, automation, deployment, and tailored digital solutions.',
        'address_text': '',
        'footer_text': 'Toror Technology Company Ltd — practical technology, thoughtfully built.',
        'meta_description': 'Toror Technology Company Ltd builds practical software, digital platforms, automation, and technology solutions.',
        'logo_path': '/static/default-logo.svg',
        'theme_mode': 'light',
        'accent_color': '#7B2431',
        'issuer_default_name': get_admin_name() or 'Chief Executive Officer',
        'issuer_default_title': 'Chief Executive Officer',
        'issuer_signature_path': '',
        'certificate_base_url': '',
        'certificate_default_title': 'Technology Partnership Recognition',
        'certificate_default_note': 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.',
        'login_enabled': '0',
        'chat_enabled': '0',
        'store_payment_instructions': 'M-PESA payment instructions have not been configured yet. Please contact Toror Technology Company Ltd before paying.',
        'store_gateway_secret': '',
        'store_match_window_hours': '48',
    }
    db = sqlite3.connect(DB_PATH)
    try:
        db.executescript(schema)
        for key, value in defaults.items():
            db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (key, value))
        legacy_to_new = {
            'site_name': ('Toror Technology and Innovations Ltd', 'Toror Technology Company Ltd'),
            'developer_name': ('Developed by me', get_admin_name() or 'Toror Technology Company Ltd'),
            'tagline': ('Toror Technology and Innovations Ltd', defaults['tagline']),
            'hero_text': ('Register to continue.', defaults['hero_text']),
            'primary_email': ('hello@example.com', ''),
        }
        for key, (legacy, updated) in legacy_to_new.items():
            db.execute('UPDATE settings SET value=? WHERE key=? AND value=?', (updated, key, legacy))
        professionalize_existing_content(db)
        db.commit()
    finally:
        db.close()

    # Lightweight migrations for older databases.
    db = sqlite3.connect(DB_PATH)
    try:
        db.row_factory = sqlite3.Row
        chat_cols = {row['name'] for row in db.execute('PRAGMA table_info(chat_messages)')}
        if 'owner_user_id' not in chat_cols:
            db.execute('ALTER TABLE chat_messages ADD COLUMN owner_user_id INTEGER')
        if 'owner_email' not in chat_cols:
            db.execute('ALTER TABLE chat_messages ADD COLUMN owner_email TEXT')
        contact_cols = {row['name'] for row in db.execute('PRAGMA table_info(contacts)')}
        for name, sql in {
            'company': 'ALTER TABLE contacts ADD COLUMN company TEXT',
            'subject': 'ALTER TABLE contacts ADD COLUMN subject TEXT',
            'message': 'ALTER TABLE contacts ADD COLUMN message TEXT',
            'status': "ALTER TABLE contacts ADD COLUMN status TEXT NOT NULL DEFAULT 'New'",
            'source': "ALTER TABLE contacts ADD COLUMN source TEXT NOT NULL DEFAULT 'Admin'",
        }.items():
            if name not in contact_cols:
                db.execute(sql)
        cert_cols = {row['name'] for row in db.execute('PRAGMA table_info(certificates)')}
        store_product_cols = {row['name'] for row in db.execute('PRAGMA table_info(store_products)')}
        if 'category' not in store_product_cols:
            db.execute("ALTER TABLE store_products ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
        store_version_cols = {row['name'] for row in db.execute('PRAGMA table_info(store_versions)')}
        if 'external_url' not in store_version_cols:
            db.execute('ALTER TABLE store_versions ADD COLUMN external_url TEXT')
        for name, sql in {
            'issuer_name': 'ALTER TABLE certificates ADD COLUMN issuer_name TEXT',
            'issuer_title': 'ALTER TABLE certificates ADD COLUMN issuer_title TEXT',
            'issuer_signature_path': 'ALTER TABLE certificates ADD COLUMN issuer_signature_path TEXT',
            'certificate_logo_path': 'ALTER TABLE certificates ADD COLUMN certificate_logo_path TEXT',
        }.items():
            if name not in cert_cols:
                db.execute(sql)
        db.execute("UPDATE certificates SET issuer_name=awarded_by WHERE (issuer_name IS NULL OR issuer_name='')")
        db.execute("INSERT INTO settings(key,value) VALUES ('theme_mode','light') ON CONFLICT(key) DO UPDATE SET value='light'")
        db.execute("INSERT INTO settings(key,value) VALUES ('accent_color','#7B2431') ON CONFLICT(key) DO UPDATE SET value='#7B2431'")
        db.commit()
    finally:
        db.close()


def professionalize_existing_content(db):
    """Bring bundled/legacy content to the current Toror public-site identity.

    Existing custom content is left alone unless it is one of the original placeholder
    values shipped with the app.
    """
    settings = {
        'site_name': 'Toror Technology Company Ltd',
        'tagline': 'Technology that turns ideas into working products.',
        'hero_kicker': 'Technology • Software • Digital Solutions',
        'hero_text': 'We design, build, deploy, and support practical digital systems for organisations, businesses, and communities.',
        'about_text': 'Toror Technology Company Ltd is a technology company focused on practical software, digital platforms, automation, and technology services that help organisations work better and serve people more effectively.',
        'history_text': 'Our history is built around learning by doing: understanding real operational problems, turning them into clear digital products, and continuing to improve those products as the needs of our clients grow.',
        'mission_text': 'To build useful, dependable technology that solves real problems and creates lasting value.',
        'vision_text': 'To become a trusted technology partner for organisations that want to modernise, simplify, and grow.',
        'service_text': 'Software development, web platforms, business systems, automation, deployment, and tailored digital solutions.',
        'footer_text': 'Toror Technology Company Ltd — practical technology, thoughtfully built.',
        'meta_description': 'Toror Technology Company Ltd builds practical software, digital platforms, automation, and technology solutions.',
        'logo_path': '/static/default-logo.svg',
        'theme_mode': 'light',
        'accent_color': '#7B2431',
        'issuer_default_name': get_admin_name() or 'Chief Executive Officer',
        'issuer_default_title': 'Chief Executive Officer',
        'issuer_signature_path': '',
        'certificate_base_url': '',
        'certificate_default_title': 'Technology Partnership Recognition',
        'certificate_default_note': 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.',
        'login_enabled': '0',
        'chat_enabled': '0',
        'store_payment_instructions': 'M-PESA payment instructions have not been configured yet. Please contact Toror Technology Company Ltd before paying.',
        'store_gateway_secret': '',
        'store_match_window_hours': '48',
    }
    # These values were placeholders/legacy values in the bundled database.
    replacements = {
        'site_name': {'Toror Technology and Innovations Ltd'},
        'tagline': {'Toror Technology and Innovations Ltd'},
        'hero_text': {'Register to continue.'},
        'about_text': {'Toror Technology and Innovations Ltd is a technology company.'},
        'logo_path': {'/static/default-logo.svg'},
    }
    for key, value in settings.items():
        current = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if not current:
            db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (key, value))
        elif key in replacements and current[0] in replacements[key]:
            db.execute('UPDATE settings SET value=? WHERE key=?', (value, key))
    # These four legacy website entries are no longer part of the public portfolio.
    # They now belong in Apps & Sites Store as website products, where the administrator
    # controls the upload, preview/summary, payment, premium status and access rules.
    legacy_store_site_links = {
        'https://oedge.onrender.com/',
        'https://denmart.co.ke/',
        'https://otravel-bleg.onrender.com/',
        'https://prime-1-rd0g.onrender.com/',
    }
    legacy_rows = db.execute(
        'SELECT id FROM projects WHERE link IN (%s)' % ','.join('?' for _ in legacy_store_site_links),
        tuple(legacy_store_site_links),
    ).fetchall()
    for row in legacy_rows:
        db.execute('DELETE FROM project_files WHERE project_id=?', (row[0],))
        db.execute('DELETE FROM projects WHERE id=?', (row[0],))


def get_setting(key, default=''):
    row = query_one('SELECT value FROM settings WHERE key=?', (key,))
    return row['value'] if row else default


def set_setting(key, value):
    execute(
        'INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value),
    )


def ensure_chat_privacy_schema():
    cols = {row['name'] for row in query_all('PRAGMA table_info(chat_messages)')}
    if 'owner_user_id' not in cols:
        execute('ALTER TABLE chat_messages ADD COLUMN owner_user_id INTEGER')
    if 'owner_email' not in cols:
        execute('ALTER TABLE chat_messages ADD COLUMN owner_email TEXT')


def backfill_chat_owner_ids():
    execute(
        '''
        UPDATE chat_messages
        SET owner_user_id = (
            SELECT id
            FROM users
            WHERE lower(users.email) = lower(chat_messages.sender)
               OR lower(users.username) = lower(chat_messages.sender)
            LIMIT 1
        )
        WHERE owner_user_id IS NULL
          AND role <> 'admin'
          AND sender IS NOT NULL
        '''
    )
    execute(
        '''
        UPDATE chat_messages
        SET owner_email = (
            SELECT email
            FROM users
            WHERE users.id = chat_messages.owner_user_id
            LIMIT 1
        )
        WHERE owner_user_id IS NOT NULL
          AND (owner_email IS NULL OR owner_email = '')
        '''
    )


def current_chat_owner_id():
    return session.get('user_id') if session.get('user_id') else None


def current_chat_sender():
    return session.get('user_email') or session.get('user_username') or 'admin'


def is_admin():
    return session.get('admin_logged_in') is True


def is_user_logged_in():
    return bool(session.get('user_id') or session.get('user_email'))


def user_display_name():
    if is_admin():
        return get_setting('developer_name', 'Team') or 'Team'
    user_id = session.get('user_id')
    if user_id:
        row = query_one('SELECT name FROM users WHERE id=?', (user_id,))
        if row and row['name']:
            return row['name']
    email = session.get('user_email')
    if email:
        row = query_one('SELECT name FROM users WHERE lower(email)=lower(?) LIMIT 1', (email,))
        if row and row['name']:
            return row['name']
        return email.split('@', 1)[0].replace('.', ' ').replace('_', ' ').title()
    return 'Guest'


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for('admin_entry'))
        return fn(*args, **kwargs)
    return wrapper


def user_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_user_logged_in() and not is_admin():
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper



@app.before_request
def protect_admin_routes():
    path = request.path or ''
    if path == '/promise212324' or path.startswith('/admin') or path.startswith('/api/admin'):
        if path in {'/promise212324'}:
            return None
        if not is_admin():
            return redirect(url_for('admin_entry'))
    return None


def allowed_file(filename, allowed_exts):
    return Path(filename).suffix.lower() in allowed_exts


def save_upload(file_storage, subdir='misc', allowed_exts=None):
    if not file_storage or not file_storage.filename:
        return None, None
    if allowed_exts is not None and not allowed_file(file_storage.filename, allowed_exts):
        raise ValueError('Unsupported file type.')
    safe_name = secure_filename(file_storage.filename)
    folder = UPLOAD_DIR / subdir
    folder.mkdir(parents=True, exist_ok=True)
    unique = f"{secrets.token_hex(8)}_{safe_name}"
    path = folder / unique
    file_storage.save(path)
    rel = str(path.relative_to(BASE_DIR)).replace('\\', '/')
    return rel, safe_name




def slugify(value):
    value = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().casefold()).strip('-')
    return value or f'item-{secrets.token_hex(4)}'


def unique_product_slug(name, product_id=None):
    base = slugify(name)
    slug = base
    n = 2
    while True:
        row = query_one('SELECT id FROM store_products WHERE slug=?', (slug,))
        if not row or (product_id is not None and int(row['id']) == int(product_id)):
            return slug
        slug = f'{base}-{n}'
        n += 1


def normalize_person_name(value):
    value = re.sub(r'[^a-z0-9 ]+', ' ', (value or '').casefold())
    return re.sub(r'\s+', ' ', value).strip()


def normalize_phone(value):
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('254') and len(digits) == 12:
        return '0' + digits[-9:]
    if len(digits) == 9 and digits[:1] in {'7', '1'}:
        return '0' + digits
    if len(digits) == 10 and digits[:2] in {'07', '01'}:
        return digits
    return digits


def normalize_external_url(value):
    raw = (value or '').strip()
    if not raw:
        return ''
    if not re.match(r'^https?://', raw, re.I):
        raise ValueError('Website link must start with http:// or https://')
    if any(ord(ch) < 32 for ch in raw) or any(ch.isspace() for ch in raw):
        raise ValueError('Website link contains spaces or invalid characters.')
    return raw


def parse_amount(value):
    if value is None:
        return None
    raw = str(value).strip().replace(',', '')
    if not raw:
        return None
    try:
        return Decimal(raw).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        match = re.search(r'(\d+(?:\.\d+)?)', raw)
        if not match:
            return None
        try:
            return Decimal(match.group(1)).quantize(Decimal('0.01'))
        except InvalidOperation:
            return None


def kes_float(value):
    amount = parse_amount(value) or Decimal('0.00')
    return float(amount)


def money_label(value):
    amount = parse_amount(value) or Decimal('0.00')
    return f'KES {amount:,.2f}'


def order_code():
    code = f'TOR-{datetime.now(timezone.utc).strftime("%Y%m%d")}-{secrets.token_hex(4).upper()}'
    while query_one('SELECT 1 FROM store_orders WHERE order_code=?', (code,)):
        code = f'TOR-{datetime.now(timezone.utc).strftime("%Y%m%d")}-{secrets.token_hex(4).upper()}'
    return code


def store_settings():
    return {
        'payment_instructions': get_setting('store_payment_instructions', ''),
        'gateway_secret': get_setting('store_gateway_secret', ''),
        'match_window_hours': int(get_setting('store_match_window_hours', '48') or '48'),
    }


class _WebsiteHTMLInfo(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ''
        self.meta_description = ''
        self._in_title = False
        self._title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'title':
            self._in_title = True
        if tag.lower() == 'meta':
            name = (attrs.get('name') or '').casefold()
            if name == 'description' and attrs.get('content'):
                self.meta_description = attrs['content'].strip()[:600]

    def handle_endtag(self, tag):
        if tag.lower() == 'title':
            self._in_title = False
            self.title = ' '.join(self._title_parts).strip()[:200]

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data.strip())


def _safe_zip_member(name):
    normalized = name.replace('\\', '/')
    path = Path(normalized)
    return not normalized.startswith('/') and '..' not in path.parts and not path.is_absolute()


def validate_website_zip(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos:
            raise ValueError('The website ZIP is empty.')
        if len(infos) > MAX_WEBSITE_FILES:
            raise ValueError('The website package contains too many files.')
        total = 0
        html_candidates = []
        for info in infos:
            name = info.filename.replace('\\', '/')
            if not _safe_zip_member(name):
                raise ValueError('The website ZIP contains an unsafe path.')
            # Reject symlink entries; websites are served as static files only.
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError('The website ZIP contains a symlink, which is not allowed.')
            total += info.file_size
            if total > MAX_WEBSITE_UNCOMPRESSED_BYTES:
                raise ValueError('The website ZIP expands beyond the permitted size.')
            if name.casefold().endswith(('.html', '.htm')):
                html_candidates.append(name)
        entry = next((x for x in html_candidates if Path(x).name.casefold() == 'index.html'), None)
        if not entry:
            entry = next((x for x in html_candidates if Path(x).name.casefold() == 'index.htm'), None)
        if not entry and html_candidates:
            entry = sorted(html_candidates)[0]
        if not entry:
            raise ValueError('The website package needs at least one HTML entry page.')
        return {'entry': entry, 'file_count': len(infos), 'uncompressed_size': total}


def extract_website_zip(source_path, destination):
    meta = validate_website_zip(source_path)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace('\\', '/')
            if name.endswith('/'):
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return meta


def generate_website_explanation(zip_path):
    info = validate_website_zip(zip_path)
    file_names = []
    readme_excerpt = ''
    title = ''
    meta_description = ''
    with zipfile.ZipFile(zip_path) as archive:
        names = [i.filename.replace('\\', '/') for i in archive.infolist() if not i.filename.endswith('/')]
        file_names = names[:1200]
        readme_name = next((n for n in names if Path(n).name.casefold() in {'readme.md', 'readme.txt'}), None)
        if readme_name:
            try:
                readme_excerpt = archive.read(readme_name).decode('utf-8', errors='ignore').strip()
            except Exception:
                readme_excerpt = ''
        try:
            raw_html = archive.read(info['entry']).decode('utf-8', errors='ignore')
            parser = _WebsiteHTMLInfo()
            parser.feed(raw_html)
            title = parser.title
            meta_description = parser.meta_description
        except Exception:
            pass
    lowered = '\n'.join(file_names).casefold()
    if 'package.json' in lowered:
        kind_text = 'JavaScript web application'
    elif 'manifest.webmanifest' in lowered or 'service-worker.js' in lowered or '/sw.js' in lowered:
        kind_text = 'web app / PWA package'
    else:
        kind_text = 'website package'
    types = []
    if any(n.casefold().endswith(('.html', '.htm')) for n in file_names): types.append('HTML pages')
    if any(n.casefold().endswith('.css') for n in file_names): types.append('CSS styles')
    if any(n.casefold().endswith('.js') for n in file_names): types.append('JavaScript')
    if any(n.casefold().endswith(('.png','.jpg','.jpeg','.webp','.svg','.gif')) for n in file_names): types.append('images')
    description = f'Uploaded website package detected as a {kind_text} with {len(file_names)} stored files and entry page {info["entry"]}.'
    if types:
        description += ' It includes ' + ', '.join(types) + '.'
    if title:
        description += f' The main page title is “{title}”.'
    if meta_description:
        description += f' Site description: {meta_description}'
    elif readme_excerpt:
        clean = re.sub(r'[#*_>`]+', ' ', readme_excerpt)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if clean:
            description += f' Package notes: {clean[:380]}'
    return description[:1500]


def stage_store_upload(file_storage, kind):
    if not file_storage or not file_storage.filename:
        raise ValueError('Choose the release file first.')
    original = secure_filename(file_storage.filename)
    if not original:
        raise ValueError('The uploaded filename is not valid.')
    ext = Path(original).suffix.casefold()
    allowed = ALLOWED_APK_EXTS if kind == 'apk' else ALLOWED_WEBSITE_EXTS
    if ext not in allowed:
        allowed_label = ', '.join(sorted(allowed))
        raise ValueError(f'Unsupported {kind} file. Allowed: {allowed_label}.')
    stage = STORE_STAGING_DIR / f'{secrets.token_hex(12)}_{original}'
    try:
        file_storage.save(stage)
        size = stage.stat().st_size
        if size <= 0:
            raise ValueError('The uploaded file is empty.')
        if size > MAX_STORE_ARTIFACT_BYTES:
            raise ValueError('The release file is too large. The maximum is 500 MB.')
        sha = hashlib.sha256()
        with stage.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                sha.update(chunk)
        if kind == 'apk':
            try:
                with zipfile.ZipFile(stage) as apk:
                    if apk.testzip() is not None:
                        raise ValueError('The APK archive failed integrity checking.')
                    if 'AndroidManifest.xml' not in apk.namelist():
                        raise ValueError('The uploaded file is not a valid Android APK (AndroidManifest.xml is missing).')
            except zipfile.BadZipFile as exc:
                raise ValueError('The uploaded APK is corrupt or is not a valid APK package.') from exc
        else:
            if ext == '.zip':
                validate_website_zip(stage)
            else:
                data = stage.read_bytes()
                if b'<html' not in data[:10000].casefold() and b'<!doctype html' not in data[:10000].casefold():
                    raise ValueError('The uploaded HTML file does not appear to be a valid HTML page.')
        return stage, original, size, sha.hexdigest()
    except Exception:
        stage.unlink(missing_ok=True)
        raise


def finalize_store_artifact(stage, kind, product_id, version_id):
    suffix = Path(stage.name).suffix.casefold()
    if kind == 'apk':
        final_dir = STORE_APK_DIR / str(product_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / f'v{version_id}_{stage.name.split("_", 1)[-1]}'
        os.replace(stage, final)
        return str(final.relative_to(BASE_DIR)).replace('\\', '/'), None, None
    final_dir = STORE_WEB_DIR / str(product_id) / f'v{version_id}'
    final_dir.mkdir(parents=True, exist_ok=True)
    if suffix == '.zip':
        # Keep the original package beside the extracted release for rollback/audit.
        zip_target = final_dir / stage.name.split('_', 1)[-1]
        try:
            os.replace(stage, zip_target)
            web_root = final_dir / 'site'
            meta = extract_website_zip(zip_target, web_root)
            return str(zip_target.relative_to(BASE_DIR)).replace('\\', '/'), str(web_root.relative_to(BASE_DIR)).replace('\\', '/'), meta['entry']
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            stage.unlink(missing_ok=True)
            raise
    html_target = final_dir / 'index.html'
    os.replace(stage, html_target)
    return str(html_target.relative_to(BASE_DIR)).replace('\\', '/'), str(final_dir.relative_to(BASE_DIR)).replace('\\', '/'), 'index.html'


def store_product_query(slug=None, include_inactive=False):
    base = '''SELECT p.*, v.id AS version_id, v.version_label, v.artifact_path, v.original_name AS version_original_name,
                     v.mime_type AS version_mime_type, v.file_size AS version_file_size, v.sha256 AS version_sha256,
                     v.release_notes, v.external_url, v.website_root, v.website_entry, v.created_at AS version_created_at
              FROM store_products p LEFT JOIN store_versions v ON v.id=p.current_version_id'''
    args = []
    where = []
    if slug:
        where.append('p.slug=?'); args.append(slug)
    if not include_inactive:
        where.append('p.active=1')
    if where:
        base += ' WHERE ' + ' AND '.join(where)
    base += ' ORDER BY p.id DESC'
    if slug:
        return query_one(base, tuple(args))
    return query_all(base, tuple(args))


def store_cart_products():
    raw = session.get('store_cart') or []
    ids = []
    for item in raw:
        try:
            pid = int(item)
        except (TypeError, ValueError):
            continue
        if pid not in ids:
            ids.append(pid)
    if ids != raw:
        session['store_cart'] = ids
        session.modified = True
    if not ids:
        return []
    placeholders = ','.join('?' for _ in ids)
    rows = query_all(f'''SELECT p.*, v.id AS version_id, v.version_label, v.file_size AS version_file_size
                         FROM store_products p LEFT JOIN store_versions v ON v.id=p.current_version_id
                         WHERE p.id IN ({placeholders}) AND p.active=1 ORDER BY p.id DESC''', tuple(ids))
    found = {int(r['id']): r for r in rows}
    cleaned = [pid for pid in ids if pid in found]
    if cleaned != ids:
        session['store_cart'] = cleaned
        session.modified = True
    return [found[pid] for pid in cleaned]


def store_product_is_paid(product):
    """A product is paid only when payment is explicitly required and the price is > 0."""
    try:
        return bool(int(product['payment_required'])) and Decimal(str(product['price_kes'] or 0)) > 0
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def store_cart_total(products):
    return sum((Decimal(str(r['price_kes'] or 0)) if store_product_is_paid(r) else Decimal('0.00')) for r in products).quantize(Decimal('0.01'))


def store_guest_identity():
    """Stable guest identifier for free-download analytics during this browser session."""
    guest_id = (session.get('store_guest_id') or '').strip()
    if not guest_id:
        guest_id = 'GUEST-' + secrets.token_hex(10).upper()
        session['store_guest_id'] = guest_id
        session.modified = True
    return guest_id


def create_free_store_order(product, buyer_name, buyer_phone, buyer_email=''):
    """Create an approved free order while requiring a real downloader identity."""
    version = current_product_version(product['id'])
    if not version:
        abort(404)
    buyer_name = re.sub(r'\s+', ' ', (buyer_name or '').strip())
    buyer_phone = normalize_phone(buyer_phone)
    buyer_email = (buyer_email or '').strip()
    if not buyer_name or len(buyer_name) < 2:
        raise ValueError("Enter the user's name before downloading the APK.")
    if not buyer_phone or len(buyer_phone) < 9:
        raise ValueError('Enter a valid phone number before downloading the APK.')
    code = order_code()
    db = get_db()
    try:
        db.execute('BEGIN')
        cur = db.execute("""INSERT INTO store_orders(order_code,buyer_name,buyer_phone,buyer_email,amount_expected,amount_entered,status,manual_note,approved_at,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (code, buyer_name, buyer_phone, buyer_email, 0.0, 0.0, 'approved',
                      'Free product — no payment required.', now_iso(), now_iso()))
        order_id = cur.lastrowid
        cur = db.execute("""INSERT INTO store_order_items(order_id,product_id,version_id,product_name_snapshot,unit_price,access_token,created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                     (order_id, product['id'], version['id'], product['name'], 0.0, secrets.token_urlsafe(28), now_iso()))
        item_id = cur.lastrowid
        db.commit()
        return query_one('SELECT * FROM store_order_items WHERE id=?', (item_id,)), query_one('SELECT * FROM store_orders WHERE id=?', (order_id,)), version
    except Exception:
        db.rollback()
        raise


def approve_store_order(order_id, payment_code='', payment_received_at=None, receipt_id=None, note='Auto-approved payment match.'):
    db = get_db()
    order = db.execute('SELECT * FROM store_orders WHERE id=?', (order_id,)).fetchone()
    if not order:
        raise ValueError('Order not found.')
    if order['status'] == 'approved':
        return False
    paid_at = payment_received_at or now_iso()
    code = (payment_code or '').strip()
    db.execute('''UPDATE store_orders SET status='approved', payment_code=?, payment_received_at=?, approved_at=?, manual_note=? WHERE id=?''',
               (code, paid_at, now_iso(), note, order_id))
    items = db.execute('SELECT * FROM store_order_items WHERE order_id=?', (order_id,)).fetchall()
    for item in items:
        product = db.execute('SELECT id, name FROM store_products WHERE id=?', (item['product_id'],)).fetchone()
        if not product or Decimal(str(item['unit_price'] or 0)) <= 0:
            continue
        db.execute('''INSERT OR IGNORE INTO accounting_entries(order_id,product_id,amount,entry_type,reference,memo,created_at)
                      VALUES (?,?,?,?,?,?,?)''',
                   (order_id, item['product_id'], float(item['unit_price']), 'sale', code or order['order_code'],
                    f'Store sale: {item["product_name_snapshot"]}', now_iso()))
    db.commit()
    if receipt_id:
        db.execute('UPDATE payment_receipts SET matched_order_id=?, classification=?, match_reason=? WHERE id=?',
                   (order_id, 'PAYMENT_MATCHED', note, receipt_id))
        db.commit()
    return True


def reject_store_order(order_id, note='Rejected by administrator.'):
    order = query_one('SELECT status FROM store_orders WHERE id=?', (order_id,))
    if not order or order['status'] == 'approved':
        return False
    execute("UPDATE store_orders SET status='rejected', manual_note=? WHERE id=?", (note, order_id))
    return True


def parse_gateway_payload(payload):
    payload = payload or {}
    raw_message = str(payload.get('raw_message') or payload.get('message') or payload.get('text') or payload.get('body') or '').strip()
    amount = parse_amount(payload.get('amount') or payload.get('paid') or payload.get('transaction_amount'))
    payer_name = (payload.get('payer_name') or payload.get('sender_name') or payload.get('customer_name') or payload.get('name') or '').strip()
    payer_phone = (payload.get('payer_phone') or payload.get('sender_phone') or payload.get('customer_phone') or payload.get('phone') or '').strip()
    tx_code = (payload.get('transaction_code') or payload.get('mpesa_code') or payload.get('code') or '').strip().upper()
    gateway_id = str(payload.get('gateway_id') or payload.get('message_id') or payload.get('id') or '').strip()
    sender = str(payload.get('sender') or payload.get('from') or '').strip()
    sim_device = str(payload.get('sim_device') or payload.get('device') or payload.get('sim') or '').strip()
    received_at = str(payload.get('received_at') or payload.get('timestamp') or now_iso()).strip()
    delivery = str(payload.get('delivery') or payload.get('status') or 'RECEIVED').strip()
    if raw_message:
        if not amount:
            m = re.search(r'\b(?:Ksh|KES)\s*([0-9][0-9,]*(?:\.\d+)?)', raw_message, flags=re.I)
            if m: amount = parse_amount(m.group(1))
        if not tx_code:
            m = re.search(r'\b([A-Z0-9]{9,12})\s+Confirmed\b', raw_message, flags=re.I)
            if m: tx_code = m.group(1).upper()
        if not payer_phone:
            m = re.search(r'(?:\+?254|0)(?:7|1)\d{8}\b', raw_message)
            if m: payer_phone = m.group(0)
        if not payer_name:
            m = re.search(r'\bfrom\s+([A-Za-z][A-Za-z .\'’-]{1,80}?)\s+(?:\+?254|0)(?:7|1)\d{8}\b', raw_message, flags=re.I)
            if m: payer_name = m.group(1).strip()
    return {
        'raw_message': raw_message,
        'amount': amount,
        'payer_name': payer_name,
        'payer_phone': payer_phone,
        'transaction_code': tx_code,
        'gateway_id': gateway_id,
        'sender': sender,
        'sim_device': sim_device,
        'received_at': received_at,
        'delivery': delivery,
    }


def parse_datetime(value):
    if not value:
        return None
    raw = str(value).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def classify_gateway_payment(parsed, receipt_id):
    amount = parsed['amount']
    payer_name = normalize_person_name(parsed['payer_name'])
    payer_phone = normalize_phone(parsed['payer_phone'])
    if amount is None or not payer_name or not payer_phone:
        reason = 'Receipt is missing an exact amount, payer name, or payer phone; manual review required.'
        execute('UPDATE payment_receipts SET classification=?, match_reason=? WHERE id=?', ('PAYMENT_UNMATCHED', reason, receipt_id))
        return None, reason
    duplicate = query_one('SELECT id, matched_order_id FROM payment_receipts WHERE id<>? AND ((transaction_code<>? AND transaction_code IS NOT NULL AND transaction_code<>\'\') OR (gateway_id<>? AND gateway_id IS NOT NULL AND gateway_id<>\'\')) AND (transaction_code=? OR gateway_id=?) LIMIT 1',
                          (receipt_id, parsed['transaction_code'], parsed['gateway_id'], parsed['transaction_code'], parsed['gateway_id'])) if (parsed['transaction_code'] or parsed['gateway_id']) else None
    if duplicate:
        reason = 'The gateway transaction appears to have been received before; duplicate receipt held for review.'
        execute('UPDATE payment_receipts SET classification=?, match_reason=? WHERE id=?', ('DUPLICATE', reason, receipt_id))
        return None, reason
    window_hours = store_settings()['match_window_hours']
    candidates = query_all("SELECT * FROM store_orders WHERE status='pending' ORDER BY id ASC")
    exact = []
    receipt_dt = parse_datetime(parsed['received_at']) or datetime.now(timezone.utc)
    for order in candidates:
        order_dt = parse_datetime(order['created_at']) or datetime.now(timezone.utc)
        if receipt_dt < order_dt - timedelta(minutes=5) or receipt_dt > order_dt + timedelta(hours=window_hours):
            continue
        expected = Decimal(str(order['amount_expected'] or 0)).quantize(Decimal('0.01'))
        if expected != amount:
            continue
        if normalize_person_name(order['buyer_name']) != payer_name:
            continue
        if normalize_phone(order['buyer_phone']) != payer_phone:
            continue
        exact.append(order)
    if len(exact) == 1:
        order = exact[0]
        code = parsed['transaction_code'] or parsed['gateway_id'] or f'RECEIPT-{receipt_id}'
        note = 'Auto-approved: exact pending order match on normalized name, normalized phone, amount, and time window.'
        approve_store_order(order['id'], code, parsed['received_at'], receipt_id, note)
        return order['id'], note
    if len(exact) > 1:
        reason = 'More than one pending order matched the same name, phone, amount and time window; manual review required.'
        execute('UPDATE payment_receipts SET classification=?, match_reason=? WHERE id=?', ('PAYMENT_AMBIGUOUS', reason, receipt_id))
        return None, reason
    reason = 'No pending order matched the payer name, phone, amount and time window.'
    execute('UPDATE payment_receipts SET classification=?, match_reason=? WHERE id=?', ('PAYMENT_UNMATCHED', reason, receipt_id))
    return None, reason


def current_product_version(product_id):
    return query_one('SELECT * FROM store_versions WHERE id=(SELECT current_version_id FROM store_products WHERE id=?)', (product_id,))


def render_store_access(item, product, version):
    return render_template('store_access.html', item=item, product=product, version=version, money_label=money_label)

def public_logo():
    logo = get_setting('logo_path', '/static/default-logo.svg')
    return logo if logo else '/static/default-logo.svg'


def send_magic_link(email, token):
    link = url_for('login_verify', token=token, _external=True)
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    smtp_from = os.environ.get('SMTP_FROM', smtp_user or get_setting('primary_email'))
    if smtp_host and smtp_user and smtp_pass:
        msg = EmailMessage()
        msg['Subject'] = f"Login to {get_setting('site_name')}"
        msg['From'] = smtp_from
        msg['To'] = email
        msg.set_content(f"Use this secure login link:\n\n{link}\n\nThis link expires soon.")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True, None
    return False, link


def user_email_allowed(email):
    row = query_one('SELECT 1 FROM contacts WHERE lower(email)=lower(?) LIMIT 1', (email,))
    return row is not None


def parse_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capture_client_context(form=None):
    form = form or request.form
    lat = parse_float_or_none(form.get('location_lat'))
    lng = parse_float_or_none(form.get('location_lng'))
    location_text = (form.get('location_text') or '').strip()
    if not location_text and lat is not None and lng is not None:
        location_text = f'{lat:.5f}, {lng:.5f}'
    device_info = (form.get('device_info') or request.headers.get('User-Agent') or '').strip()[:500]
    ip_address = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    return {
        'location_text': location_text[:255],
        'location_lat': lat,
        'location_lng': lng,
        'device_info': device_info,
        'ip_address': ip_address[:255],
    }


@app.context_processor
def inject_globals():
    return {
        'site_name': get_setting('site_name', 'Toror Technology Company Ltd'),
        'developer_name': get_setting('developer_name', get_admin_name() or 'Toror Technology Company Ltd'),
        'tagline': get_setting('tagline', ''),
        'hero_text': get_setting('hero_text', ''),
        'hero_kicker': get_setting('hero_kicker', ''),
        'about_text': get_setting('about_text', ''),
        'history_text': get_setting('history_text', ''),
        'mission_text': get_setting('mission_text', ''),
        'vision_text': get_setting('vision_text', ''),
        'service_text': get_setting('service_text', ''),
        'primary_email': get_setting('primary_email', ''),
        'primary_phone': get_setting('primary_phone', ''),
        'address_text': get_setting('address_text', ''),
        'footer_text': get_setting('footer_text', ''),
        'meta_description': get_setting('meta_description', ''),
        'logo_path': public_logo(),
        'theme_mode': get_setting('theme_mode', 'light'),
        'accent_color': '#7B2431',
        'issuer_default_name': get_setting('issuer_default_name', get_admin_name()),
        'issuer_default_title': get_setting('issuer_default_title', 'Chief Executive Officer'),
        'issuer_signature_path': get_setting('issuer_signature_path', ''),
        'certificate_default_title': get_setting('certificate_default_title', 'Technology Partnership Recognition'),
        'certificate_default_note': get_setting('certificate_default_note', 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.'),
        'certificate_base_url': get_setting('certificate_base_url', ''),
        'is_admin': is_admin,
        'show_nav': True,
        'user_email': session.get('user_email'),
        'user_id': session.get('user_id'),
        'display_name': user_display_name(),
        'admin_mode': is_admin(),
        'admin_name': get_admin_name(),
        'current_year': datetime.now().year,
        'current_date_label': datetime.now().strftime('%d %B %Y'),
        'store_cart_count': len(store_cart_products()),
        'store_payment_instructions': get_setting('store_payment_instructions', ''),
    }


@app.after_request
def add_cache_headers(resp):
    if request.path.startswith('/static/'):
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/')
def index():
    projects = query_all("SELECT * FROM projects WHERE status <> 'Draft' ORDER BY id DESC LIMIT 6")
    store_products = store_product_query(include_inactive=False)[:8]
    return render_template('home.html', projects=projects, store_products=store_products, store_cart_count=len(store_cart_products()))




@app.route('/store')
@app.route('/store/')
@app.route('/apps')
@app.route('/apps/')
@app.route('/app-store')
@app.route('/app-store/')
@app.route('/appstore')
@app.route('/appstore/')
def store():
    products = store_product_query(include_inactive=False)
    groups = {}
    for product in products:
        category = (product['category'] or 'Other').strip() or 'Other'
        groups.setdefault(category, []).append(product)
    category_order = sorted(groups, key=lambda x: (x.casefold() != 'music', x.casefold()))
    grouped_products = [(category, groups[category]) for category in category_order]
    return render_template(
        'store.html', products=products, grouped_products=grouped_products,
        categories=category_order, cart=store_cart_products(),
        cart_total=store_cart_total(store_cart_products())
    )


@app.route('/store/free/<int:product_id>', methods=['GET', 'POST'])
def store_get_free(product_id):
    product = query_one('SELECT * FROM store_products WHERE id=? AND active=1', (product_id,))
    if not product:
        abort(404)
    if store_product_is_paid(product):
        return redirect(url_for('store_product', slug=product['slug']))
    if product['kind'] != 'apk':
        return redirect(url_for('store_site_direct', slug=product['slug']))
    if request.method == 'GET':
        return render_template('store_free_identity.html', product=product)
    try:
        item, order, version = create_free_store_order(
            product,
            request.form.get('buyer_name', ''),
            request.form.get('buyer_phone', ''),
            request.form.get('buyer_email', ''),
        )
        return redirect(url_for('store_download', access_token=item['access_token']))
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('store_get_free', product_id=product_id))


@app.route('/store/free-download/<slug>')
def store_free_download(slug):
    product = store_product_query(slug=slug, include_inactive=False)
    if not product or product['kind'] != 'apk' or store_product_is_paid(product):
        abort(404)
    return redirect(url_for('store_get_free', product_id=product['id']))


@app.route('/store/free-site/<slug>/')
def store_free_site(slug):
    product = store_product_query(slug=slug, include_inactive=False)
    if not product or product['kind'] != 'website' or store_product_is_paid(product):
        abort(404)
    return redirect(url_for('store_site_direct', slug=slug))


@app.route('/store/direct-site/<slug>')
def store_site_direct(slug):
    product = store_product_query(slug=slug, include_inactive=False)
    if not product or product['kind'] != 'website':
        abort(404)
    version = current_product_version(product['id'])
    if version and version['external_url']:
        return redirect(version['external_url'], code=302)
    # Legacy support for older uploaded website releases.
    if version and version['website_root']:
        root = BASE_DIR / version['website_root']
        target = root / (version['website_entry'] or 'index.html')
        if target.exists() and target.is_file():
            response = send_from_directory(str(target.parent), target.name, as_attachment=False, mimetype='text/html')
            response.headers['Content-Security-Policy'] = 'sandbox allow-scripts'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Cache-Control'] = 'no-store'
            return response
    abort(404)


@app.route('/store/product/<slug>')
def store_product(slug):
    product = store_product_query(slug=slug, include_inactive=False)
    if not product:
        abort(404)
    return render_template('store_product.html', product=product, version=current_product_version(product['id']), cart_count=len(store_cart_products()))


@app.route('/store/cart')
def store_cart():
    products = store_cart_products()
    return render_template('store_checkout.html', products=products, cart=True, cart_total=store_cart_total(products), store_settings=store_settings(), order=None)


@app.route('/store/cart/add/<int:product_id>', methods=['POST'])
def store_cart_add(product_id):
    product = query_one('SELECT id, slug, active FROM store_products WHERE id=?', (product_id,))
    if not product or not int(product['active']):
        abort(404)
    cart = store_cart_products()
    ids = [int(x['id']) for x in cart]
    if product_id not in ids:
        ids.append(product_id)
    session['store_cart'] = ids
    session.modified = True
    flash('Added to cart.', 'success')
    return redirect(request.form.get('next') or url_for('store'))


@app.route('/store/cart/remove/<int:product_id>', methods=['POST'])
def store_cart_remove(product_id):
    ids = []
    for item in session.get('store_cart') or []:
        try: pid = int(item)
        except (TypeError, ValueError): continue
        if pid != product_id: ids.append(pid)
    session['store_cart'] = ids
    session.modified = True
    return redirect(url_for('store_cart'))


@app.route('/store/checkout', methods=['GET', 'POST'])
def store_checkout():
    products = store_cart_products()
    if not products:
        flash('Your cart is empty.', 'error')
        return redirect(url_for('store'))
    total = store_cart_total(products)
    # A cart containing only free items must never become a payment loop.
    if total == 0 and request.method == 'GET' and all(not store_product_is_paid(p) for p in products):
        if len(products) == 1:
            return redirect(url_for('store_get_free', product_id=products[0]['id']), code=307)
    if request.method == 'POST':
        buyer_name = request.form.get('buyer_name', '').strip()
        buyer_phone = request.form.get('buyer_phone', '').strip()
        buyer_email = request.form.get('buyer_email', '').strip()
        amount_entered = parse_amount(request.form.get('amount'))
        if not buyer_name or not buyer_phone:
            flash('Name and phone number are required.', 'error')
            return redirect(url_for('store_checkout'))
        if total > 0 and amount_entered != total:
            flash(f'Enter the exact amount: {money_label(total)}.', 'error')
            return redirect(url_for('store_checkout'))
        amount_value = float(amount_entered or Decimal('0.00'))
        code = order_code()
        db = get_db()
        try:
            db.execute('BEGIN')
            cur = db.execute('''INSERT INTO store_orders(order_code,buyer_name,buyer_phone,buyer_email,amount_expected,amount_entered,status,created_at)
                                VALUES (?,?,?,?,?,?,?,?)''', (code, buyer_name, buyer_phone, buyer_email, float(total), amount_value, 'pending' if total > 0 else 'approved', now_iso()))
            order_id = cur.lastrowid
            for product in products:
                db.execute('''INSERT INTO store_order_items(order_id,product_id,version_id,product_name_snapshot,unit_price,access_token,created_at)
                              VALUES (?,?,?,?,?,?,?)''', (order_id, product['id'], product['version_id'], product['name'], float(product['price_kes'] or 0), secrets.token_urlsafe(28), now_iso()))
            db.commit()
        except Exception:
            db.rollback()
            raise
        if total == 0:
            # Free products do not enter the payment stream.
            execute("UPDATE store_orders SET approved_at=?, manual_note=? WHERE id=?", (now_iso(), 'Free order — no payment required.', order_id))
            session['store_cart'] = []
            session.modified = True
            return redirect(url_for('store_order', order_code=code))
        session['store_cart'] = []
        session.modified = True
        return redirect(url_for('store_order', order_code=code))
    return render_template('store_checkout.html', products=products, cart=False, cart_total=total, store_settings=store_settings(), order=None)


@app.route('/store/order/<order_code>')
def store_order(order_code):
    order = query_one('SELECT * FROM store_orders WHERE order_code=?', (order_code.upper(),))
    if not order:
        abort(404)
    items = query_all('''SELECT oi.*, p.kind, p.slug, p.active, p.icon_path, p.access_instructions, p.current_version_id,
                                v.version_label, v.original_name, v.file_size
                         FROM store_order_items oi JOIN store_products p ON p.id=oi.product_id
                         LEFT JOIN store_versions v ON v.id=p.current_version_id
                         WHERE oi.order_id=? ORDER BY oi.id ASC''', (order['id'],))
    return render_template('store_order.html', order=order, items=items, store_settings=store_settings(), money_label=money_label)


@app.route('/api/store/order/<order_code>')
def store_order_api(order_code):
    order = query_one('SELECT id,order_code,status,approved_at,manual_note,payment_code,amount_expected FROM store_orders WHERE order_code=?', (order_code.upper(),))
    if not order:
        return jsonify({'ok': False, 'error': 'Order not found'}), 404
    items = query_all('''SELECT oi.id, oi.access_token, p.name, p.kind, p.slug, v.version_label
                         FROM store_order_items oi JOIN store_products p ON p.id=oi.product_id
                         LEFT JOIN store_versions v ON v.id=p.current_version_id WHERE oi.order_id=?''', (order['id'],))
    return jsonify({'ok': True, 'order': dict(order), 'items': [dict(x) for x in items]})


@app.route('/store/access/<access_token>')
def store_access(access_token):
    item = query_one('SELECT * FROM store_order_items WHERE access_token=?', (access_token,))
    if not item:
        abort(404)
    order = query_one('SELECT * FROM store_orders WHERE id=?', (item['order_id'],))
    if not order or order['status'] != 'approved':
        return redirect(url_for('store_order', order_code=order['order_code'] if order else ''))
    product = query_one('SELECT * FROM store_products WHERE id=?', (item['product_id'],))
    if not product:
        abort(404)
    version = current_product_version(product['id'])
    return render_store_access(item, product, version)


def record_apk_download(item, order, product, version):
    """Record a served APK download without ever blocking the actual file delivery."""
    try:
        previous = query_one('SELECT COUNT(*) c FROM store_download_events WHERE order_item_id=?', (item['id'],))
        prior_count = int(previous['c'] or 0)
        client_version = (request.args.get('current_version') or '').strip()[:100]
        if client_version and client_version != (version['version_label'] or ''):
            kind = 'update'
        elif prior_count == 0:
            kind = 'initial'
        else:
            kind = 'redownload'
        execute("""INSERT INTO store_download_events(
                    order_id,order_item_id,product_id,version_id,product_name_snapshot,
                    buyer_name,buyer_phone,buyer_email,version_label,download_kind,
                    current_version_sent_by_client,user_agent,downloaded_at
                  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            item['order_id'], item['id'], item['product_id'], version['id'], product['name'],
            order['buyer_name'], order['buyer_phone'], order['buyer_email'], version['version_label'],
            kind, client_version or None, (request.headers.get('User-Agent') or '')[:500], now_iso()
        ))
    except Exception:
        # Analytics must never make a valid buyer's APK unavailable.
        try:
            get_db().rollback()
        except Exception:
            pass


@app.route('/store/download/<access_token>')
def store_download(access_token):
    item = query_one('SELECT * FROM store_order_items WHERE access_token=?', (access_token,))
    if not item:
        abort(404)
    order = query_one('SELECT * FROM store_orders WHERE id=?', (item['order_id'],))
    if not order or order['status'] != 'approved':
        abort(403)
    product = query_one('SELECT * FROM store_products WHERE id=?', (item['product_id'],))
    version = current_product_version(item['product_id']) if product else None
    if not product or not version or product['kind'] != 'apk':
        abort(404)
    path = BASE_DIR / version['artifact_path']
    if not path.exists():
        abort(404)
    record_apk_download(item, order, product, version)
    response = send_from_directory(str(path.parent), path.name, as_attachment=True, download_name=f"{slugify(product['name'])}-{version['version_label']}.apk")
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/store/site/<access_token>/')
@app.route('/store/site/<access_token>/<path:asset_path>')
def store_site(access_token, asset_path=''):
    item = query_one('SELECT * FROM store_order_items WHERE access_token=?', (access_token,))
    if not item:
        abort(404)
    order = query_one('SELECT status FROM store_orders WHERE id=?', (item['order_id'],))
    if not order or order['status'] != 'approved':
        abort(403)
    product = query_one('SELECT * FROM store_products WHERE id=?', (item['product_id'],))
    version = current_product_version(item['product_id']) if product else None
    if not product or not version or product['kind'] != 'website':
        abort(404)
    if version['external_url']:
        return redirect(version['external_url'], code=302)
    if not version['website_root']:
        abort(404)
    root = BASE_DIR / version['website_root']
    if not root.exists():
        abort(404)
    rel = asset_path.strip('/') if asset_path else (version['website_entry'] or 'index.html')
    if '..' in Path(rel).parts or Path(rel).is_absolute():
        abort(404)
    target = root / rel
    if target.is_dir():
        target = target / 'index.html'
    if not target.exists() or not target.is_file():
        abort(404)
    response = send_from_directory(str(target.parent), target.name, as_attachment=False, mimetype=mimetypes.guess_type(target.name)[0])
    # Uploaded websites are sandboxed so their JavaScript cannot read the Toror application.
    if target.suffix.casefold() in {'.html', '.htm'}:
        response.headers['Content-Security-Policy'] = 'sandbox allow-scripts'
        response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


def _version_tuple(value):
    parts = release_version_key(value)
    return parts if parts is not None else tuple()

@app.route('/api/store/apk/<slug>/update-check')
@app.route('/api/store/apk/<slug>/latest')
def store_apk_update_check(slug):
    product = store_product_query(slug=slug, include_inactive=False)
    if not product or product['kind'] != 'apk' or not product['version_id']:
        abort(404)
    current_version = (request.args.get('current_version') or '').strip()
    token = (request.args.get('access_token') or '').strip()
    paid = store_product_is_paid(product)
    entitled = False
    if token:
        row = query_one("""SELECT oi.id FROM store_order_items oi JOIN store_orders o ON o.id=oi.order_id
                           WHERE oi.access_token=? AND oi.product_id=? AND o.status='approved' LIMIT 1""", (token, product['id']))
        entitled = row is not None
    latest = product['version_label'] or ''
    current_key = _version_tuple(current_version)
    latest_key = _version_tuple(latest)
    update_available = bool(current_version) and ((latest_key > current_key) if current_key and latest_key else current_version != latest)
    if paid and not entitled:
        response = jsonify({'ok': True, 'update_available': update_available, 'requires_purchase': True, 'version': latest})
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
    download_url = url_for('store_download', access_token=token) if token and entitled else (url_for('store_get_free', product_id=product['id']) if not paid else None)
    response = jsonify({
        'ok': True,
        'product': product['name'],
        'version': latest,
        'current_version': current_version,
        'update_available': update_available,
        'sha256': product['version_sha256'],
        'download_url': download_url,
        'latest_release_url': url_for('store_download', access_token=token) if token and entitled else None,
        'notes': product['release_notes'] or '',
    })
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/services')
def services():
    return render_template('services.html')


@app.route('/work')
def work():
    rows = query_all("SELECT * FROM projects WHERE status <> 'Draft' ORDER BY id DESC")
    file_map = {}
    for row in query_all('SELECT * FROM project_files ORDER BY id DESC'):
        file_map.setdefault(row['project_id'], []).append(row)
    return render_template('projects.html', projects=rows, project_files=file_map)


@app.route('/projects')
def projects():
    return redirect(url_for('work'))


@app.route('/portal')
def portal():
    return redirect(url_for('index'))


@app.route('/profile')
def profile():
    return redirect(url_for('index'))


@app.route('/chat')
def chat():
    return redirect(url_for('contact'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    return redirect(url_for('index'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('user_username', None)
    return redirect(url_for('index'))


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        company = (request.form.get('company') or '').strip()
        subject = (request.form.get('subject') or '').strip()
        message = (request.form.get('message') or '').strip()
        website = (request.form.get('website') or '').strip()  # honeypot
        if website:
            flash('Thank you. Your message was received.', 'success')
            return redirect(url_for('contact'))
        if not name or not (email or phone) or not message:
            flash('Please provide your name, a phone or email, and a message.', 'error')
            return redirect(url_for('contact'))
        execute(
            'INSERT INTO contacts(name,email,phone,company,subject,message,notes,status,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (name[:160], email[:160], phone[:80], company[:160], subject[:200], message[:2500], message[:2500], 'New', 'Website', now_iso()),
        )
        flash('Thank you. Your message has been sent to Toror Technology Company Ltd.', 'success')
        return redirect(url_for('contact'))
    return render_template('contact.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/api/chat/poll')
@user_required
def chat_poll():
    return jsonify([])


@app.route('/api/chat/send', methods=['POST'])
def chat_send():
    if is_admin():
        message = (request.form.get('message') or '').strip()
        edit_id = (request.form.get('edit_id') or '').strip()
        if not message:
            return jsonify({'ok': False, 'error': 'Message required.'}), 400
        sender = get_admin_name() or 'Toror Admin'
        if edit_id:
            existing = query_one('SELECT * FROM chat_messages WHERE id=? AND role=?', (edit_id, 'admin'))
            if existing:
                execute('UPDATE chat_messages SET message=?, edited=1, edited_at=? WHERE id=?', (message[:1200], now_iso(), edit_id))
                return jsonify({'ok': True, 'edited': True})
        execute('INSERT INTO chat_messages(sender,role,owner_user_id,owner_email,message,created_at) VALUES (?,?,?,?,?,?)', (sender,'admin',None,session.get('user_email'),message[:1200],now_iso()))
        return jsonify({'ok': True, 'edited': False})
    return jsonify({'ok': False, 'error': 'Visitor chat is not enabled on the public site.'}), 410


@app.route('/api/chat/delete/<int:message_id>', methods=['POST'])
def chat_delete(message_id):
    if not is_admin():
        return jsonify({'ok': False, 'error': 'Visitor chat is not enabled on the public site.'}), 410
    execute('DELETE FROM chat_messages WHERE id=?', (message_id,))
    return jsonify({'ok': True})


@app.route('/login/verify/<token>')
def login_verify(token):
    return redirect(url_for('index'))


@app.route('/promise212324', methods=['GET', 'POST'])
def admin_entry():
    open_mode = False
    if request.method == 'POST':
        admin_name = request.form.get('admin_name', '').strip()
        password = request.form.get('password', '')
        configured_name = get_admin_name()
        configured_password = get_admin_password()
        name_ok = bool(admin_name and configured_name and hmac.compare_digest(admin_name.casefold(), configured_name.casefold()))
        password_ok = False
        if configured_password and password:
            if configured_password.startswith(('scrypt:', 'pbkdf2:', 'argon2:')):
                try:
                    password_ok = check_password_hash(configured_password, password)
                except ValueError:
                    password_ok = False
            else:
                password_ok = hmac.compare_digest(password, configured_password)
        if name_ok and password_ok:
            session.clear()
            session['admin_logged_in'] = True
            session['user_email'] = _env_value('ADMIN_EMAIL')
            session['admin_name'] = configured_name
            flash('Welcome to the private administration workspace.', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Invalid administrator credentials. Check ADMIN_NAME and ADMIN_PASSWORD in Render.', 'error')
    return render_template('admin_login.html', open_mode=open_mode)


@app.route('/admin')
def admin_redirect():
    return redirect(url_for('admin_dashboard' if is_admin() else 'admin_entry'))


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Session closed.', 'success')
    return redirect(url_for('admin_entry'))


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    stats = {
        'users': query_one('SELECT COUNT(*) c FROM users')['c'],
        'contacts': query_one('SELECT COUNT(*) c FROM contacts')['c'],
        'projects': query_one('SELECT COUNT(*) c FROM projects')['c'],
        'vault': query_one('SELECT COUNT(*) c FROM vault_files')['c'],
        'messages': query_one('SELECT COUNT(*) c FROM chat_messages')['c'],
        'certificates': query_one('SELECT COUNT(*) c FROM certificates')['c'],
        'store_products': query_one('SELECT COUNT(*) c FROM store_products')['c'],
        'store_pending': query_one("SELECT COUNT(*) c FROM store_orders WHERE status='pending'")['c'],
        'store_income': query_one('SELECT COALESCE(SUM(amount),0) c FROM accounting_entries')['c'],
        'store_downloads': query_one('SELECT COUNT(*) c FROM store_download_events')['c'],
        'store_downloaders': query_one('SELECT COUNT(DISTINCT buyer_phone) c FROM store_download_events')['c'],
    }
    recent_users = query_all('SELECT * FROM users ORDER BY id DESC LIMIT 8')
    recent_contacts = query_all('SELECT * FROM contacts ORDER BY id DESC LIMIT 8')
    recent_orders = query_all('SELECT * FROM store_orders ORDER BY id DESC LIMIT 8')
    return render_template('admin_dashboard.html', stats=stats, recent_users=recent_users, recent_contacts=recent_contacts, recent_orders=recent_orders, admin_name=get_admin_name())




@app.route('/admin/store', methods=['GET', 'POST'])
@admin_required
def admin_store():
    if request.method == 'POST':
        action = request.form.get('action', '').strip()
        if action == 'settings':
            secret = request.form.get('store_gateway_secret', '').strip()
            instructions = request.form.get('store_payment_instructions', '').strip()
            window_raw = request.form.get('store_match_window_hours', '48').strip()
            try:
                window = max(1, min(168, int(window_raw)))
            except ValueError:
                window = 48
            if secret:
                set_setting('store_gateway_secret', secret)
            set_setting('store_payment_instructions', instructions)
            set_setting('store_match_window_hours', str(window))
            flash('Store payment and gateway settings saved.', 'success')
            return redirect(url_for('admin_store'))
        if action == 'create_product':
            name = request.form.get('name', '').strip()
            kind = request.form.get('kind', 'apk').strip().lower()
            category = request.form.get('category', 'Other').strip() or 'Other'
            description = request.form.get('description', '').strip()
            short_description = request.form.get('short_description', '').strip()
            instructions = request.form.get('access_instructions', '').strip()
            release_notes = request.form.get('release_notes', '').strip()
            version_label = request.form.get('version_label', '').strip() or '1.0.0'
            price = parse_amount(request.form.get('price')) or Decimal('0.00')
            payment_required = 1 if request.form.get('payment_required') == '1' and price > 0 else 0
            premium_enabled = 1 if request.form.get('premium_enabled') == '1' else 0
            active = 1 if request.form.get('active') == '1' else 0
            artifact = request.files.get('artifact')
            icon = request.files.get('icon')
            website_url = request.form.get('website_url', '').strip()
            if kind not in {'apk','website'}:
                flash('Choose APK or Website.', 'error'); return redirect(url_for('admin_store'))
            if not name:
                flash('Product name is required.', 'error'); return redirect(url_for('admin_store'))
            if price < 0:
                flash('Price cannot be negative.', 'error'); return redirect(url_for('admin_store'))
            if kind == 'apk' and (not artifact or not artifact.filename):
                flash('Choose the APK release file.', 'error'); return redirect(url_for('admin_store'))
            if kind == 'website' and not website_url:
                flash('Enter the website URL.', 'error'); return redirect(url_for('admin_store'))
            stage = None; icon_stage = None; final_artifact = None; final_root = None
            try:
                external_url = normalize_external_url(website_url) if kind == 'website' else ''
                if kind == 'apk':
                    stage, original, file_size, sha256 = stage_store_upload(artifact, 'apk')
                    mime_type = mimetypes.guess_type(original)[0] or 'application/vnd.android.package-archive'
                    artifact_path = 'PENDING'
                else:
                    original = external_url
                    file_size = 0
                    sha256 = hashlib.sha256(external_url.encode('utf-8')).hexdigest()
                    mime_type = 'text/html'
                    artifact_path = 'EXTERNAL_URL'
                    if not description:
                        description = f'Website: {external_url}'
                    if not short_description:
                        short_description = 'Open this website directly from Toror Apps & Sites.'
                icon_path = ''
                if icon and icon.filename:
                    icon_stage, _ = save_upload(icon, 'store/icons', {'.png','.jpg','.jpeg','.webp','.svg'})
                    icon_path = '/' + icon_stage if not icon_stage.startswith('/') else icon_stage
                db = get_db()
                db.execute('BEGIN')
                slug = unique_product_slug(name)
                cur = db.execute('''INSERT INTO store_products(name,slug,kind,short_description,category,description,price_kes,payment_required,premium_enabled,active,icon_path,access_instructions,created_at,updated_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (name,slug,kind,short_description,category,description,float(price),payment_required,premium_enabled,active,icon_path,instructions,now_iso(),now_iso()))
                product_id = cur.lastrowid
                cur = db.execute('''INSERT INTO store_versions(product_id,version_label,artifact_path,original_name,mime_type,file_size,sha256,release_notes,external_url,is_current,created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (product_id,version_label,artifact_path,original,mime_type,file_size,sha256,release_notes,external_url or None,1,now_iso()))
                version_id = cur.lastrowid
                if kind == 'apk':
                    final_artifact, final_root, _ = finalize_store_artifact(stage, kind, product_id, version_id)
                    stage = None
                    db.execute('UPDATE store_versions SET artifact_path=? WHERE id=?', (final_artifact,version_id))
                db.execute('UPDATE store_products SET current_version_id=?, updated_at=? WHERE id=?', (version_id,now_iso(),product_id))
                db.commit()
                icon_stage = None
                flash(f'{kind.title()} product “{name}” published.', 'success')
            except Exception as exc:
                try: get_db().rollback()
                except Exception: pass
                if stage: Path(stage).unlink(missing_ok=True)
                if final_artifact: (BASE_DIR / final_artifact).unlink(missing_ok=True)
                if final_root and (BASE_DIR / final_root).exists(): shutil.rmtree(BASE_DIR / final_root, ignore_errors=True)
                if icon_stage: Path(BASE_DIR / icon_stage).unlink(missing_ok=True)
                flash(f'Product was not published because the new release could not be committed safely: {exc}', 'error')
            return redirect(url_for('admin_store'))
    products = store_product_query(include_inactive=True)
    orders = query_all('''SELECT o.*, GROUP_CONCAT(oi.product_name_snapshot, ', ') AS item_names
                          FROM store_orders o LEFT JOIN store_order_items oi ON oi.order_id=o.id
                          GROUP BY o.id ORDER BY o.id DESC LIMIT 80''')
    receipts = query_all('SELECT * FROM payment_receipts ORDER BY id DESC LIMIT 40')
    download_stats = query_one('SELECT COUNT(*) total, COUNT(DISTINCT buyer_phone) unique_downloaders FROM store_download_events')
    download_products = query_all("""SELECT p.id, p.name, p.kind, p.slug, p.active,
                                           COUNT(d.id) AS download_count,
                                           COUNT(DISTINCT d.buyer_phone) AS unique_downloaders,
                                           MAX(d.downloaded_at) AS last_downloaded_at
                                    FROM store_products p LEFT JOIN store_download_events d ON d.product_id=p.id
                                    GROUP BY p.id ORDER BY download_count DESC, p.id DESC""")
    recent_downloads = query_all("""SELECT d.*, p.slug, p.kind, o.order_code
                                   FROM store_download_events d
                                   JOIN store_products p ON p.id=d.product_id
                                   JOIN store_orders o ON o.id=d.order_id
                                   ORDER BY d.id DESC LIMIT 60""")
    ledger = query_all('''SELECT a.*, o.order_code, p.name AS product_name FROM accounting_entries a
                          LEFT JOIN store_orders o ON o.id=a.order_id LEFT JOIN store_products p ON p.id=a.product_id
                          ORDER BY a.id DESC LIMIT 80''')
    total_income = sum((Decimal(str(r['amount'] or 0)) for r in ledger), Decimal('0.00'))
    return render_template('admin_store.html', products=products, orders=orders, receipts=receipts, ledger=ledger, total_income=total_income, gateway_url=url_for('payment_gateway_receiver', _external=True), store_settings=store_settings(), money_label=money_label, download_stats=download_stats, download_products=download_products, recent_downloads=recent_downloads)


@app.route('/admin/store/<int:product_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_store_edit(product_id):
    product = query_one('SELECT * FROM store_products WHERE id=?', (product_id,))
    if not product:
        abort(404)
    if request.method == 'POST':
        action = request.form.get('action', 'details')
        if action == 'archive':
            execute('UPDATE store_products SET active=0, updated_at=? WHERE id=?', (now_iso(), product_id))
            flash('Product archived. Its releases, orders and accounting history remain intact.', 'success')
            return redirect(url_for('admin_store'))
        name = request.form.get('name', '').strip()
        category = request.form.get('category', 'Other').strip() or 'Other'
        description = request.form.get('description', '').strip()
        short_description = request.form.get('short_description', '').strip()
        instructions = request.form.get('access_instructions', '').strip()
        price = parse_amount(request.form.get('price')) or Decimal('0.00')
        payment_required = 1 if request.form.get('payment_required') == '1' and price > 0 else 0
        premium_enabled = 1 if request.form.get('premium_enabled') == '1' else 0
        active = 1 if request.form.get('active') == '1' else 0
        if not name or price < 0 or (payment_required and price <= 0):
            flash('Name is required; price cannot be negative; paid products must cost more than zero.', 'error')
            return redirect(url_for('admin_store_edit', product_id=product_id))
        slug = unique_product_slug(name, product_id)
        execute('''UPDATE store_products SET name=?,slug=?,short_description=?,category=?,description=?,price_kes=?,payment_required=?,premium_enabled=?,active=?,access_instructions=?,updated_at=? WHERE id=?''',
                (name,slug,short_description,category,description,float(price),payment_required,premium_enabled,active,instructions,now_iso(),product_id))
        icon = request.files.get('icon')
        if icon and icon.filename:
            try:
                rel, _ = save_upload(icon, 'store/icons', {'.png','.jpg','.jpeg','.webp','.svg'})
                execute('UPDATE store_products SET icon_path=? WHERE id=?', ('/'+rel if not rel.startswith('/') else rel, product_id))
            except ValueError as exc:
                flash(str(exc), 'error')
                return redirect(url_for('admin_store_edit', product_id=product_id))
        flash('Product details updated.', 'success')
        return redirect(url_for('admin_store'))
    versions = query_all('SELECT * FROM store_versions WHERE product_id=? ORDER BY id DESC', (product_id,))
    current_version = current_product_version(product_id)
    return render_template('admin_store_edit.html', product=product, versions=versions, current_version=current_version, money_label=money_label)


def release_version_key(value):
    parts = re.findall(r'\d+', str(value or ''))
    if not parts:
        return None
    return tuple(int(x) for x in parts)


@app.route('/admin/store/<int:product_id>/version', methods=['POST'])
@admin_required
def admin_store_version(product_id):
    product = query_one('SELECT * FROM store_products WHERE id=?', (product_id,))
    if not product:
        abort(404)
    version_label = request.form.get('version_label', '').strip()
    release_notes = request.form.get('release_notes', '').strip()
    if not version_label:
        flash('Enter the new version label before publishing.', 'error')
        return redirect(url_for('admin_store_edit', product_id=product_id))
    current_version = current_product_version(product_id)
    if current_version:
        current_label = current_version['version_label'] or ''
        if version_label == current_label:
            flash('That version is already live. Use a different version label.', 'error')
            return redirect(url_for('admin_store_edit', product_id=product_id))
        old_key = release_version_key(current_label)
        new_key = release_version_key(version_label)
        if old_key and new_key and new_key <= old_key:
            flash(f'New version must be higher than the current version {current_label}.', 'error')
            return redirect(url_for('admin_store_edit', product_id=product_id))
    stage = None; final_artifact = None; final_root = None
    try:
        external_url = None
        if product['kind'] == 'apk':
            artifact = request.files.get('artifact')
            stage, original, file_size, sha256 = stage_store_upload(artifact, 'apk')
            mime_type = mimetypes.guess_type(original)[0] or 'application/vnd.android.package-archive'
            artifact_path = 'PENDING'
        else:
            external_url = normalize_external_url(request.form.get('website_url', '').strip())
            original = external_url
            file_size = 0
            sha256 = hashlib.sha256(external_url.encode('utf-8')).hexdigest()
            mime_type = 'text/html'
            artifact_path = 'EXTERNAL_URL'
        db = get_db()
        db.execute('BEGIN')
        cur = db.execute('''INSERT INTO store_versions(product_id,version_label,artifact_path,original_name,mime_type,file_size,sha256,release_notes,external_url,is_current,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (product_id,version_label,artifact_path,original,mime_type,file_size,sha256,release_notes,external_url,1,now_iso()))
        version_id = cur.lastrowid
        if product['kind'] == 'apk':
            final_artifact, final_root, _ = finalize_store_artifact(stage, 'apk', product_id, version_id)
            stage = None
            db.execute('UPDATE store_versions SET artifact_path=? WHERE id=?', (final_artifact,version_id))
        db.execute('UPDATE store_versions SET is_current=0 WHERE product_id=? AND id<>?', (product_id,version_id))
        db.execute('UPDATE store_products SET current_version_id=?, updated_at=? WHERE id=?', (version_id,now_iso(),product_id))
        db.commit()
        flash(f'Version {version_label} is live. Existing approved access links now serve the new release automatically.', 'success')
    except Exception as exc:
        try: get_db().rollback()
        except Exception: pass
        if stage: Path(stage).unlink(missing_ok=True)
        if final_artifact: (BASE_DIR / final_artifact).unlink(missing_ok=True)
        if final_root and (BASE_DIR / final_root).exists(): shutil.rmtree(BASE_DIR / final_root, ignore_errors=True)
        flash(f'The previous release was kept. New release was not published: {exc}', 'error')
    return redirect(url_for('admin_store_edit', product_id=product_id))


@app.route('/admin/store/orders/<int:order_id>/approve', methods=['POST'])
@admin_required
def admin_store_order_approve(order_id):
    code = request.form.get('payment_code', '').strip().upper()
    note = request.form.get('note', '').strip() or 'Manually approved by administrator.'
    try:
        changed = approve_store_order(order_id, code, now_iso(), None, note)
        flash('Order approved and accounting entry recorded.' if changed else 'Order was already approved.', 'success')
    except Exception as exc:
        flash(f'Approval failed: {exc}', 'error')
    return redirect(url_for('admin_store') + '#orders')


@app.route('/admin/store/orders/<int:order_id>/reject', methods=['POST'])
@admin_required
def admin_store_order_reject(order_id):
    note = request.form.get('note', '').strip() or 'Rejected by administrator.'
    changed = reject_store_order(order_id, note)
    flash('Order rejected. The original order and gateway receipts remain recorded.' if changed else 'An approved order cannot be rejected from this screen.', 'success' if changed else 'error')
    return redirect(url_for('admin_store') + '#orders')


@app.route('/api/admin/store/feed')
@admin_required
def admin_store_feed():
    last_id = request.args.get('last_id', '0')
    try: last_id = int(last_id)
    except ValueError: last_id = 0
    rows = query_all('SELECT * FROM payment_receipts WHERE id>? ORDER BY id ASC LIMIT 60', (last_id,))
    return jsonify([dict(r) for r in rows])


@app.route('/admin/users')
@admin_required
def admin_users():
    users = query_all('SELECT * FROM users ORDER BY id DESC')
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@admin_required
def toggle_user(user_id):
    execute('UPDATE users SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?', (user_id,))
    flash('User status updated.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    execute('DELETE FROM users WHERE id=?', (user_id,))
    flash('User removed.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_user(user_id):
    user = query_one('SELECT * FROM users WHERE id=?', (user_id,))
    if not user:
        abort(404)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        if not name or not username or not email:
            flash('Name, username, and email are required.', 'error')
            return redirect(url_for('edit_user', user_id=user_id))
        existing = query_one('SELECT 1 FROM users WHERE id<>? AND (lower(name)=lower(?) OR lower(username)=lower(?) OR lower(email)=lower(?)) LIMIT 1', (user_id, name, username, email))
        if existing:
            flash('That name, username, or email is already in use.', 'error')
            return redirect(url_for('edit_user', user_id=user_id))
        params = [name, username, email]
        sql = 'UPDATE users SET name=?, username=?, email=?'
        if password:
            sql += ', password_hash=?'
            params.append(generate_password_hash(password))
        sql += ' WHERE id=?'
        params.append(user_id)
        execute(sql, tuple(params))
        flash('User updated.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin_user_edit.html', user=user)


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        fields = {
            'site_name': 'Toror Technology Company Ltd',
            'developer_name': '',
            'issuer_default_name': '',
            'issuer_default_title': 'Chief Executive Officer',
            'certificate_base_url': '',
            'certificate_default_title': 'Technology Partnership Recognition',
            'certificate_default_note': 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.',
            'tagline': '',
            'primary_email': '',
            'primary_phone': '',
            'address_text': '',
            'hero_kicker': '',
            'hero_text': '',
            'about_text': '',
            'history_text': '',
            'mission_text': '',
            'vision_text': '',
            'service_text': '',
            'footer_text': '',
            'meta_description': '',
            'theme_mode': 'light',
            'accent_color': '#7B2431',
        }
        for key, fallback in fields.items():
            value = request.form.get(key, '').strip()
            if key == 'site_name' and not value:
                value = fallback
            set_setting(key, value)
        flash('Public site settings updated.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin_settings.html')


@app.route('/admin/logo', methods=['POST'])
@admin_required
def admin_logo_upload():
    file = request.files.get('logo')
    if not file or not file.filename:
        flash('Choose a logo file first.', 'error')
        return redirect(url_for('admin_settings'))
    try:
        rel, _ = save_upload(file, 'logo', ALLOWED_IMAGE_EXTS)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_settings'))
    set_setting('logo_path', '/' + rel if not rel.startswith('/') else rel)
    flash('Logo updated.', 'success')
    return redirect(url_for('admin_settings'))


def _database_snapshot_bytes():
    if not DB_PATH.exists():
        init_db()
    source = sqlite3.connect(str(DB_PATH))
    try:
        mem = sqlite3.connect(':memory:')
        try:
            source.backup(mem)
            out = BytesIO()
            # SQLite backup written to a temporary real file so the snapshot is a valid .db binary.
            temp_path = DATA_DIR / f'.backup_{secrets.token_hex(8)}.db'
            try:
                target = sqlite3.connect(str(temp_path))
                try:
                    mem.backup(target)
                    target.commit()
                finally:
                    target.close()
                out.write(temp_path.read_bytes())
                out.seek(0)
                return out
            finally:
                temp_path.unlink(missing_ok=True)
        finally:
            mem.close()
    finally:
        source.close()


def _validate_sqlite_bytes(data):
    temp_path = DATA_DIR / f'.restore_check_{secrets.token_hex(8)}.db'
    try:
        temp_path.write_bytes(data)
        db = sqlite3.connect(str(temp_path))
        try:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise ValueError('The uploaded database failed SQLite integrity checking.')
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'settings','contacts','projects','certificates','users'}
            missing = required - names
            if missing:
                raise ValueError('The uploaded database is not a Toror database or is missing required tables.')
        finally:
            db.close()
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


@app.route('/admin/backup/database')
@admin_required
def admin_backup_database():
    snap = _database_snapshot_bytes()
    response = app.response_class(snap.getvalue(), mimetype='application/vnd.sqlite3')
    response.headers['Content-Disposition'] = f'attachment; filename="toror-database-{datetime.now().strftime("%Y%m%d-%H%M%S")}.sqlite3"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/admin/backup/full')
@admin_required
def admin_backup_full():
    db_bytes = _database_snapshot_bytes().getvalue()
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('toror.db', db_bytes)
        upload_root = UPLOAD_DIR
        if upload_root.exists():
            for path in upload_root.rglob('*'):
                if path.is_file():
                    archive.write(path, str(path.relative_to(BASE_DIR)).replace('\\', '/'))
        archive.writestr('backup_manifest.json', json.dumps({
            'product': 'Toror Technology Company Ltd',
            'created_at': now_iso(),
            'contains': ['SQLite database', 'static uploaded assets, including APK releases and website packages', 'store orders, gateway receipts and accounting ledger'],
            'restore_note': 'Restore only through the private admin backup/restore screen.'
        }, indent=2))
    out.seek(0)
    response = app.response_class(out.getvalue(), mimetype='application/zip')
    response.headers['Content-Disposition'] = f'attachment; filename="toror-full-backup-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/admin/restore', methods=['POST'])
@admin_required
def admin_restore():
    uploaded = request.files.get('backup')
    if not uploaded or not uploaded.filename:
        flash('Choose a SQLite database or Toror full-backup ZIP first.', 'error')
        return redirect(url_for('admin_settings'))
    filename = secure_filename(uploaded.filename)
    data = uploaded.read()
    if not data:
        flash('The uploaded backup is empty.', 'error')
        return redirect(url_for('admin_settings'))

    old_backup = DATA_DIR / f"pre_restore_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(4)}.sqlite3"
    restore_path = None
    extracted_uploads = None
    try:
        if filename.lower().endswith('.zip'):
            temp_zip = DATA_DIR / f'.restore_{secrets.token_hex(8)}.zip'
            temp_zip.write_bytes(data)
            try:
                with zipfile.ZipFile(temp_zip) as archive:
                    names = archive.namelist()
                    db_candidates = [n for n in names if Path(n).name.lower() == 'toror.db' and not n.startswith('/')]
                    if not db_candidates:
                        raise ValueError('The backup ZIP does not contain toror.db.')
                    total_uncompressed = sum(i.file_size for i in archive.infolist())
                    if total_uncompressed > 500 * 1024 * 1024:
                        raise ValueError('The backup ZIP is larger than the permitted restore size.')
                    db_data = archive.read(db_candidates[0])
                    restore_path = _validate_sqlite_bytes(db_data)
                    extracted_uploads = DATA_DIR / f'.restore_uploads_{secrets.token_hex(8)}'
                    extracted_uploads.mkdir(parents=True, exist_ok=True)
                    for info in archive.infolist():
                        name = info.filename.replace('\\', '/')
                        if not name.startswith('static/uploads/') or name.endswith('/'):
                            continue
                        rel = Path(name[len('static/uploads/'):])
                        if '..' in rel.parts or rel.is_absolute():
                            raise ValueError('The backup contains an unsafe upload path.')
                        target = extracted_uploads / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as src, target.open('wb') as dst:
                            dst.write(src.read())
            finally:
                temp_zip.unlink(missing_ok=True)
        else:
            restore_path = _validate_sqlite_bytes(data)

        # Preserve the current database before replacing it.
        if DB_PATH.exists():
            old_backup.write_bytes(_database_snapshot_bytes().getvalue())
        old_conn = g.pop('db', None)
        if old_conn is not None:
            old_conn.close()
        os.replace(restore_path, DB_PATH)
        restore_path = None
        # Re-run schema creation/migrations against the restored database so an older backup
        # can be restored safely even after the application has gained new fields.
        init_db()

        if extracted_uploads is not None:
            if UPLOAD_DIR.exists():
                for p in sorted(UPLOAD_DIR.rglob('*'), key=lambda x: len(x.parts), reverse=True):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        try: p.rmdir()
                        except OSError: pass
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            for path in extracted_uploads.rglob('*'):
                if path.is_file():
                    dest = UPLOAD_DIR / path.relative_to(extracted_uploads)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(path.read_bytes())
            for p in sorted(extracted_uploads.rglob('*'), key=lambda x: len(x.parts), reverse=True):
                if p.is_file(): p.unlink(missing_ok=True)
                elif p.is_dir():
                    try: p.rmdir()
                    except OSError: pass
            extracted_uploads.rmdir()
        flash(f'Restore completed. A pre-restore database copy was retained as {old_backup.name}.', 'success')
    except Exception as exc:
        if restore_path:
            Path(restore_path).unlink(missing_ok=True)
        if extracted_uploads and extracted_uploads.exists():
            for p in sorted(extracted_uploads.rglob('*'), key=lambda x: len(x.parts), reverse=True):
                if p.is_file(): p.unlink(missing_ok=True)
                elif p.is_dir():
                    try: p.rmdir()
                    except OSError: pass
            try: extracted_uploads.rmdir()
            except OSError: pass
        flash(f'Restore failed: {exc}', 'error')
    return redirect(url_for('admin_settings'))


@app.route('/admin/issuer-signature', methods=['POST'])
@admin_required
def admin_issuer_signature_upload():
    file = request.files.get('signature')
    if not file or not file.filename:
        flash('Choose a signature image first.', 'error')
        return redirect(url_for('admin_settings'))
    try:
        rel, _ = save_upload(file, 'issuer-signatures', {'.png', '.jpg', '.jpeg', '.webp'})
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_settings'))
    set_setting('issuer_signature_path', '/' + rel if not rel.startswith('/') else rel)
    flash('Issuer signature updated. Existing certificates keep their recorded issuer information.', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/contacts', methods=['GET', 'POST'])
@admin_required
def admin_contacts():
    if request.method == 'POST':
        if 'csv' in request.files and request.files['csv'].filename:
            csv_file = request.files['csv']
            rows = csv_file.read().decode('utf-8', errors='ignore').splitlines()
            imported = 0
            for line in rows:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 2:
                    continue
                execute(
                    'INSERT INTO contacts(name,email,phone,company,subject,message,notes,status,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (parts[0], parts[1], parts[2] if len(parts) > 2 else '', parts[3] if len(parts) > 3 else '', '', parts[4] if len(parts) > 4 else '', parts[4] if len(parts) > 4 else '', 'Imported', 'CSV', now_iso()),
                )
                imported += 1
            flash(f'Imported {imported} contacts.', 'success')
            return redirect(url_for('admin_contacts'))
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        company = request.form.get('company', '').strip()
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        notes = request.form.get('notes', '').strip()
        if name:
            execute(
                'INSERT INTO contacts(name,email,phone,company,subject,message,notes,status,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (name, email, phone, company, subject, message, notes, 'New', 'Admin', now_iso()),
            )
            flash('Contact saved.', 'success')
        return redirect(url_for('admin_contacts'))
    contacts = query_all('SELECT * FROM contacts ORDER BY id DESC')
    return render_template('admin_contacts.html', contacts=contacts)


@app.route('/admin/contacts/<int:contact_id>/delete', methods=['POST'])
@admin_required
def delete_contact(contact_id):
    execute('DELETE FROM contacts WHERE id=?', (contact_id,))
    flash('Contact deleted.', 'success')
    return redirect(url_for('admin_contacts'))


@app.route('/admin/contacts/<int:contact_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_contact(contact_id):
    contact = query_one('SELECT * FROM contacts WHERE id=?', (contact_id,))
    if not contact:
        abort(404)
    if request.method == 'POST':
        execute('UPDATE contacts SET name=?, email=?, phone=?, company=?, subject=?, message=?, notes=?, status=? WHERE id=?', (
            request.form.get('name', '').strip(),
            request.form.get('email', '').strip(),
            request.form.get('phone', '').strip(),
            request.form.get('company', '').strip(),
            request.form.get('subject', '').strip(),
            request.form.get('message', '').strip(),
            request.form.get('notes', '').strip(),
            request.form.get('status', 'New').strip() or 'New',
            contact_id,
        ))
        flash('Contact updated.', 'success')
        return redirect(url_for('admin_contacts'))
    return render_template('admin_contact_edit.html', contact=contact)


@app.route('/admin/projects', methods=['GET', 'POST'])
@admin_required
def admin_projects():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        summary = request.form.get('summary', '').strip()
        link = request.form.get('link', '').strip()
        status = request.form.get('status', 'Active').strip()
        files = request.files.getlist('attachments')
        if title and summary:
            cur = execute(
                'INSERT INTO projects(title,summary,link,status,created_at) VALUES (?,?,?,?,?)',
                (title, summary, link, status, now_iso()),
            )
            project_id = cur.lastrowid
            for file in files:
                if file and file.filename:
                    try:
                        rel, original = save_upload(file, 'projects', ALLOWED_PROJECT_EXTS)
                    except ValueError:
                        continue
                    mime = mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'
                    execute(
                        'INSERT INTO project_files(project_id,filename,original_name,mime_type,created_at) VALUES (?,?,?,?,?)',
                        (project_id, rel, original or file.filename, mime, now_iso()),
                    )
            flash('Project published.', 'success')
        return redirect(url_for('admin_projects'))
    projects = query_all('SELECT * FROM projects ORDER BY id DESC')
    files = query_all('SELECT * FROM project_files ORDER BY id DESC')
    file_map = {}
    for row in files:
        file_map.setdefault(row['project_id'], []).append(row)
    return render_template('admin_projects.html', projects=projects, project_files=file_map)


@app.route('/admin/projects/<int:project_id>/delete', methods=['POST'])
@admin_required
def delete_project(project_id):
    execute('DELETE FROM projects WHERE id=?', (project_id,))
    flash('Project removed.', 'success')
    return redirect(url_for('admin_projects'))


@app.route('/admin/projects/<int:project_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_project(project_id):
    project = query_one('SELECT * FROM projects WHERE id=?', (project_id,))
    if not project:
        abort(404)
    if request.method == 'POST':
        execute('UPDATE projects SET title=?, summary=?, link=?, status=? WHERE id=?', (
            request.form.get('title', '').strip(),
            request.form.get('summary', '').strip(),
            request.form.get('link', '').strip(),
            request.form.get('status', 'Active').strip(),
            project_id,
        ))
        extra_files = request.files.getlist('attachments')
        for file in extra_files:
            if file and file.filename:
                try:
                    rel, original = save_upload(file, 'projects', ALLOWED_PROJECT_EXTS)
                except ValueError:
                    continue
                mime = mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'
                execute(
                    'INSERT INTO project_files(project_id,filename,original_name,mime_type,created_at) VALUES (?,?,?,?,?)',
                    (project_id, rel, original or file.filename, mime, now_iso()),
                )
        flash('Project updated.', 'success')
        return redirect(url_for('admin_projects'))
    return render_template('admin_project_edit.html', project=project)


@app.route('/files/<path:relpath>')
def media_file(relpath):
    if '..' in Path(relpath).parts:
        abort(404)
    return send_from_directory(BASE_DIR, relpath, as_attachment=False)


@app.route('/admin/vault', methods=['GET', 'POST'])
@admin_required
def admin_vault():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        file = request.files.get('video')
        if not title or not file or not file.filename:
            flash('Add a title and choose a video file.', 'error')
            return redirect(url_for('admin_vault'))
        if not allowed_file(file.filename, ALLOWED_VIDEO_EXTS):
            flash('Only mp4, webm, mov, or m4v videos are allowed.', 'error')
            return redirect(url_for('admin_vault'))
        rel, _ = save_upload(file, 'vault', ALLOWED_VIDEO_EXTS)
        token = secrets.token_urlsafe(18)
        mime_type = mimetypes.guess_type(file.filename)[0] or 'video/mp4'
        execute(
            'INSERT INTO vault_files(title,filename,token,mime_type,description,active,created_at) VALUES (?,?,?,?,?,?,?)',
            (title, rel, token, mime_type, description, 1, now_iso()),
        )
        flash('Video added to the secret vault.', 'success')
        return redirect(url_for('admin_vault'))
    vault = query_all('SELECT * FROM vault_files ORDER BY id DESC')
    return render_template('admin_vault.html', vault=vault)


@app.route('/admin/vault/<int:file_id>/edit', methods=['GET', 'POST'])
@admin_required
def vault_edit(file_id):
    item = query_one('SELECT * FROM vault_files WHERE id=?', (file_id,))
    if not item:
        abort(404)
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        active = 1 if request.form.get('active') == '1' else 0
        file = request.files.get('video')
        filename = item['filename']
        mime_type = item['mime_type']
        if file and file.filename:
            if not allowed_file(file.filename, ALLOWED_VIDEO_EXTS):
                flash('Only mp4, webm, mov, or m4v videos are allowed.', 'error')
                return redirect(url_for('vault_edit', file_id=file_id))
            rel, _ = save_upload(file, 'vault', ALLOWED_VIDEO_EXTS)
            filename = rel
            mime_type = mimetypes.guess_type(file.filename)[0] or 'video/mp4'
        execute('UPDATE vault_files SET title=?, filename=?, mime_type=?, description=?, active=? WHERE id=?', (
            title, filename, mime_type, description, active, file_id,
        ))
        flash('Vault item updated.', 'success')
        return redirect(url_for('admin_vault'))
    return render_template('admin_vault_edit.html', item=item)


@app.route('/admin/vault/<int:file_id>/toggle', methods=['POST'])
@admin_required
def vault_toggle(file_id):
    execute('UPDATE vault_files SET active = CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?', (file_id,))
    flash('Vault visibility updated.', 'success')
    return redirect(url_for('admin_vault'))


@app.route('/admin/vault/<int:file_id>/delete', methods=['POST'])
@admin_required
def vault_delete(file_id):
    row = query_one('SELECT filename FROM vault_files WHERE id=?', (file_id,))
    if row:
        try:
            (BASE_DIR / row['filename']).unlink(missing_ok=True)
        except Exception:
            pass
    execute('DELETE FROM vault_files WHERE id=?', (file_id,))
    flash('Vault file removed.', 'success')
    return redirect(url_for('admin_vault'))


@app.route('/v/<token>')
def vault_access(token):
    row = query_one('SELECT * FROM vault_files WHERE token=? AND active=1', (token,))
    if not row:
        abort(404)
    return render_template('vault_view.html', item=row, video_url=url_for('media_file', relpath=row['filename']))


@app.route('/vault/<token>')
def vault_access_alias(token):
    return vault_access(token)




@app.route('/api/gateway/mpesa', methods=['POST', 'GET'])
@app.route('/api/gateway/messages', methods=['POST', 'GET'])
def payment_gateway_receiver():
    # GET is a safe health check; POST is the listener contract.
    configured = get_setting('store_gateway_secret', '')
    if request.method == 'GET':
        return jsonify({'ok': True, 'service': 'toror-payment-gateway', 'ready': bool(configured)})
    supplied = request.headers.get('X-Toror-Gateway-Key', '').strip()
    auth = request.headers.get('Authorization', '').strip()
    if not supplied and auth.casefold().startswith('bearer '):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = request.args.get('token', '').strip()
    if not configured or not supplied or not hmac.compare_digest(supplied, configured):
        return jsonify({'ok': False, 'error': 'Unauthorized gateway request.'}), 401
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict(flat=True)
    parsed = parse_gateway_payload(payload)
    if not parsed['raw_message']:
        return jsonify({'ok': False, 'error': 'A raw receipt message is required.'}), 400
    # Idempotency: identical gateway id or transaction code is stored only once as a live receipt.
    if parsed['transaction_code']:
        existing = query_one('SELECT id,classification,matched_order_id FROM payment_receipts WHERE transaction_code=? LIMIT 1', (parsed['transaction_code'],))
        if existing:
            return jsonify({'ok': True, 'duplicate': True, 'receipt_id': existing['id'], 'classification': existing['classification'], 'order_id': existing['matched_order_id']})
    if parsed['gateway_id']:
        existing = query_one('SELECT id,classification,matched_order_id FROM payment_receipts WHERE gateway_id=? LIMIT 1', (parsed['gateway_id'],))
        if existing:
            return jsonify({'ok': True, 'duplicate': True, 'receipt_id': existing['id'], 'classification': existing['classification'], 'order_id': existing['matched_order_id']})
    cur = execute('''INSERT INTO payment_receipts(received_at,gateway_id,sim_device,sender,payer_name,payer_phone,amount,transaction_code,raw_message,classification,delivery,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (parsed['received_at'],parsed['gateway_id'],parsed['sim_device'],parsed['sender'],parsed['payer_name'],parsed['payer_phone'],float(parsed['amount']) if parsed['amount'] is not None else None,parsed['transaction_code'],parsed['raw_message'],'M-PESA CANDIDATE',parsed['delivery'],now_iso()))
    receipt_id = cur.lastrowid
    order_id, reason = classify_gateway_payment(parsed, receipt_id)
    if order_id:
        return jsonify({'ok': True, 'receipt_id': receipt_id, 'classification': 'PAYMENT_MATCHED', 'order_id': order_id, 'message': reason}), 200
    return jsonify({'ok': True, 'receipt_id': receipt_id, 'classification': query_one('SELECT classification FROM payment_receipts WHERE id=?',(receipt_id,))['classification'], 'message': reason}), 202


@app.route('/api/admin/chat/read', methods=['POST'])
@admin_required
def admin_chat_read():
    execute('UPDATE chat_messages SET is_read=1 WHERE is_read=0')
    return jsonify({'ok': True})


@app.route('/admin/chat')
@admin_required
def admin_chat():
    messages = query_all('SELECT * FROM chat_messages ORDER BY id ASC LIMIT 150')
    return render_template('admin_chat.html', messages=messages)



def certificate_payload(row):
    return '|'.join(str(row.get(k, '')) for k in (
        'serial', 'recipient_name', 'business_name', 'software_name',
        'award_title', 'issuer_name', 'issuer_title', 'award_date', 'notes'
    ))


def certificate_signature(payload):
    secret = (os.environ.get('SECRET_KEY') or get_admin_password() or 'toror-certificate-secret').encode('utf-8')
    return hmac.new(secret, payload.encode('utf-8'), hashlib.sha256).hexdigest()[:40].upper()


def certificate_base_url():
    configured = _env_value('CERTIFICATE_BASE_URL') or get_setting('certificate_base_url', '')
    return configured.rstrip('/') if configured else request.url_root.rstrip('/')


def certificate_verification_url(serial, sig):
    return f"{certificate_base_url()}{url_for('verify_certificate', serial=serial, sig=sig)}"


def signature_file_path(signature_path):
    if not signature_path:
        return None
    rel = signature_path.lstrip('/').replace('/', os.sep)
    path = BASE_DIR / rel
    return path if path.exists() and path.is_file() else None


def _draw_wrapped_centered(c, text, font_name, font_size, center_x, y, max_width, leading=None, color=None):
    words = str(text or '').split()
    if not words:
        return y
    leading = leading or font_size * 1.28
    lines, line = [], ''
    for word in words:
        trial = f'{line} {word}'.strip()
        if stringWidth(trial, font_name, font_size) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    if color:
        c.setFillColor(color)
    c.setFont(font_name, font_size)
    for item in lines:
        c.drawCentredString(center_x, y, item)
        y -= leading
    return y


def _local_logo_path(logo_path=None):
    """Resolve a stored /static/... logo URL to a local file for ReportLab."""
    candidate = (logo_path or public_logo() or '').strip()
    if candidate.startswith('/'):
        candidate = candidate[1:]
    path = (BASE_DIR / candidate).resolve()
    try:
        path.relative_to(BASE_DIR.resolve())
    except ValueError:
        return None
    return path if path.exists() and path.is_file() else None


def _draw_logo(c, logo_path, center_x, center_y, max_w, max_h):
    """Draw the current company logo, supporting raster files and SVG uploads."""
    path = _local_logo_path(logo_path)
    if not path:
        return False
    try:
        ext = path.suffix.lower()
        if ext == '.svg':
            drawing = svg2rlg(str(path))
            if not drawing or not drawing.width or not drawing.height:
                return False
            scale = min(max_w / float(drawing.width), max_h / float(drawing.height))
            drawing.width *= scale
            drawing.height *= scale
            drawing.scale(scale, scale)
            renderPDF.draw(drawing, c, center_x - drawing.width / 2, center_y - drawing.height / 2)
            return True
        c.drawImage(
            ImageReader(str(path)),
            center_x - max_w / 2,
            center_y - max_h / 2,
            max_w,
            max_h,
            preserveAspectRatio=True,
            anchor='c',
            mask='auto',
        )
        return True
    except Exception:
        return False


def _draw_certificate_corner(c, x, y, size, maroon, gold, sky_dark):
    c.saveState()
    c.setStrokeColor(maroon)
    c.setLineWidth(1.7)
    c.line(x, y, x + size, y)
    c.line(x, y, x, y + size)
    c.setStrokeColor(gold)
    c.setLineWidth(0.8)
    c.line(x + 7, y + 7, x + size - 7, y + 7)
    c.line(x + 7, y + 7, x + 7, y + size - 7)
    c.setStrokeColor(sky_dark)
    c.setLineWidth(0.55)
    c.line(x + 17, y + 17, x + size - 17, y + 17)
    c.line(x + 17, y + 17, x + 17, y + size - 17)
    c.restoreState()


def draw_certificate(c, row, verification_url):
    # A tall, print-friendly portrait certificate - visually closer to a premium
    # mobile/portrait card while remaining standard A4 for normal printing.
    width, height = A4
    maroon = colors.HexColor('#7B2431')
    dark_maroon = colors.HexColor('#5A111D')
    sky = colors.HexColor('#A9DBEA')
    sky_dark = colors.HexColor('#4E94A8')
    gold = colors.HexColor('#C9A65A')
    ink = colors.HexColor('#151517')
    muted = colors.HexColor('#5D6B70')
    paper = colors.HexColor('#F8FBFC')
    white = colors.white

    c.setTitle(f"{row.get('award_title') or 'Certificate'} - {row.get('recipient_name') or 'Recipient'}")
    c.setFillColor(paper)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Elegant top and bottom colour bands.
    c.setFillColor(dark_maroon)
    c.rect(0, height - 13, width, 13, fill=1, stroke=0)
    c.setFillColor(maroon)
    c.rect(0, 0, width, 7, fill=1, stroke=0)

    # Main portrait plaque with generous whitespace.
    plaque_x, plaque_y = 35, 31
    plaque_w, plaque_h = width - 70, height - 62
    c.setFillColor(white)
    c.setStrokeColor(colors.HexColor('#D8C9A5'))
    c.setLineWidth(0.9)
    c.roundRect(plaque_x, plaque_y, plaque_w, plaque_h, 21, fill=1, stroke=1)
    c.setStrokeColor(colors.HexColor('#7D9EA8'))
    c.setLineWidth(0.5)
    c.roundRect(plaque_x + 10, plaque_y + 10, plaque_w - 20, plaque_h - 20, 16, fill=0, stroke=1)

    _draw_certificate_corner(c, plaque_x + 18, plaque_y + plaque_h - 66, 38, maroon, gold, sky_dark)
    _draw_certificate_corner(c, plaque_x + plaque_w - 56, plaque_y + plaque_h - 66, 38, maroon, gold, sky_dark)
    _draw_certificate_corner(c, plaque_x + 18, plaque_y + 28, 38, maroon, gold, sky_dark)
    _draw_certificate_corner(c, plaque_x + plaque_w - 56, plaque_y + 28, 38, maroon, gold, sky_dark)

    cx = width / 2

    # Company mark: the uploaded official logo, never a hard-coded letter.
    logo_box_y = height - 122
    c.setFillColor(colors.HexColor('#EFF7FA'))
    c.circle(cx, logo_box_y, 44, fill=1, stroke=0)
    c.setStrokeColor(gold)
    c.setLineWidth(1.1)
    c.circle(cx, logo_box_y, 47, fill=0, stroke=1)
    if not _draw_logo(c, row.get('certificate_logo_path') or public_logo(), cx, logo_box_y, 67, 67):
        # Last-resort textual fallback only when an actual image cannot be read.
        c.setFillColor(dark_maroon)
        c.setFont('Helvetica-Bold', 17)
        c.drawCentredString(cx, logo_box_y - 6, 'TOROR')

    company = (get_setting('site_name', 'Toror Technology Company Ltd') or 'Toror Technology Company Ltd').strip()
    c.setFillColor(dark_maroon)
    c.setFont('Helvetica-Bold', 10.5)
    c.drawCentredString(cx, height - 177, company.upper()[:70])
    c.setFillColor(sky_dark)
    c.setFont('Helvetica', 7.3)
    c.drawCentredString(cx, height - 192, 'OFFICIAL TECHNOLOGY PARTNERSHIP RECOGNITION')

    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 22)
    heading_y = _draw_wrapped_centered(c, 'CERTIFICATE OF TECHNOLOGY PARTNERSHIP', 'Helvetica-Bold', 22, cx, height - 232, 455, leading=26, color=ink)
    c.setStrokeColor(gold)
    c.setLineWidth(1.25)
    c.line(cx - 92, heading_y - 3, cx + 92, heading_y - 3)
    c.setFillColor(muted)
    c.setFont('Helvetica', 8.7)
    c.drawCentredString(cx, heading_y - 22, 'Presented with appreciation for trust, adoption and collaboration')

    # Recipient block.
    body_top = height - 335
    c.setFillColor(muted)
    c.setFont('Helvetica', 9.6)
    c.drawCentredString(cx, body_top, 'THIS CERTIFICATE IS PROUDLY PRESENTED TO')
    c.setFillColor(dark_maroon)
    c.setFont('Helvetica-Bold', 27)
    recipient = str(row.get('recipient_name') or 'John Doe')[:44]
    c.drawCentredString(cx, body_top - 39, recipient)

    business = str(row.get('business_name') or 'Example Organisation')
    c.setFillColor(ink)
    c.setFont('Helvetica', 10)
    c.drawCentredString(cx, body_top - 62, f'of {business[:66]}')

    software = str(row.get('software_name') or 'Toror Technology Platform')
    text_y = _draw_wrapped_centered(
        c,
        f'For choosing and adopting {software} as part of a practical technology partnership with {company}.',
        'Helvetica',
        9.8,
        cx,
        body_top - 92,
        360,
        leading=14,
        color=ink,
    )

    award_title = str(row.get('award_title') or 'Technology Partnership Recognition')[:70]
    c.setFillColor(maroon)
    c.setFont('Helvetica-Bold', 11.8)
    c.drawCentredString(cx, text_y - 6, award_title)

    note = (row.get('notes') or '').strip()
    if note:
        _draw_wrapped_centered(c, note, 'Helvetica', 8.25, cx, text_y - 29, 350, leading=11.2, color=muted)

    # Lower trust line and metadata, grouped tightly so the portrait layout feels intentional.
    c.setStrokeColor(colors.HexColor('#DFE9EC'))
    c.setLineWidth(0.7)
    c.line(plaque_x + 44, 255, plaque_x + plaque_w - 44, 255)
    c.setFillColor(sky_dark)
    c.setFont('Helvetica-Bold', 7.1)
    c.drawString(plaque_x + 52, 238, 'CERTIFICATE NUMBER')
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 7.2)
    c.drawString(plaque_x + 52, 225, str(row.get('serial') or '')[:34])
    c.setFillColor(muted)
    c.setFont('Helvetica', 6.7)
    c.drawString(plaque_x + 52, 213, 'Digitally recorded and independently verifiable')
    c.setFillColor(sky_dark)
    c.setFont('Helvetica-Bold', 7.1)
    c.drawString(plaque_x + 300, 238, 'ISSUE DATE')
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 7.2)
    c.drawString(plaque_x + 300, 225, str(row.get('award_date') or '')[:30])

    # Issuer/signatory zone remains visually separate from the verification area.
    sig_y = 157
    sig_left = plaque_x + 50
    sig_right = plaque_x + 225
    c.setStrokeColor(ink)
    c.setLineWidth(0.7)
    c.line(sig_left, sig_y, sig_right, sig_y)
    sig_path = signature_file_path(row.get('issuer_signature_path'))
    if sig_path and sig_path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
        try:
            c.drawImage(ImageReader(str(sig_path)), sig_left + 6, sig_y + 4, 150, 43, preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass
    else:
        signature_name = str(row.get('issuer_name') or row.get('awarded_by') or 'Authorised Signatory')[:28]
        c.setFillColor(dark_maroon)
        c.setFont('Helvetica-Oblique', 14)
        c.drawCentredString((sig_left + sig_right) / 2, sig_y + 17, signature_name)
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 7.9)
    c.drawCentredString((sig_left + sig_right) / 2, 141, str(row.get('issuer_name') or row.get('awarded_by') or 'Authorised Signatory')[:34])
    c.setFillColor(sky_dark)
    c.setFont('Helvetica', 6.9)
    c.drawCentredString((sig_left + sig_right) / 2, 130, str(row.get('issuer_title') or 'Authorised Signatory')[:34])

    date_left = plaque_x + 263
    date_right = plaque_x + 385
    c.setStrokeColor(ink)
    c.line(date_left, sig_y, date_right, sig_y)
    date_center = (date_left + date_right) / 2
    c.setFillColor(ink)
    award_end = _draw_wrapped_centered(
        c,
        str(row.get('award_title') or 'Technology Partnership Recognition'),
        'Helvetica-Bold',
        7.4,
        date_center,
        141,
        118,
        leading=8.4,
        color=ink,
    )
    c.setFillColor(sky_dark)
    c.setFont('Helvetica', 6.9)
    c.drawCentredString(date_center, award_end - 2, 'AWARD TITLE')

    # QR verification panel, intentionally compact and integrated into the portrait layout.
    panel_x, panel_y, panel_w, panel_h = plaque_x + 50, 49, plaque_w - 100, 67
    c.setFillColor(colors.HexColor('#F3F8F9'))
    c.setStrokeColor(colors.HexColor('#C8DCE1'))
    c.setLineWidth(0.65)
    c.roundRect(panel_x, panel_y, panel_w, panel_h, 12, fill=1, stroke=1)
    qr = qrcode.QRCode(version=4, box_size=3, border=2)
    qr.add_data(verification_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#5A111D', back_color='#FFFFFF').convert('RGB')
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    c.drawImage(ImageReader(buf), panel_x + 7, panel_y + 7, 53, 53, preserveAspectRatio=True, mask='auto')
    detail_x = panel_x + 70
    c.setFillColor(dark_maroon)
    c.setFont('Helvetica-Bold', 7.0)
    c.drawString(detail_x, panel_y + 47, 'VERIFY AUTHENTICITY')
    c.setFillColor(ink)
    c.setFont('Helvetica-Bold', 6.0)
    c.drawString(detail_x, panel_y + 35, str(row.get('serial') or '')[:32])
    auth = str(row.get('verification_sig') or '')
    c.setFont('Helvetica', 5.1)
    c.drawString(detail_x, panel_y + 25, auth[:28])
    c.drawString(detail_x, panel_y + 16, auth[28:56])
    c.setFillColor(sky_dark)
    c.setFont('Helvetica', 5.5)
    c.drawString(detail_x, panel_y + 6, 'SCAN TO OPEN THE OFFICIAL ONLINE RECORD')

    c.setFillColor(muted)
    c.setFont('Helvetica', 5.8)
    c.drawCentredString(cx, 25, 'This certificate is issued as part of the official recognition records of the organisation.')
    c.showPage()
    c.save()

def create_certificate_pdf(row):
    if not REPORTING_AVAILABLE:
        raise RuntimeError('Certificate generation dependencies are not installed.')
    folder = UPLOAD_DIR / 'certificates'; folder.mkdir(parents=True, exist_ok=True)
    filename = f"{row['serial']}.pdf"; path = folder / filename
    verify_url = certificate_verification_url(row['serial'], row['verification_sig'])
    c = canvas.Canvas(str(path), pagesize=A4); draw_certificate(c, row, verify_url)
    return str(path.relative_to(BASE_DIR))


@app.route('/admin/certificates', methods=['GET','POST'])
@admin_required
def admin_certificates():
    if request.method == 'POST':
        recipient = request.form.get('recipient_name','').strip()
        business = request.form.get('business_name','').strip()
        software = request.form.get('software_name','').strip()
        award_title = request.form.get('award_title','').strip() or get_setting('certificate_default_title', 'Technology Partnership Recognition')
        issuer_name = request.form.get('issuer_name','').strip() or get_setting('issuer_default_name', get_admin_name())
        issuer_title = request.form.get('issuer_title','').strip() or get_setting('issuer_default_title', 'Chief Executive Officer')
        award_date = request.form.get('award_date','').strip() or datetime.now().strftime('%d %B %Y')
        notes = request.form.get('notes','').strip() or get_setting('certificate_default_note', 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.')
        issuer_signature_path = get_setting('issuer_signature_path', '')
        if not recipient or not business or not software or not issuer_name or not issuer_title:
            flash('Recipient, business, software, issuer name, and issuer title are required.', 'error')
            return redirect(url_for('admin_certificates'))
        serial = f"TOROR-{datetime.now().year}-{secrets.token_hex(5).upper()}"
        while query_one('SELECT 1 FROM certificates WHERE serial=?', (serial,)):
            serial = f"TOROR-{datetime.now().year}-{secrets.token_hex(5).upper()}"
        row = {
            'serial': serial,
            'recipient_name': recipient,
            'business_name': business,
            'software_name': software,
            'award_title': award_title,
            'issuer_name': issuer_name,
            'issuer_title': issuer_title,
            'awarded_by': issuer_name,  # compatibility with older records/schema
            'award_date': award_date,
            'notes': notes,
            'issuer_signature_path': issuer_signature_path,
            'certificate_logo_path': public_logo(),
        }
        sig = certificate_signature(certificate_payload(row))
        row['verification_sig'] = sig
        try:
            rel = create_certificate_pdf(row)
        except RuntimeError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('admin_certificates'))
        execute('''INSERT INTO certificates
            (serial,verification_sig,recipient_name,business_name,software_name,award_title,awarded_by,issuer_name,issuer_title,issuer_signature_path,certificate_logo_path,award_date,notes,pdf_filename,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (serial,sig,recipient,business,software,award_title,issuer_name,issuer_name,issuer_title,issuer_signature_path,row['certificate_logo_path'],award_date,notes,rel,now_iso()))
        flash(f'Certificate {serial} created.', 'success')
        return redirect(url_for('admin_certificates'))
    certificates = query_all('SELECT * FROM certificates ORDER BY id DESC')
    return render_template(
        'admin_certificates.html',
        certificates=certificates,
        issuer_default_name=get_setting('issuer_default_name', get_admin_name()),
        issuer_default_title=get_setting('issuer_default_title', 'Chief Executive Officer'),
        issuer_signature_path=get_setting('issuer_signature_path', ''),
        certificate_default_title=get_setting('certificate_default_title', 'Technology Partnership Recognition'),
        certificate_default_note=get_setting('certificate_default_note', 'In recognition of your decision to adopt our technology solution and begin a meaningful partnership with Toror Technology Company Ltd. We appreciate your trust and look forward to the value created through this collaboration.'),
        certificate_base_url=certificate_base_url(),
    )


@app.route('/admin/certificates/<int:certificate_id>/download')
@admin_required
def admin_certificate_download(certificate_id):
    row = query_one('SELECT * FROM certificates WHERE id=?', (certificate_id,))
    if not row:
        abort(404)
    row = dict(row)
    row['issuer_name'] = row.get('issuer_name') or row.get('awarded_by') or get_setting('issuer_default_name', get_admin_name())
    row['issuer_title'] = row.get('issuer_title') or get_setting('issuer_default_title', 'Chief Executive Officer')
    row['issuer_signature_path'] = row.get('issuer_signature_path') or ''
    row['certificate_logo_path'] = row.get('certificate_logo_path') or public_logo()
    # Re-render on download so the improved portrait design is applied to older
    # records too, while preserving their stored recipient/issuer/signature data.
    rel = create_certificate_pdf(dict(row))
    execute('UPDATE certificates SET pdf_filename=? WHERE id=?', (rel,certificate_id))
    path = BASE_DIR / rel
    return send_from_directory(str(path.parent), path.name, as_attachment=True, download_name=path.name)


@app.route('/admin/certificates/<int:certificate_id>/delete', methods=['POST'])
@admin_required
def admin_certificate_delete(certificate_id):
    row = query_one('SELECT pdf_filename FROM certificates WHERE id=?', (certificate_id,))
    if row and row['pdf_filename']:
        try: (BASE_DIR / row['pdf_filename']).unlink(missing_ok=True)
        except Exception: pass
    execute('DELETE FROM certificates WHERE id=?', (certificate_id,))
    flash('Certificate removed.', 'success')
    return redirect(url_for('admin_certificates'))


@app.route('/verify')
def verify_certificate_form():
    serial = (request.args.get('serial') or '').strip().upper()
    sig = (request.args.get('sig') or '').strip().upper()
    if serial:
        return verify_certificate(serial, sig_override=sig)
    return render_template('verify.html', certificate=None, valid=False, serial='')


@app.route('/verify/<serial>')
def verify_certificate(serial, sig_override=None):
    row = query_one('SELECT * FROM certificates WHERE serial=?', (serial.upper(),))
    provided = (sig_override if sig_override is not None else request.args.get('sig') or '').upper()
    valid = bool(row and provided and hmac.compare_digest(provided, row['verification_sig']))
    return render_template('verify.html', certificate=row, valid=valid, serial=serial.upper())


@app.route('/qr/home.png')
def home_qr():
    if not REPORTING_AVAILABLE:
        abort(503)
    target = request.url_root.rstrip('/') + url_for('index')
    qr = qrcode.QRCode(version=4, box_size=9, border=4)
    qr.add_data(target)
    qr.make(fit=True)
    image = qr.make_image(fill_color='#5A111D', back_color='#F4FBFE').convert('RGB')
    buf = BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    response = app.response_class(buf.getvalue(), mimetype='image/png')
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@app.route('/favicon.ico')
def favicon():
    return redirect(public_logo())


@app.route('/pulse_receiver', methods=['GET', 'POST'])
def pulse_receiver():
    return jsonify({'ok': True, 'service': 'toror'})


@app.route('/api/version')
def version():
    return jsonify({'site_name': get_setting('site_name'), 'public_mode': True, 'generated_at': now_iso()})


@app.route('/robots.txt')
def robots():
    base = request.url_root.rstrip('/')
    return app.response_class(f'User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /promise212324\nSitemap: {base}/sitemap.xml\n', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap():
    base = request.url_root.rstrip('/')
    paths = ['/', '/about', '/services', '/work', '/store', '/faq', '/contact', '/privacy', '/terms', '/verify']
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(f'<url><loc>{base}{path}</loc></url>' for path in paths) + '</urlset>'
    return app.response_class(xml, mimetype='application/xml')


@app.route('/sw.js')
def service_worker():
    response = send_from_directory(BASE_DIR / 'static' / 'js', 'sw.js', mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@app.route('/manifest.webmanifest')
def manifest():
    return jsonify({
        'name': get_setting('site_name'),
        'short_name': 'Toror Tech',
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#F2FAFD',
        'theme_color': '#641521',
        'icons': [
            {'src': public_logo(), 'sizes': '192x192', 'type': mimetypes.guess_type(public_logo())[0] or 'image/svg+xml'}
        ]
    })


init_db()
with app.app_context():
    ensure_chat_privacy_schema()
    backfill_chat_owner_ids()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), debug=True)
