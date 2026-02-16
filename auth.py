import os, json, time
import requests
from urllib.parse import urlencode
from flask import Blueprint, redirect, request, url_for, session, render_template, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

bp = Blueprint('auth', __name__)

# Config via env
OIDC_AUTH_URL     = os.getenv('OIDC_AUTH_URL')
OIDC_TOKEN_URL    = os.getenv('OIDC_TOKEN_URL')
OIDC_USERINFO_URL = os.getenv('OIDC_USERINFO_URL')
OIDC_CLIENT_ID    = os.getenv('OIDC_CLIENT_ID')
OIDC_CLIENT_SECRET= os.getenv('OIDC_CLIENT_SECRET')
OIDC_REDIRECT_URI = os.getenv('OIDC_REDIRECT_URI')
OIDC_SCOPE        = os.getenv('OIDC_SCOPE','openid profile email')
OIDC_VERIFY_TLS   = os.getenv('OIDC_VERIFY_TLS','true').lower()=='true'
SSO_ENABLED       = os.getenv('SSO_ENABLED','true').lower()=='true'

# Simple User wrapper for Flask-Login (real data comes from SQLite)
class SimpleUser(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.username = row['username']
        self.email = row['email']
        self.display_name = row['display_name'] or row['username']
        self.role = row['role']
        self.active = row['active']

    def is_active(self):
        return bool(self.active)

# Helpers injected by app at init time
get_db = None

# ---------- Routes ----------
@bp.route('/login')
def login():
    if not SSO_ENABLED:
        flash('SSO désactivé côté serveur (SSO_ENABLED=false)','error')
        return redirect(url_for('admin_login'))
    if not (OIDC_AUTH_URL and OIDC_CLIENT_ID and OIDC_REDIRECT_URI):
        flash('SSO non configuré (variables OIDC_*)','error')
        return redirect(url_for('admin_login'))
    state = str(int(time.time()))
    session['oidc_state']=state
    params = {
        'response_type':'code',
        'client_id': OIDC_CLIENT_ID,
        'redirect_uri': OIDC_REDIRECT_URI,
        'scope': OIDC_SCOPE,
        'state': state
    }
    return redirect(f"{OIDC_AUTH_URL}?{urlencode(params)}")

@bp.route('/callback')
def callback():
    if request.args.get('error'):
        flash(f"SSO erreur: {request.args.get('error_description') or request.args.get('error')}", 'error')
        return redirect(url_for('index'))
    code = request.args.get('code')
    state = request.args.get('state')
    if not code or state != session.get('oidc_state'):
        flash('SSO: code/state invalide','error')
        return redirect(url_for('index'))
    # Exchange code for tokens
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': OIDC_REDIRECT_URI,
        'client_id': OIDC_CLIENT_ID,
        'client_secret': OIDC_CLIENT_SECRET
    }
    try:
        tok = requests.post(OIDC_TOKEN_URL, data=data, timeout=10, verify=OIDC_VERIFY_TLS)
        tok.raise_for_status()
        tokens = tok.json()
    except Exception as e:
        flash(f'SSO: échange token échoué: {e}','error')
        return redirect(url_for('index'))
    # Fetch userinfo
    try:
        headers={'Authorization': f"Bearer {tokens.get('access_token')}"}
        ui = requests.get(OIDC_USERINFO_URL, headers=headers, timeout=10, verify=OIDC_VERIFY_TLS)
        ui.raise_for_status()
        info = ui.json()
    except Exception as e:
        flash(f'SSO: userinfo échoué: {e}','error')
        return redirect(url_for('index'))

    # Map user
    email = info.get('email') or ''
    sub   = info.get('sub') or ''
    name  = info.get('name') or info.get('preferred_username') or email.split('@')[0]

    db=get_db()
    row=db.execute('SELECT * FROM user WHERE sso_subject=? OR email=?', (sub, email)).fetchone()
    if row is None:
        # create basic user role
        db.execute('INSERT INTO user(username,email,display_name,role,active,sso_subject) VALUES (?,?,?,?,?,?)',
                   (email or name, email, name, 'user', 1, sub))
        db.commit()
        row=db.execute('SELECT * FROM user WHERE sso_subject=? OR email=?', (sub, email)).fetchone()
    # update last_login
    db.execute("UPDATE user SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (row['id'],))
    db.commit()
    user=SimpleUser(row)
    login_user(user, remember=True)
    flash(f"Connecté en SSO comme {user.display_name}", 'ok')
    return redirect(url_for('index'))

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Déconnecté','ok')
    return redirect(url_for('index'))

@bp.route('/admin/login', methods=['GET','POST'])
def admin_login():
    # Local admin-only login (form)
    from flask import current_app
    db = get_db()
    if request.method=='POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        row=db.execute("SELECT * FROM user WHERE username=? AND role='admin' AND active=1", (username,)).fetchone()
        if row and row['password_hash'] and check_password_hash(row['password_hash'], password):
            user=SimpleUser(row)
            login_user(user, remember=False)
            flash('Connecté en administrateur','ok')
            return redirect(url_for('admin_users'))
        flash('Identifiants invalides','error')
    return render_template('admin_login.html')
