import os
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, g, abort, send_from_directory, flash
from flask_login import LoginManager, login_required, current_user, login_user, logout_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from mir_client import MiRClient
from processus import Item, Location, can_move, choose_slot

# --- ROLES ACCEPTÉS DANS LE SYSTÈME ----------------------------------
ROLES = [
    "admin",
    "manager",
    "douane",
    "photo",
    "inspection",
    "rac",
    "emballage",
    "expedition",
    "user"
]

# Mapping : route → rôles autorisés
ROLE_PERMISSIONS = {
    "index": ROLES,  # tout le monde
    "items": ["admin", "user"],
    "manager_dashboard": ["admin", "manager"],

    # Flux principal
    "work_douane": ["admin", "douane"],
    "work_photo": ["admin", "photo"],
    "work_inspection": ["admin", "inspection"],
    "work_rac": ["admin", "rac"],
    "work_emballage": ["admin", "emballage"],
    "work_expe": ["admin", "expedition"],

    # Admin
    "admin_users": ["admin"],

    # Archives
    "archives": ["admin"],
    "archives_export": ["admin"],

    # Robot MiR
    "robot_status": ["admin"],
    "robot_mission": ["admin", "photo", "inspection", "emballage"]
}


BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DATA_DIR=os.path.join(BASE_DIR,'data')
UPLOAD_DIR=os.path.join(BASE_DIR,'uploads')
os.makedirs(DATA_DIR,exist_ok=True); os.makedirs(UPLOAD_DIR,exist_ok=True)
DATABASE=os.environ.get('DATABASE',os.path.join(DATA_DIR,'stock.db'))
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE','false').lower()=='true'
ALLOWED_EXTENSIONS={'png','jpg','jpeg','gif','webp'}

def allowed_file(filename:str)->bool:
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

class SimpleUser(UserMixin):
    def __init__(self,row):
        self.id=row['id']; self.username=row['username']; self.email=row['email']
        self.display_name=row['display_name'] or row['username']; self.role=row['role']; self.active=row['active']
    def is_active(self): return bool(self.active)

def create_app():
    app=Flask(__name__); app.config['UPLOAD_FOLDER']=UPLOAD_DIR
    app.jinja_env.globals['ROLES'] = ROLES
    app.secret_key=os.environ.get('SECRET_KEY','dev-secret'); app.config['SESSION_COOKIE_SECURE']=SESSION_COOKIE_SECURE

    def get_db():
        if 'db' not in g:
            g.db=sqlite3.connect(DATABASE,detect_types=sqlite3.PARSE_DECLTYPES); g.db.row_factory=sqlite3.Row
        return g.db
    @app.teardown_appcontext
    def close_db(exc):
        db=g.pop('db',None)
        if db is not None: db.close()
    app.get_db=get_db

    login_manager=LoginManager(app); login_manager.login_view='login'
    @login_manager.user_loader
    def load_user(user_id):
        row=get_db().execute('SELECT * FROM user WHERE id=?',(user_id,)).fetchone(); return SimpleUser(row) if row else None

    @app.route('/healthz')
    def healthz(): return {'ok':True},200
    @app.route('/readyz')
    def readyz():
        try:
            get_db().execute('SELECT 1'); return {'ready':True},200
        except Exception as e:
            return {'ready':False,'error':str(e)},500

