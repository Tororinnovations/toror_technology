import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import smtplib
import sqlite3
import zipfile
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
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    REPORTING_AVAILABLE = True
except ImportError:  # pragma: no cover - deployment dependency guard
    REPORTING_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / os.environ.get('TOROR_DATA_DIR', 'data')
UPLOAD_DIR = BASE_DIR / 'static' / 'uploads'
DB_PATH = Path(os.environ.get('TOROR_DB_PATH', DATA_DIR / 'toror.db'))

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=220 * 1024 * 1024,
)

ALLOWED_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
ALLOWED_DOC_EXTS = {'.pdf', '.xlsx', '.xls', '.docx', '.pptx', '.txt'}
ALLOWED_VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v'}
ALLOWED_PROJECT_EXTS = ALLOWED_IMAGE_EXTS | ALLOWED_DOC_EXTS | ALLOWED_VIDEO_EXTS


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
        'login_enabled': '0',
        'chat_enabled': '0',
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
        for name, sql in {
            'issuer_name': 'ALTER TABLE certificates ADD COLUMN issuer_name TEXT',
            'issuer_title': 'ALTER TABLE certificates ADD COLUMN issuer_title TEXT',
            'issuer_signature_path': 'ALTER TABLE certificates ADD COLUMN issuer_signature_path TEXT',
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
        'login_enabled': '0',
        'chat_enabled': '0',
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
    # Keep the public portfolio tightly focused on the current Toror products/sites.
    curated = [
        ('Oedge', 'Toror digital services and mobility platform.', 'https://oedge.onrender.com/', 'Active'),
        ('Denmart', 'Denmart online business platform.', 'https://denmart.co.ke/', 'Active'),
        ('O Travel', 'Toror travel and ticketing platform.', 'https://otravel-bleg.onrender.com/', 'Active'),
        ('Prime School Platform', 'Digital school management platform by Toror Technology Company Ltd.', 'https://prime-1-rd0g.onrender.com/', 'Active'),
    ]
    legacy_links = {
        'https://kerryconnect360.onrender.com/',
        '1. https://e-agriculture.onrender.com',
        'https://beacon-cloud.onrender.com/friendly',
        'https://tomorrow-au2q.onrender.com/business.html',
    }
    rows = db.execute('SELECT id, link FROM projects ORDER BY id ASC').fetchall()
    legacy_ids = [r[0] for r in rows if r[1] in legacy_links]
    for idx, item in enumerate(curated):
        if idx < len(legacy_ids):
            db.execute('UPDATE projects SET title=?, summary=?, link=?, status=? WHERE id=?', (*item, legacy_ids[idx]))
        else:
            db.execute('INSERT INTO projects(title,summary,link,status,created_at) VALUES (?,?,?,?,?)', (*item, now_iso()))
    # If the shipped database already has a different project set, ensure these four links
    # are present without deleting additional admin-created work.
    existing_links = {r[0] for r in db.execute('SELECT link FROM projects').fetchall()}
    for item in curated:
        if item[2] not in existing_links:
            db.execute('INSERT INTO projects(title,summary,link,status,created_at) VALUES (?,?,?,?,?)', (*item, now_iso()))


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
    return render_template('home.html', projects=projects)


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
    }
    recent_users = query_all('SELECT * FROM users ORDER BY id DESC LIMIT 8')
    recent_contacts = query_all('SELECT * FROM contacts ORDER BY id DESC LIMIT 8')
    return render_template('admin_dashboard.html', stats=stats, recent_users=recent_users, recent_contacts=recent_contacts, admin_name=get_admin_name())


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
            'contains': ['SQLite database', 'static uploaded assets'],
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


