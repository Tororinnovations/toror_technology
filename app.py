import mimetypes
import os
import secrets
import smtplib
import sqlite3
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
        notes TEXT,
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
        'site_name': 'Toror Technology and Innovations Ltd',
        'developer_name': 'Developed by me',
        'tagline': 'Toror Technology and Innovations Ltd',
        'primary_email': os.environ.get('PRIMARY_EMAIL', 'hello@example.com'),
        'hero_text': 'Register to continue.',
        'logo_path': '/static/default-logo.svg',
        'theme_mode': 'light',
        'accent_color': '#2563eb',
        'login_enabled': '1',
        'chat_enabled': '1',
    }
    db = sqlite3.connect(DB_PATH)
    try:
        db.executescript(schema)
        for key, value in defaults.items():
            db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (key, value))
        db.commit()
    finally:
        db.close()


def get_setting(key, default=''):
    row = query_one('SELECT value FROM settings WHERE key=?', (key,))
    return row['value'] if row else default


def set_setting(key, value):
    execute(
        'INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value),
    )


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
        'site_name': get_setting('site_name', 'Toror Technology and Innovations Ltd'),
        'developer_name': get_setting('developer_name', 'Developed by me'),
        'tagline': get_setting('tagline', ''),
        'hero_text': get_setting('hero_text', ''),
        'primary_email': get_setting('primary_email', ''),
        'logo_path': public_logo(),
        'theme_mode': get_setting('theme_mode', 'light'),
        'accent_color': get_setting('accent_color', '#2563eb'),
        'is_admin': is_admin,
        'show_nav': is_user_logged_in() or is_admin(),
        'user_email': session.get('user_email'),
        'display_name': user_display_name(),
        'admin_mode': is_admin(),
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
    if is_user_logged_in() or is_admin():
        return redirect(url_for('portal'))
    return redirect(url_for('register'))


@app.route('/portal')
@user_required
def portal():
    projects = query_all('SELECT * FROM projects ORDER BY id DESC LIMIT 6')
    contacts = query_all('SELECT * FROM contacts ORDER BY id DESC LIMIT 6')
    return render_template('portal.html', projects=projects, contacts=contacts)


@app.route('/projects')
@user_required
def projects():
    rows = query_all('SELECT * FROM projects ORDER BY id DESC')
    file_map = {}
    for row in query_all('SELECT * FROM project_files ORDER BY id DESC'):
        file_map.setdefault(row['project_id'], []).append(row)
    return render_template('projects.html', projects=rows, project_files=file_map)


@app.route('/profile')
@user_required
def profile():
    return render_template('profile.html')


@app.route('/chat')
@user_required
def chat():
    messages = query_all('SELECT * FROM chat_messages ORDER BY id ASC LIMIT 100')
    return render_template('chat.html', messages=messages)


@app.route('/api/chat/poll')
@user_required
def chat_poll():
    last_id = int(request.args.get('last_id', '0'))
    rows = query_all('SELECT * FROM chat_messages WHERE id > ? ORDER BY id ASC', (last_id,))
    return jsonify([dict(row) for row in rows])


@app.route('/api/chat/send', methods=['POST'])
@user_required
def chat_send():
    message = (request.form.get('message') or '').strip()
    edit_id = (request.form.get('edit_id') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'Message required.'}), 400
    sender = session.get('user_email') or session.get('user_username') or 'admin'
    role = 'admin' if is_admin() else 'visitor'
    if edit_id:
        existing = query_one('SELECT * FROM chat_messages WHERE id=?', (edit_id,))
        if existing and (existing['sender'] == sender or is_admin()):
            execute(
                'UPDATE chat_messages SET message=?, edited=1, edited_at=? WHERE id=?',
                (message[:1200], now_iso(), edit_id),
            )
            return jsonify({'ok': True, 'edited': True})
    execute(
        'INSERT INTO chat_messages(sender, role, message, created_at) VALUES (?, ?, ?, ?)',
        (sender, role, message[:1200], now_iso()),
    )
    return jsonify({'ok': True, 'edited': False})


@app.route('/api/chat/delete/<int:message_id>', methods=['POST'])
@user_required
def chat_delete(message_id):
    row = query_one('SELECT * FROM chat_messages WHERE id=?', (message_id,))
    sender = session.get('user_email') or session.get('user_username') or 'admin'
    if not row or (row['sender'] != sender and not is_admin()):
        return jsonify({'ok': False, 'error': 'Not allowed.'}), 403
    execute('DELETE FROM chat_messages WHERE id=?', (message_id,))
    return jsonify({'ok': True})


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        if not name or not username or not email or not password:
            flash('Please complete all fields.', 'error')
            return redirect(url_for('register'))
        if query_one('SELECT 1 FROM users WHERE lower(name)=lower(?) OR lower(username)=lower(?) OR lower(email)=lower(?) LIMIT 1', (name, username, email)):
            flash('That name, username, or email already exists. Use a new one.', 'error')
            return redirect(url_for('register'))
        ctx = capture_client_context()
        execute(
            'INSERT INTO users(name, username, email, password_hash, location_text, location_lat, location_lng, device_info, ip_address, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (name, username, email, generate_password_hash(password), ctx['location_text'], ctx['location_lat'], ctx['location_lng'], ctx['device_info'], ctx['ip_address'], now_iso(), now_iso()),
        )
        flash('Account created. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = (request.form.get('identifier') or '').strip().lower()
        password = request.form.get('password') or ''
        if not identifier or not password:
            flash('Enter your username or email and password.', 'error')
            return redirect(url_for('login'))
        if get_setting('login_enabled', '1') != '1':
            flash('Access is temporarily closed.', 'error')
            return redirect(url_for('login'))
        user = query_one('SELECT * FROM users WHERE lower(username)=? OR lower(email)=? LIMIT 1', (identifier, identifier))
        if not user or not user['is_active']:
            flash('Invalid login details.', 'error')
            return redirect(url_for('login'))
        if not check_password_hash(user['password_hash'], password):
            flash('Invalid login details.', 'error')
            return redirect(url_for('login'))
        ctx = capture_client_context()
        execute(
            'UPDATE users SET location_text=?, location_lat=?, location_lng=?, device_info=?, ip_address=?, last_login_at=?, last_seen_at=? WHERE id=?',
            (ctx['location_text'], ctx['location_lat'], ctx['location_lng'], ctx['device_info'], ctx['ip_address'], now_iso(), now_iso(), user['id']),
        )
        session['user_id'] = user['id']
        session['user_email'] = user['email']
        session['user_username'] = user['username']
        flash('You are now logged in.', 'success')
        return redirect(url_for('portal'))
    return render_template('login.html')


@app.route('/login/verify/<token>')
def login_verify(token):
    row = query_one('SELECT * FROM login_tokens WHERE token=? AND used=0', (token,))
    if not row:
        abort(404)
    expires_at = datetime.fromisoformat(row['expires_at'])
    if expires_at < datetime.now(timezone.utc):
        flash('That login link has expired.', 'error')
        return redirect(url_for('login'))
    execute('UPDATE login_tokens SET used=1 WHERE token=?', (token,))
    user = query_one('SELECT * FROM users WHERE lower(email)=lower(?) LIMIT 1', (row['email'],))
    if user:
        session['user_id'] = user['id']
        session['user_email'] = user['email']
        session['user_username'] = user['username']
    flash('You are now logged in.', 'success')
    return redirect(url_for('portal'))


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('register'))