# --- Init DB (schema + migrations + seed + normalisation) ---------------
    def init_db():

        db = get_db()
        db.execute('PRAGMA foreign_keys=ON;')

        # 0) Statuts officiels du workflow strict  -----------------------------
        statuses = (
            'Attente douane', 'Attente photo', 'Poste photo',
            'Attente inspection', 'Attente RAC',
            'Prison', 'Attente emballage', 'Emballage',
            'Attente expédition client', 'Attente expédition ST', 'Attente départ T2',
            'STOCK', 'NOGO', 'Supprimé'
        )
        status_check = ",".join([f"'{s}'" for s in statuses])

        # 1) Tables principales si absentes  -----------------------------------
        db.execute("""
        CREATE TABLE IF NOT EXISTS location(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('SOL','ETAGERE','POSTE')),
            capacity INTEGER,
            size TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """)
        db.execute(f"""
        CREATE TABLE IF NOT EXISTS item(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            description TEXT,
            photo_path TEXT,
            size TEXT CHECK(size IN ('GRAND','PETIT') OR size IS NULL),
            status TEXT NOT NULL CHECK(status IN ({status_check})),
            active INTEGER NOT NULL DEFAULT 1,
            st_repair INTEGER NOT NULL DEFAULT 0,
            repair_snpa INTEGER NOT NULL DEFAULT 0,
            location_id INTEGER,
            avis_no TEXT,
            order_no TEXT,
            bl_no TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(location_id) REFERENCES location(id) ON DELETE SET NULL
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS movement(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            from_location_id INTEGER,
            to_location_id INTEGER,
            action TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user TEXT,
            FOREIGN KEY(item_id) REFERENCES item(id) ON DELETE CASCADE
        );
        """)
        db.commit()

        # 2) Colonnes manquantes AVANT toute migration/UPDATE ------------------
        def col_exists(table, col):
            r = db.execute(f"PRAGMA table_info({table})").fetchall()
            return any(x["name"] == col for x in r)

        if not col_exists('location', 'size'):
            db.execute("ALTER TABLE location ADD COLUMN size TEXT")
        if not col_exists('location', 'active'):
            db.execute("ALTER TABLE location ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

        if not col_exists('item', 'active'):
            db.execute("ALTER TABLE item ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if not col_exists('item', 'st_repair'):
            db.execute("ALTER TABLE item ADD COLUMN st_repair INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'repair_snpa'):
            db.execute("ALTER TABLE item ADD COLUMN repair_snpa INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # 3) MIGRATION FORCÉE (détection large de l'ancien CHECK) --------------
    
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='item'").fetchone()
        needs_migration = False
        if row:
            sql = row['sql'] or ""
            old_keywords = ["RECU", "PHOTO", "INSPECTION", "EMBALLAGE", "STOCK"]
            if all(k in sql for k in old_keywords):
                needs_migration = True

        if needs_migration:
            print("[MIGRATION] Ancien schéma détecté → migration forcée")
            db.execute("PRAGMA foreign_keys=OFF;")
            db.execute("BEGIN;")
            db.execute("ALTER TABLE item RENAME TO item_old;")

        # Nouvelle table 'item' avec CHECK étendu
        db.execute(f"""
        CREATE TABLE item(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            description TEXT,
            photo_path TEXT,
            size TEXT CHECK(size IN ('GRAND','PETIT') OR size IS NULL),
            status TEXT NOT NULL CHECK(status IN ({status_check})),
            active INTEGER NOT NULL DEFAULT 1,
            st_repair INTEGER NOT NULL DEFAULT 0,
            repair_snpa INTEGER NOT NULL DEFAULT 0,
            location_id INTEGER,
            avis_no TEXT,
            order_no TEXT,
            bl_no TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(location_id) REFERENCES location(id) ON DELETE SET NULL
        );
        """)

        # Migration des données + mapping anciens statuts
        db.execute("""
        INSERT INTO item(
            id, sku, description, photo_path, size,
            status, active, st_repair, repair_snpa,
            location_id, avis_no, order_no, bl_no,
            created_at, updated_at
        )
        SELECT
            id,
            sku,
            description,
            photo_path,
            size,
            CASE status
                WHEN 'RECU'        THEN 'Attente photo'
                WHEN 'PHOTO'       THEN 'Poste photo'
                WHEN 'INSPECTION'  THEN 'Attente inspection'
                WHEN 'EMBALLAGE'   THEN 'Attente emballage'
                WHEN 'STOCK'       THEN 'STOCK'
                ELSE 'Attente photo'
            END,
            COALESCE(active,1),
            COALESCE(st_repair,0),
            COALESCE(repair_snpa,0),
            location_id,
            avis_no,
            order_no,
            bl_no,
            created_at,
            updated_at
        FROM item_old;
        """)

        db.execute("DROP TABLE item_old;")
        db.execute("COMMIT;")
        db.execute("PRAGMA foreign_keys=ON;")
        db.commit()
        print("[MIGRATION] OK — Nouveau schéma appliqué.")

    # 4) Seed emplacements si vide ------------------------------------------
        c = db.execute("SELECT COUNT(*) AS c FROM location").fetchone()['c']
        if c == 0:
            # SOL grands
            for code in ['S-A1','S-A2','S-A3','S-A4','S-B1','S-B2','S-B3','S-B4']:
                db.execute(
                    "INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                    (code, f"Grand Chariot {code}", 'SOL', 1, 'GRAND')
                )
            # SOL petits
            for row_code in ['C','D','E','F','G','H']:
                for i in range(1,7):
                    code = f"S-{row_code}{i}"
                    db.execute(
                        "INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                        (code, f"Petit Chariot {code}", 'SOL', 1, 'PETIT')
                    )
            # POSTES
            for code, name in [
                ('POSTE-PHOTO','Poste Photo'),
                ('POSTE-INSPECTION','Poste Inspection'),
                ('POSTE-EMBALLAGE','Poste Emballage'),
            ]:
                db.execute(
                    "INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                    (code, name, 'POSTE', 1, None)
                )
            # ÉTAGÈRES
            for e in [1,2,3]:
                for s in ['A','B','C','D']:
                    code = f"ETAGERE-{e}-{s}"
                    db.execute(
                        "INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                        (code, f"Étagère {e} plateau {s}", 'ETAGERE', None, None)
                    )
            db.commit()

        # 5) Normalisation des noms (après présence de 'size') -------------------
        for e in (1,2,3):
            for s in ("A","B","C","D"):
                code = f"ETAGERE-{e}-{s}"
                name = f"Étagère {e} (Encours) – Plateau {s}" if e in (1,2) else f"Étagère 3 (NOGO) – Plateau {s}"
                db.execute("UPDATE location SET name=? WHERE code=? AND kind='ETAGERE'", (name, code))

        db.execute("UPDATE location SET name='Grand Chariot '||code WHERE kind='SOL' AND size='GRAND'")
        db.execute("UPDATE location SET name='Petit Chariot '||code WHERE kind='SOL' AND size='PETIT'")
        db.commit()

        # 6) Table user + seed admin --------------------------------------------
        db.execute("""
            CREATE TABLE IF NOT EXISTS user(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            display_name TEXT,
            role TEXT NOT NULL CHECK(role IN (
                'user','admin','douane','photo','inspection','rac','emballage','expedition'
            )) DEFAULT 'admin',
            active INTEGER NOT NULL DEFAULT 1,
            password_hash TEXT,
            last_login_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        admin = db.execute(
            "SELECT * FROM user WHERE role='admin' LIMIT 1"
        ).fetchone()

        if admin is None:
            username = os.getenv('ADMIN_USERNAME','admin')
            pwd = os.getenv('ADMIN_PASSWORD') or 'ChangeMe!'
            db.execute(
                "INSERT INTO user(username,display_name,role,active,password_hash) VALUES (?,?,?,?,?)",
                (username, 'Administrateur', 'admin', 1, generate_password_hash(pwd))
            )
            db.commit()
            print("[WARN] admin/ChangeMe! créé automatiquement")


    # helpers basic
    def item_by_id(item_id): return get_db().execute('SELECT * FROM item WHERE id=?',(item_id,)).fetchone()
    def location_by_id(loc_id): return get_db().execute('SELECT * FROM location WHERE id=?',(loc_id,)).fetchone()
    def free_sol_slots():
        return get_db().execute("""
          SELECT l.* FROM location l
          LEFT JOIN item it ON it.location_id=l.id AND it.active=1
          WHERE l.kind='SOL' AND l.capacity=1 AND l.active=1
          GROUP BY l.id HAVING COUNT(it.id)=0
          ORDER BY l.code
        """).fetchall()

    def _movement(item_id:int, action:str, user:str=None, from_id=None, to_id=None):
        db=get_db(); db.execute("""
            INSERT INTO movement(item_id, from_location_id, to_location_id, action, user)
            VALUES (?,?,?,?,?)
        """, (item_id, from_id, to_id, action, user or getattr(current_user,'username',None))); db.commit()
    def log_delete(item_id, user=None):
        db=get_db(); it=item_by_id(item_id)
        if not it: return False
        db.execute("UPDATE item SET status='Supprimé', active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (item_id,))
        _movement(item_id,'DELETE_LOGICAL', user=user, from_id=it['location_id'], to_id=None)
        return True
    def move_item(item_id,to_location_id,action='MOVE',user=None):
        db=get_db(); it=item_by_id(item_id); dest=location_by_id(to_location_id)
        if not it or not dest: abort(400)
        if it['active']!=1: abort(400)

        # capacity check omitted (can_move available in processus if needed)
        _movement(item_id, action, user=user, from_id=it['location_id'], to_id=to_location_id)
        db.execute('UPDATE item SET location_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',(to_location_id,item_id)); db.commit(); return True

    def _place_on_shelf(item_id:int, level:int, plate:str):
        code=f"ETAGERE-{level}-{plate.upper()}"; dest=get_db().execute("SELECT * FROM location WHERE code=? AND active=1",(code,)).fetchone()
        if not dest: raise ValueError('Étagère/plateau introuvable.')
        move_item(item_id, dest['id'], action='PLACE_SHELF', user=getattr(current_user,'username',None))
    def _place_on_sol_amr(item_id:int, size:str):
        size=size.upper(); slots=free_sol_slots(); picked=None
        for s in slots:
            if s['kind']=='SOL' and s['size']==size: picked=s; break
        if not picked: raise ValueError(f'Aucun SOL {size} disponible.')
        move_item(item_id, picked['id'], action='PUT_STOCK_AMR', user=getattr(current_user,'username',None))
    def _compute_next_after_photo(it)->str:
        return 'Attente inspection' if (it['st_repair']==1 or it['repair_snpa']==1) else 'Attente RAC'
    def _set_status(item_id:int, new_status:str):
        db=get_db(); db.execute('UPDATE item SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',(new_status,item_id)); db.commit()
    def _ensure_active(it):
        if not it or it['active']!=1: abort(404)

# ------ Décorateur role requirement -----#

    def role_required(*roles):
        """RBAC strict basé sur la liste des rôles autorisés."""
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if not current_user.is_authenticated:
                    abort(403)

                role = getattr(current_user, "role", None)
                if role not in roles:
                    flash("Accès refusé : rôle non autorisé.", "error")
                    return abort(403)

                return fn(*args, **kwargs)
            return wrapper
        return decorator

    # -------- Auth --------
    @app.route('/login', methods=['GET','POST'])
    def login():
        if request.method=='POST':
            username=(request.form.get('username') or '').strip(); password=(request.form.get('password') or '')
            row=get_db().execute("SELECT * FROM user WHERE username=? AND active=1",(username,)).fetchone()
            if row and row['password_hash'] and check_password_hash(row['password_hash'], password):
                get_db().execute("UPDATE user SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",(row['id'],)); get_db().commit()
                login_user(SimpleUser(row), remember=False); flash('Connecté','ok'); return redirect(url_for('index'))
            flash('Identifiants invalides','error')
        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout(): logout_user(); flash('Déconnecté','ok'); return redirect(url_for('login'))

    # -------- Index / Items / Item detail / Locations --------
    @app.route('/')
    @login_required
    def index():
        db=get_db(); kg={'GRAND':{},'PETIT':{}}
        for size in ['GRAND','PETIT']:
            total=db.execute("SELECT COUNT(*) AS c FROM location WHERE kind='SOL' AND size=?",(size,)).fetchone()['c']
            occ=db.execute("""
                SELECT COUNT(*) AS c FROM item i JOIN location l ON i.location_id=l.id
                 WHERE i.active=1 AND l.kind='SOL' AND l.size=?
            """,(size,)).fetchone()['c']
            kg[size]={'total':total,'occupied':occ,'free':total-occ}
        statuses=db.execute('SELECT status, COUNT(*) AS c FROM item WHERE active=1 GROUP BY status').fetchall()
        return render_template('index.html',kg=kg,statuses=statuses)

    @app.route('/items', methods=['GET','POST'])
    @login_required
    def items():
        db=get_db()
        if request.method=='POST':
            sku=(request.form.get('sku') or '').strip(); size=(request.form.get('size') or 'PETIT').upper()
            sous_douane=(request.form.get('sous_douane')=='on'); st_repair=1 if (request.form.get('st_repair')=='on') else 0
            repair_snpa=1 if (request.form.get('repair_snpa')=='on') else 0
            if size not in ('GRAND','PETIT'):
                flash('Taille requise (GRAND/PETIT)','error'); return redirect(url_for('items'))
            if not sku:
                row=db.execute("SELECT sku FROM item WHERE sku LIKE 'SKU-%' ORDER BY CAST(SUBSTR(sku,5) AS INTEGER) DESC LIMIT 1").fetchone()
                if row is None: sku='SKU-00001'
                else:
                    last=int(row['sku'].split('-')[1]); sku=f'SKU-{last+1:05d}'
            desc=(request.form.get('description') or '').strip(); avis=(request.form.get('avis_no') or '').strip() or None
            od=(request.form.get('order_no') or '').strip() or None; bl=(request.form.get('bl_no') or '').strip() or None
            status='Attente douane' if sous_douane else 'Attente photo'
            db.execute("""
                INSERT INTO item(
                    sku, description, size,
                    avis_no, order_no, bl_no,
                    status, st_repair, repair_snpa,
                    active, location_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (sku, desc, size, avis, od, bl, status, st_repair, repair_snpa, 1, None))
            new_id=db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']; db.commit()
            flash(f'Article créé {sku} — statut: {status}','ok'); return redirect(url_for('item_detail', item_id=new_id))
        q=(request.args.get('q') or '').strip(); base_sql="SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.active=1"; params=[]
        if q:
            base_sql += " AND (i.sku LIKE ? OR i.description LIKE ? OR i.avis_no LIKE ? OR i.order_no LIKE ? OR i.bl_no LIKE ?)"; params.extend([f'%{q}%']*5)
        base_sql += " ORDER BY i.created_at DESC LIMIT 200"; rows=db.execute(base_sql, tuple(params)).fetchall()
        return render_template('items.html', items=rows, q=q)

    @app.route('/items/<int:item_id>')
    @login_required
    def item_detail(item_id):
        db=get_db(); it=item_by_id(item_id)
        if not it: abort(404)
        loc=location_by_id(it['location_id']) if it['location_id'] else None
        moves=db.execute("""
          SELECT m.*, lf.code AS from_code, lt.code AS to_code FROM movement m
          LEFT JOIN location lf ON m.from_location_id=lf.id
          LEFT JOIN location lt ON m.to_location_id=lt.id
          WHERE m.item_id=? ORDER BY m.created_at DESC
        """,(item_id,)).fetchall()
        sol_free=free_sol_slots(); return render_template('item_detail.html', it=it, loc=loc, moves=moves, sol_free=sol_free)

    @app.route('/uploads/<path:filename>')
    @login_required
    def uploads(filename):
        return send_from_directory(UPLOAD_DIR, filename)

    @app.route('/locations')
    @login_required
    def locations():
        db=get_db(); kind=request.args.get('kind'); show_all=(request.args.get('show')=='all')
        base="""
          SELECT l*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id AND i.active=1) AS occ
            FROM location l
        """.replace('l*','l.*')
        where=[]; params=[]
        if kind in ('SOL','ETAGERE','POSTE'):
            where.append('l.kind = ?'); params.append(kind)
        if not show_all:
            where.append('l.active = 1')
        sql=base
        if where: sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY l.kind, l.code'
        locs=db.execute(sql, tuple(params)).fetchall(); return render_template('locations.html', locations=locs, kind=kind, show_all=show_all)

    # --- Admin users (simple) ---
    @app.route('/admin/users')
    @login_required
    def admin_users():
        if getattr(current_user,'role',None)!='admin': flash('Accès administrateur requis','error'); return redirect(url_for('index'))
        rows=get_db().execute('SELECT id,username,email,display_name,role,active,last_login_at,created_at FROM user ORDER BY role DESC, username').fetchall()
        return render_template('admin_users.html', users=rows)

    @app.route('/admin/users/create', methods=['POST'])
    @login_required
    def admin_users_create():
        if getattr(current_user,'role',None)!='admin': flash('Accès administrateur requis','error'); return redirect(url_for('index'))
        username=(request.form.get('username') or '').strip(); display=(request.form.get('display_name') or '').strip() or username
        role=(request.form.get('role') or 'user')
        if not username: flash('username requis','error'); return redirect(url_for('admin_users'))
        db=get_db()
        try:
            db.execute('INSERT INTO user(username,display_name,role,active) VALUES (?,?,?,1)',(username,display,role)); db.commit(); flash('Utilisateur créé','ok')
        except Exception as e:
            flash(f'Echec création: {e}','error')
        return redirect(url_for('admin_users'))

    @app.route('/admin/users/toggle/<int:uid>', methods=['POST'])
    @login_required
    def admin_users_toggle(uid):
        if getattr(current_user,'role',None)!='admin': flash('Accès administrateur requis','error'); return redirect(url_for('index'))
        db=get_db(); db.execute('UPDATE user SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(uid,)); db.commit(); flash('Statut utilisateur mis à jour','ok')
        return redirect(url_for('admin_users'))

    @app.route('/admin/users/password/<int:uid>', methods=['POST'])
    @login_required
    def admin_users_password(uid):
        if getattr(current_user,'role',None)!='admin': flash('Accès administrateur requis','error'); return redirect(url_for('index'))
        pwd=(request.form.get('password') or '').strip()
        if len(pwd)<8: flash('Mot de passe trop court (>=8)','error'); return redirect(url_for('admin_users'))
        db=get_db(); db.execute('UPDATE user SET password_hash=? WHERE id=?',(generate_password_hash(pwd),uid)); db.commit(); flash('Mot de passe mis à jour','ok')
        return redirect(url_for('admin_users'))

    @app.route('/admin/users/role/<int:uid>', methods=['POST'])
    @login_required
    @role_required('admin')
    def admin_users_role(uid):
        role = request.form.get("role")
        if role not in ROLES:
            flash("Rôle invalide.", "error")
            return redirect(url_for('admin_users'))

        db = get_db()
        db.execute("UPDATE user SET role=? WHERE id=?", (role, uid))
        db.commit()

        flash(f"Rôle utilisateur mis à jour : {role}", "ok")
        return redirect(url_for('admin_users'))

# ============================================================
# SUPPRESSION D’ITEMS (ADMIN UNIQUEMENT)
# ============================================================

    @app.route('/items/<int:item_id>/delete', methods=['POST'])
    @login_required
    @role_required('admin')
    def item_delete(item_id):
        """
        Suppression logique: envoie l'item dans les archives (active=0).
        Réversible : on peut plus tard prévoir une 'restauration'.
        """
        db = get_db()
# Sanity check: existe et actif ?
        row = db.execute("SELECT id, active FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            flash("Item introuvable.", "error")
            return redirect(url_for('items'))
        if row["active"] == 0:
            flash("Cet item est déjà archivé.", "error")
            return redirect(url_for('items'))

 # Soft delete
        db.execute("UPDATE item SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (item_id,))
 # Journalisation , si table movement
        try:
            db.execute("""
                INSERT INTO movement(item_id, from_location_id, to_location_id, action, user)
                VALUES (?,?,?,?,?)
            """, (item_id, None, None, "SOFT_DELETE", getattr(current_user, "username", None)))
        except Exception:
            pass
        db.commit()

        flash("Item archivé (suppression logique).", "ok")
        return redirect(url_for('items'))


    @app.route('/items/<int:item_id>/purge', methods=['POST'])
    @login_required
    @role_required('admin')
    def item_purge(item_id):
        """
        Suppression définitive: hard delete en base (irréversible).
        Utiliser avec parcimonie (GDPR/traçabilité...).
        """
        db = get_db()
        row = db.execute("SELECT id FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            flash("Item introuvable.", "error")
            return redirect(url_for('items'))

# supprimer ses mouvements associés si contrainte FK
# db.execute("DELETE FROM movement WHERE item_id=?", (item_id,))

        db.execute("DELETE FROM item WHERE id=?", (item_id,))
        db.commit()

        flash("Item supprimé définitivement (purge).", "ok")
        return redirect(url_for('items'))

    #---------------------Manager------------------------
    @app.route("/manager/dashboard")
    @login_required
    @role_required("admin", "manager")
    def manager_dashboard():
        db = get_db()

        encours = {}

        encours['attente_douane'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_DOUANE' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_photo'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_PHOTO' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_inspection'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_INSPECTION' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_rac'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_RAC' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_emballage'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_EMBALLAGE' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_client'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_EXPE_CLIENT' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_st'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_EXPE_ST' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()

        encours['attente_t2'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'ATTENTE_EXPE_T2' AND active = 1
                ORDER BY created_at ASC
        """).fetchall()
        encours['nogo'] = db.execute("""
                SELECT * FROM item
                WHERE status = 'NOGO' AND active = 1
                ORDER BY updated_at DESC
        """).fetchall()

        return render_template("manager_dashboard.html", encours=encours)

    # -------------------- WORKFLOWS --------------------
    # DOUANE
    @app.route('/work/douane')
    @login_required
    @role_required('douane' , 'admin')
    def work_douane():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
              FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente douane'
             ORDER BY i.created_at ASC
        """).fetchall()
        return render_template('work_douane.html', items=rows)

    @app.route('/items/<int:item_id>/douane_out', methods=['POST'])
    @login_required
    @role_required('douane' , 'admin')
    def douane_out(item_id):
        db=get_db(); it=item_by_id(item_id)
        if not it: abort(404)
        if it['status']!='Attente douane':
            flash("L'item n'est pas en attente douane.", 'error'); return redirect(url_for('work_douane'))
        db.execute("UPDATE item SET status='Attente photo', updated_at=CURRENT_TIMESTAMP WHERE id=?",(item_id,))
        db.execute("""
            INSERT INTO movement(item_id, from_location_id, to_location_id, action, user)
            VALUES (?,?,?,?,?)
        """, (item_id, it['location_id'], None, 'DOUANE_OUT', getattr(current_user,'username',None)))
        db.commit(); flash('Sortie de douane → Attente photo.','ok'); return redirect(url_for('work_douane'))

    # PHOTO
    @app.route('/work/photo')
    @login_required
    @role_required('photo', 'admin')
    def work_photo():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
              FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente photo'
             ORDER BY i.created_at ASC
        """
        ).fetchall()
        choose=request.args.get('choose'); item_id=request.args.get('item_id', type=int)
        return render_template('work_photo.html', items=rows, choose=choose, modal_item_id=item_id)

    @app.route('/work/photo/<int:item_id>/ok', methods=['POST'])
    @login_required
    @role_required('photo' , 'admin')
    def photo_ok(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo':
            flash('Statut invalide pour Photo OK','error'); return redirect(url_for('work_photo'))
        return redirect(url_for('work_photo', choose='storage', item_id=item_id))

    @app.route('/work/photo/place_shelf', methods=['POST'])
    @login_required
    @role_required('photo' , 'admin')
    def photo_place_shelf():
        item_id=int(request.form.get('item_id')); level=int(request.form.get('level')); plate=(request.form.get('plate') or 'A').upper()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo': flash('Statut invalide','error'); return redirect(url_for('work_photo'))
        try:
            _place_on_shelf(item_id, level, plate); _movement(item_id,'PHOTO_DONE'); _set_status(item_id, _compute_next_after_photo(it))
            flash('Photo OK + Placement étagère effectué','ok')
        except ValueError as e:
            flash(str(e),'error')
        return redirect(url_for('work_photo'))

    @app.route('/work/photo/place_amr', methods=['POST'])
    @login_required
    @role_required('photo' , 'admin')
    def photo_place_amr():
        item_id=int(request.form.get('item_id')); size=(request.form.get('amr_size') or 'PETIT').upper()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo': flash('Statut invalide','error'); return redirect(url_for('work_photo'))
        try:
            _place_on_sol_amr(item_id, size); _movement(item_id,'PHOTO_DONE'); _set_status(item_id, _compute_next_after_photo(it))
            flash('Photo OK + Placement SOL (AMR) enregistré','ok')
        except ValueError as e:
            flash(str(e),'error')
        return redirect(url_for('work_photo'))

    # INSPECTION
    @app.route('/work/inspection')
    @login_required
    @role_required('inspection' , 'admin')
    def work_inspection():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
              FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente inspection'
             ORDER BY i.created_at ASC
        """).fetchall()
        ask_pool=(request.args.get('pool')=='ask'); item_id=request.args.get('item_id', type=int)
        return render_template('work_inspection.html', items=rows, ask_pool=ask_pool, modal_item_id=item_id)

    @app.route('/work/inspection/<int:item_id>/nok', methods=['POST'])
    @login_required
    @role_required('inspection' , 'admin')
    def inspection_nok(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente inspection': flash('Statut invalide','error'); return redirect(url_for('work_inspection'))
        _movement(item_id,'INSPECTION_NOK'); _set_status(item_id,'Prison'); flash('Inspection NOK → Prison','ok')
        return redirect(url_for('work_inspection'))

    @app.route('/work/inspection/<int:item_id>/ok', methods=['POST'])
    @login_required
    @role_required('inspection' , 'admin')
    def inspection_ok(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente inspection': flash('Statut invalide','error'); return redirect(url_for('work_inspection'))
        return redirect(url_for('work_inspection', pool='ask', item_id=item_id))

    @app.route('/work/inspection/pool', methods=['POST'])
    @login_required
    @role_required('inspection' , 'admin')
    def inspection_pool_decision():
        item_id=int(request.form.get('item_id')); choice=(request.form.get('choice') or 'non').lower()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente inspection': flash('Statut invalide','error'); return redirect(url_for('work_inspection'))
        _movement(item_id,'INSPECTION_OK')
        if choice=='oui':
            log_delete(item_id, user=getattr(current_user,'username',None)); flash('Inspection OK → Pool → Supprimé','ok')
        else:
            _set_status(item_id,'Attente emballage'); flash('Inspection OK → Attente emballage','ok')
        return redirect(url_for('work_inspection'))

    # RAC
    @app.route('/work/rac')
    @login_required
    @role_required('rac' , 'admin')
    def work_rac():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
              FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente RAC'
             ORDER BY i.created_at ASC
        """).fetchall()
        return render_template('work_rac.html', items=rows)

    @app.route('/work/rac/<int:item_id>/ok', methods=['POST'])
    @login_required
    @role_required('rac', 'admin')
    def rac_ok(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente RAC': flash('Statut invalide','error'); return redirect(url_for('work_rac'))
        _movement(item_id,'RAC_OK'); _set_status(item_id,'Attente emballage'); flash('RAC OK → Attente emballage','ok')
        return redirect(url_for('work_rac'))

    @app.route('/work/rac/<int:item_id>/nok', methods=['POST'])
    @login_required
    @role_required('rac', 'admin')
    def rac_nok(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente RAC': flash('Statut invalide','error'); return redirect(url_for('work_rac'))
        _movement(item_id,'RAC_NOK'); _set_status(item_id,'Prison'); flash('RAC NOK → Prison','ok')
        return redirect(url_for('work_rac'))

    # EMBALLAGE
    @app.route('/work/emballage')
    @login_required
    @role_required('emballage', 'admin')
    def work_emballage():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
              FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente emballage'
             ORDER BY i.created_at ASC
        """).fetchall()
        ask_route=(request.args.get('route')=='ask'); item_id=request.args.get('item_id', type=int)
        return render_template('work_emballage.html', items=rows, ask_route=ask_route, modal_item_id=item_id)

    @app.route('/work/emballage/<int:item_id>/done', methods=['POST'])
    @login_required
    @role_required('emballage' , 'admin')
    def emballage_done(item_id):
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente emballage': flash('Statut invalide','error'); return redirect(url_for('work_emballage'))
        _movement(item_id,'EMBALLAGE_OK')
        if it['st_repair']==1 or it['repair_snpa']==1:
            _set_status(item_id,'Attente expédition client'); flash('Emballage → Attente expédition client','ok'); return redirect(url_for('work_emballage'))
        return redirect(url_for('work_emballage', route='ask', item_id=item_id))

    @app.route('/work/emballage/route', methods=['POST'])
    @login_required
    @role_required('emballage','admin')
    def emballage_choose_route():
        item_id=int(request.form.get('item_id')); route=(request.form.get('route') or 'ST').upper()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente emballage': flash('Statut invalide','error'); return redirect(url_for('work_emballage'))
        if route=='ST': _set_status(item_id,'Attente expédition ST'); flash('→ Attente expédition ST','ok')
        else: _set_status(item_id,'Attente départ T2'); flash('→ Attente départ T2','ok')
        return redirect(url_for('work_emballage'))

    # EXPÉDITION
    @app.route('/work/expe')
    @login_required
    @role_required('emballage','admin')
    def work_expe():
        db=get_db()
        client=db.execute("""
            SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente expédition client' ORDER BY i.created_at ASC
        """).fetchall()
        st=db.execute("""
            SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente expédition ST' ORDER BY i.created_at ASC
        """).fetchall()
        t2=db.execute("""
            SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id
             WHERE i.active=1 AND i.status='Attente départ T2' ORDER BY i.created_at ASC
        """).fetchall()
        return render_template('work_expe.html', client=client, st=st, t2=t2)

    @app.route('/work/expe/<int:item_id>/ship', methods=['POST'])
    @login_required
    @role_required('emballage', 'admin')
    def expe_ship(item_id):
        kind=(request.form.get('kind') or 'client')
        it=item_by_id(item_id); _ensure_active(it)
        valid={'client':'Attente expédition client','st':'Attente expédition ST','t2':'Attente départ T2'}
        if it['status']!=valid.get(kind): flash('Statut invalide pour expédition','error'); return redirect(url_for('work_expe'))
        _movement(item_id, f'EXPE_{kind.upper()}'); log_delete(item_id, user=getattr(current_user,'username',None))
        flash('Expédition effectuée → Item supprimé (logique)','ok'); return redirect(url_for('work_expe'))

# ===================== ROBOT MiR ===============================
    from mir_client import MiRClient

    @app.route('/robot/status')
    @login_required
    @role_required('admin')
    def robot_status():
        """
        Affiche l'état du robot :
        - DRY-RUN: faux retour (utile sur poste dev / Raspi sans robot)
        - LIVE    : lecture via l'API MiR
        """
        try:
            c = MiRClient()        # lit MIR_* dans l'env
            st = c.status()        # dict
            dry = getattr(c, 'dry', True)
            return render_template('robot_status.html', status=st, dry=dry)
        except Exception as e:
            flash(f"Erreur statut robot: {e}", "error")
            return render_template('robot_status.html', status=None, dry=True), 500


    @app.route('/robot/mission', methods=['POST'])
    @login_required
    @role_required('admin','photo','inspection','emballage')
    def robot_mission():
        """
        Démarre une mission simple côté MiR.
        Form field: target = 'POSTE-PHOTO' | 'POSTE-INSPECTION' | 'POSTE-EMBALLAGE'
        """
        target = (request.form.get('target') or 'POSTE-PHOTO').upper()

        try:
            c = MiRClient()
            missions = c.missions()           # liste {'name','guid'}
            match = next((m for m in missions if m['name'].upper() == target), None)
            if not match:
                flash("Mission inconnue sur MiR.", "error")
                return redirect(url_for('robot_status'))

            res = c.start_mission(match['guid'])   # déclenchement
            dry = getattr(c, 'dry', True)
            msg = "Mission envoyée" + (" (DRY-RUN)" if dry else "")
            flash(msg, "ok")
            return redirect(url_for('robot_status'))

        except Exception as e:
            flash(f"Erreur mission: {e}", "error")
            return redirect(url_for('robot_status'))



    @app.route('/archives')
    @login_required
    @role_required('admin')
    def archives():
        db = get_db()
        q = (request.args.get("q") or "").strip()

        sql = """
            SELECT i.*, l.code AS loc_code
            FROM item i
            LEFT JOIN location l ON i.location_id=l.id
            WHERE i.active = 0
        """
        params = []

        if q:
            sql += " AND (i.sku LIKE ? OR i.description LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])

        sql += " ORDER BY updated_at DESC LIMIT 300"

        rows = db.execute(sql, params).fetchall()

        return render_template("archives.html", items=rows, q=q)

        @app.route('/archives/export')
        @login_required
        @role_required('admin')
        def archives_export():
            import csv, io

            db = get_db()
            rows = db.execute("""
                SELECT id, sku, status, updated_at, avis_no, order_no, bl_no
                FROM item
                WHERE active = 0
                ORDER BY updated_at DESC
            """).fetchall()

            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=';')
            writer.writerow(["id", "sku", "status", "updated_at", "avis_no", "order_no", "bl_no"])

            for r in rows:
                writer.writerow([
                    r["id"], r["sku"], r["status"], r["updated_at"],
                    r["avis_no"], r["order_no"], r["bl_no"]
                ])

            buffer.seek(0)
            return app.response_class(
                buffer.read(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=archives.csv"}
            )

    return app

app=create_app()
if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','5000')), debug=True)