def draw_certificate(c, row, verification_url):
    width, height = landscape(A4)
    maroon = colors.HexColor('#7B2431')
    dark_maroon = colors.HexColor('#5A111D')
    sky = colors.HexColor('#A9DBEA')
    sky_dark = colors.HexColor('#4E94A8')
    gold = colors.HexColor('#C9A65A')
    ink = colors.HexColor('#151517')
    paper = colors.HexColor('#F4FBFE')
    white = colors.white

    c.setFillColor(paper); c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(maroon); c.rect(0, 0, width, 20, fill=1, stroke=0); c.rect(0, height-20, width, 20, fill=1, stroke=0)

    # Distinctive layered certificate frame.
    for inset, stroke, sw in [(27, maroon, 3.2), (36, gold, 1.3), (45, sky_dark, 0.9)]:
        c.setStrokeColor(stroke); c.setLineWidth(sw); c.roundRect(inset, inset, width-2*inset, height-2*inset, 10, fill=0, stroke=1)

    # Fine security geometry kept away from the content area.
    c.saveState()
    c.setStrokeColor(colors.Color(sky.red, sky.green, sky.blue, alpha=0.35)); c.setLineWidth(0.45)
    for x in range(70, int(width)-70, 22):
        c.line(x, 55, x+38, height-55); c.line(x, height-55, x+38, 55)
    c.restoreState()

    # Central Toror medallion.
    cx, cy = width/2, height*0.68
    c.setFillColor(sky); c.circle(cx, cy, 40, fill=1, stroke=0)
    c.setStrokeColor(maroon); c.setLineWidth(3.5); c.circle(cx, cy, 45, fill=0, stroke=1)
    c.setStrokeColor(gold); c.setLineWidth(1.3); c.circle(cx, cy, 50, fill=0, stroke=1)
    c.setFillColor(dark_maroon); c.setFont('Helvetica-Bold', 25); c.drawCentredString(cx, cy-9, 'T')

    # Header.
    c.setFillColor(maroon); c.setFont('Helvetica-Bold', 12); c.drawCentredString(cx, height-58, 'TOROR TECHNOLOGY COMPANY LTD')
    c.setFillColor(ink); c.setFont('Helvetica-Bold', 24); c.drawCentredString(cx, height-96, 'CERTIFICATE OF TECHNOLOGY PARTNERSHIP')
    c.setFillColor(sky_dark); c.setFont('Helvetica', 9.5); c.drawCentredString(cx, height-115, 'Digitally issued recognition with independent online verification')

    # Recipient body, with a deliberate exclusion zone around the QR panel below.
    body_y = height*0.505
    c.setFillColor(ink); c.setFont('Helvetica', 10.5); c.drawCentredString(cx, body_y, 'This certificate is proudly presented to')
    c.setFillColor(dark_maroon); c.setFont('Helvetica-Bold', 26); c.drawCentredString(cx, body_y-36, row['recipient_name'])
    c.setFillColor(ink); c.setFont('Helvetica', 10.5); c.drawCentredString(cx, body_y-59, f"of {row['business_name']}")
    _draw_wrapped_centered(c, f"for acquiring and adopting {row['software_name']} from Toror Technology Company Ltd.", 'Helvetica', 10.5, cx, body_y-86, 650, color=ink)
    c.setFillColor(maroon); c.setFont('Helvetica-Bold', 12.5); c.drawCentredString(cx, body_y-113, row['award_title'])
    note = row.get('notes') or ''
    if note:
        _draw_wrapped_centered(c, note, 'Helvetica', 8.5, cx, body_y-136, 560, leading=10, color=ink)

    # Issuer/signature zone: fully separate from QR zone.
    left_x = 125
    sig_y = 73
    c.setStrokeColor(ink); c.setLineWidth(0.75); c.line(55, sig_y, 205, sig_y)
    sig_path = signature_file_path(row.get('issuer_signature_path'))
    from reportlab.lib.utils import ImageReader
    if sig_path and sig_path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
        try:
            c.drawImage(ImageReader(str(sig_path)), 63, sig_y+4, 134, 42, preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass
    else:
        signature_name = row.get('issuer_name') or row.get('awarded_by') or 'Authorised Signatory'
        c.setFillColor(dark_maroon); c.setFont('Helvetica-Oblique', 18); c.drawCentredString(130, sig_y+14, signature_name[:28])
        c.setStrokeColor(dark_maroon); c.setLineWidth(1.1); c.bezier(72, sig_y+9, 108, sig_y-2, 151, sig_y+11, 194, sig_y+1)
    c.setFillColor(ink); c.setFont('Helvetica-Bold', 8.5); c.drawString(55, 59, (row.get('issuer_name') or row.get('awarded_by') or 'Authorised Signatory')[:38])
    c.setFillColor(sky_dark); c.setFont('Helvetica', 7.5); c.drawString(55, 47, (row.get('issuer_title') or 'Authorised Signatory')[:38])
    c.setFont('Helvetica', 6.8); c.drawString(55, 36, 'Electronic signature / authorised issuer')

    # Date zone.
    c.setStrokeColor(ink); c.setLineWidth(0.75); c.line(250, sig_y, 380, sig_y)
    c.setFillColor(ink); c.setFont('Helvetica-Bold', 9); c.drawCentredString(315, 58, row['award_date'])
    c.setFillColor(sky_dark); c.setFont('Helvetica', 7.5); c.drawCentredString(315, 46, 'Award date')

    # Dedicated QR authenticity panel; no body/signature text enters this reserved rectangle.
    panel_x, panel_y, panel_w, panel_h = width-247, 38, 202, 103
    c.setFillColor(colors.Color(sky.red, sky.green, sky.blue, alpha=0.12)); c.roundRect(panel_x, panel_y, panel_w, panel_h, 9, fill=1, stroke=0)
    c.setStrokeColor(sky_dark); c.setLineWidth(0.8); c.roundRect(panel_x, panel_y, panel_w, panel_h, 9, fill=0, stroke=1)
    qr = qrcode.QRCode(version=4, box_size=3, border=2); qr.add_data(verification_url); qr.make(fit=True)
    img = qr.make_image(fill_color='#5A111D', back_color='#F4FBFE').convert('RGB')
    buf = BytesIO(); img.save(buf, format='PNG'); buf.seek(0)
    c.drawImage(ImageReader(buf), panel_x+8, panel_y+12, 78, 78, preserveAspectRatio=True, mask='auto')
    detail_x = panel_x + 96
    c.setFillColor(dark_maroon); c.setFont('Helvetica-Bold', 6.8); c.drawString(detail_x, panel_y+78, 'SCAN TO VERIFY')
    c.setFillColor(ink); c.setFont('Helvetica-Bold', 6.0); c.drawString(detail_x, panel_y+63, row['serial'])
    c.setFillColor(sky_dark); c.setFont('Helvetica-Bold', 5.7); c.drawString(detail_x, panel_y+48, 'AUTHENTICITY CODE')
    auth = str(row.get('verification_sig') or '')
    c.setFillColor(ink); c.setFont('Helvetica', 5.0)
    c.drawString(detail_x, panel_y+36, auth[:22])
    c.drawString(detail_x, panel_y+26, auth[22:44])
    c.setFillColor(sky_dark); c.setFont('Helvetica', 5.5); c.drawString(detail_x, panel_y+12, 'ONLINE RECORD')
    c.showPage(); c.save()


def create_certificate_pdf(row):
    if not REPORTING_AVAILABLE:
        raise RuntimeError('Certificate generation dependencies are not installed.')
    folder = UPLOAD_DIR / 'certificates'; folder.mkdir(parents=True, exist_ok=True)
    filename = f"{row['serial']}.pdf"; path = folder / filename
    verify_url = certificate_verification_url(row['serial'], row['verification_sig'])
    c = canvas.Canvas(str(path), pagesize=landscape(A4)); draw_certificate(c, row, verify_url)
    return str(path.relative_to(BASE_DIR))


@app.route('/admin/certificates', methods=['GET','POST'])
@admin_required
def admin_certificates():
    if request.method == 'POST':
        recipient = request.form.get('recipient_name','').strip()
        business = request.form.get('business_name','').strip()
        software = request.form.get('software_name','').strip()
        award_title = request.form.get('award_title','').strip() or 'Technology Partnership Recognition'
        issuer_name = request.form.get('issuer_name','').strip() or get_setting('issuer_default_name', get_admin_name())
        issuer_title = request.form.get('issuer_title','').strip() or get_setting('issuer_default_title', 'Chief Executive Officer')
        award_date = request.form.get('award_date','').strip() or datetime.now().strftime('%d %B %Y')
        notes = request.form.get('notes','').strip()
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
        }
        sig = certificate_signature(certificate_payload(row))
        row['verification_sig'] = sig
        try:
            rel = create_certificate_pdf(row)
        except RuntimeError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('admin_certificates'))
        execute('''INSERT INTO certificates
            (serial,verification_sig,recipient_name,business_name,software_name,award_title,awarded_by,issuer_name,issuer_title,issuer_signature_path,award_date,notes,pdf_filename,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (serial,sig,recipient,business,software,award_title,issuer_name,issuer_name,issuer_title,issuer_signature_path,award_date,notes,rel,now_iso()))
        flash(f'Certificate {serial} created.', 'success')
        return redirect(url_for('admin_certificates'))
    certificates = query_all('SELECT * FROM certificates ORDER BY id DESC')
    return render_template(
        'admin_certificates.html',
        certificates=certificates,
        issuer_default_name=get_setting('issuer_default_name', get_admin_name()),
        issuer_default_title=get_setting('issuer_default_title', 'Chief Executive Officer'),
        issuer_signature_path=get_setting('issuer_signature_path', ''),
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
    path = BASE_DIR / row['pdf_filename'] if row['pdf_filename'] else None
    if not path or not path.exists():
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
    paths = ['/', '/about', '/services', '/work', '/faq', '/contact', '/privacy', '/terms', '/verify']
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