@app.route('/xtspolsjhulupjoppsuplmkzcodup', methods=['GET', 'POST'])
def admin_entry():

    open_mode = os.environ.get('ADMIN_DEVELOPMENT_OPEN', '1') == '1'
    if open_mode and request.method == 'GET' and not session.get('admin_logged_in'):
        session['admin_logged_in'] = True
        session['user_email'] = os.environ.get('ADMIN_EMAIL', 'admin@local')
        flash('Admin access opened for local use.', 'success')
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST' or not open_mode:
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == os.environ.get('ADMIN_USERNAME', 'admin') and password == os.environ.get('ADMIN_PASSWORD', 'change-me'):
            session['admin_logged_in'] = True
            session['user_email'] = os.environ.get('ADMIN_EMAIL', 'admin@local')
            flash('Signed in.', 'success')
            return redirect(url_for('admin_dashboard'))
        if not open_mode:
            flash('Invalid credentials.', 'error')
    return render_template('admin_login.html', open_mode=open_mode)


@app.route('/admin')
def admin_redirect():
    return redirect(url_for('admin_entry'))


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Session closed.', 'success')
    return redirect(url_for('login'))


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    stats = {
        'users': query_one('SELECT COUNT(*) c FROM users')['c'],
        'contacts': query_one('SELECT COUNT(*) c FROM contacts')['c'],
        'projects': query_one('SELECT COUNT(*) c FROM projects')['c'],
        'vault': query_one('SELECT COUNT(*) c FROM vault_files')['c'],
        'messages': query_one('SELECT COUNT(*) c FROM chat_messages')['c'],
    }
    recent_users = query_all('SELECT * FROM users ORDER BY id DESC LIMIT 8')
    return render_template('admin_dashboard.html', stats=stats, recent_users=recent_users)


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
        set_setting('site_name', request.form.get('site_name', '').strip() or 'Toror Technology and Innovations Ltd')
        set_setting('developer_name', request.form.get('developer_name', '').strip())
        set_setting('tagline', request.form.get('tagline', '').strip())
        set_setting('primary_email', request.form.get('primary_email', '').strip())
        set_setting('hero_text', request.form.get('hero_text', '').strip())
        set_setting('theme_mode', request.form.get('theme_mode', 'light'))
        set_setting('accent_color', request.form.get('accent_color', '#2563eb'))
        flash('Settings updated.', 'success')
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
                    'INSERT INTO contacts(name,email,phone,notes,created_at) VALUES (?,?,?,?,?)',
                    (parts[0], parts[1], parts[2] if len(parts) > 2 else '', parts[3] if len(parts) > 3 else '', now_iso()),
                )
                imported += 1
            flash(f'Imported {imported} contacts.', 'success')
            return redirect(url_for('admin_contacts'))
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        notes = request.form.get('notes', '').strip()
        if name:
            execute(
                'INSERT INTO contacts(name,email,phone,notes,created_at) VALUES (?,?,?,?,?)',
                (name, email, phone, notes, now_iso()),
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
        execute('UPDATE contacts SET name=?, email=?, phone=?, notes=? WHERE id=?', (
            request.form.get('name', '').strip(),
            request.form.get('email', '').strip(),
            request.form.get('phone', '').strip(),
            request.form.get('notes', '').strip(),
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


@app.route('/api/version')
def version():
    return jsonify({'site_name': get_setting('site_name'), 'generated_at': now_iso()})


@app.route('/sw.js')
def service_worker():
    return send_from_directory(BASE_DIR / 'static' / 'js', 'sw.js', mimetype='application/javascript')


@app.route('/manifest.webmanifest')
def manifest():
    return jsonify({
        'name': get_setting('site_name'),
        'short_name': 'Toror Tech',
        'start_url': '/register',
        'display': 'standalone',
        'background_color': '#ffffff',
        'theme_color': get_setting('accent_color', '#2563eb'),
        'icons': [
            {'src': '/static/default-logo.svg', 'sizes': '192x192', 'type': 'image/svg+xml'}
        ]
    })


init_db()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), debug=True)
