import os
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, g, abort, send_from_directory, flash, jsonify
from flask_login import LoginManager, login_required, current_user, login_user, logout_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from mir_client import MiRClient
from processus import Item, Location, can_move, choose_slot
from authlib.integrations.flask_client import OAuth
import requests
import easyocr
import numpy as np
from io import BytesIO
from PIL import Image

# Outil OCR singleton pour éviter rechargements lourds à chaque requête
OCR_READER = None

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
    "reception",
    "user"
]

# Mapping : route → rôles autorisés
ROLE_PERMISSIONS = {
    "index": ROLES,  # tout le monde
    "items": ["admin", "user"],
    "manager_dashboard": ["admin", "manager"],

    # Flux principal
    "work_reception" : ["admin", "reception"],
    # "work_reception_check" : ["admin", "reception"],  # Supprimé - réception directe
    "work_induction": ["admin", "induction"],
    "work_depart_atelier": ["admin", "atelier"],
    "work_retour_atelier": ["admin", "atelier"],
    # "workload_reception": ["admin", "reception"],  # Supprimé - réception directe
    "work_douane": ["admin", "douane"],
    "work_photo": ["admin", "photo"],
    "work_inspection": ["admin", "inspection"],
    "work_rac": ["admin", "rac"],
    "work_emballage": ["admin", "emballage"],
    "work_sap": ["admin", "reception"],
    "work_repair": ["admin", "inspection"],
    "work_pool": ["admin", "inspection"],
    "work_prison": ["admin", "inspection"],
    "work_kardex": ["admin", "inspection"],
    "work_input_st": ["admin"],
    "work_expe": ["admin", "expedition"],

    # Workflows & Visualization
    "return_st_workflow": ["admin", "manager"],
    "Global_WIP": ["admin", "manager"],

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

    # OIDC Setup
    oauth = OAuth(app)
    oauth.register(
        name='safran',
        client_id=os.getenv('OIDC_CLIENT_ID'),
        client_secret=os.getenv('OIDC_CLIENT_SECRET'),
        server_metadata_url=os.getenv('OIDC_METADATA_URL', 'https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration'),
        client_kwargs={'scope': 'openid profile email'},
    )

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
            'Attente Douane',
            'Attente Photo',
            'Attente Inspection',
            'Attente Induction',
            'NOGO',
            'Depart Atelier',
            'Attente Emballage',
            'Attente Expedition',
            'Attente Expe Client',
            'Attente Expe ST',
            'Retour Atelier',
            'Prison',
            'Disponible',
            'Exp ST Return',
            'ARCHIVE',
            'STOCK'
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
            active INTEGER NOT NULL DEFAULT 1,
            chariot_status TEXT DEFAULT 'libre'
        );
        """)
        db.execute(f"""
        CREATE TABLE IF NOT EXISTS item(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            pn TEXT,
            description TEXT,
            serial_number TEXT,
            tsn INTEGER,
            csn INTEGER,
            photo_path TEXT,
            size TEXT CHECK(size IN ('GRAND','PETIT','HORS GABARIT') OR size IS NULL),
            status TEXT NOT NULL CHECK(status IN ({status_check})),
            active INTEGER NOT NULL DEFAULT 1,
            st_repair INTEGER NOT NULL DEFAULT 0,
            repair_snpa INTEGER NOT NULL DEFAULT 0,
            hors_gabarit INTEGER NOT NULL DEFAULT 0,
            return_st INTEGER NOT NULL DEFAULT 0,
            int_repair_capa INTEGER NOT NULL DEFAULT 0,
            repair_int INTEGER NOT NULL DEFAULT 0,
            prepa_st INTEGER NOT NULL DEFAULT 0,
            photo_ins INTEGER NOT NULL DEFAULT 0,
            location_id INTEGER,
            avis_no TEXT,
            order_no TEXT,
            bl_no TEXT,
            is_nogo INTEGER NOT NULL DEFAULT 0,
            is_repair_snpa INTEGER NOT NULL DEFAULT 0,
            sap_created INTEGER NOT NULL DEFAULT 0,
            pool_ok INTEGER NOT NULL DEFAULT 0,
            std_exchange INTEGER NOT NULL DEFAULT 0,
            prepa_st_ok INTEGER NOT NULL DEFAULT 0,
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
        if not col_exists('location', 'chariot_status'):
            try:
                db.execute("ALTER TABLE location ADD COLUMN chariot_status TEXT DEFAULT 'libre'")
                db.commit()
                # Initialiser le statut pour les chariots SOL
                db.execute("UPDATE location SET chariot_status='libre' WHERE kind='SOL' AND chariot_status IS NULL")
                db.commit()
            except Exception as e:
                print(f"[MIGRATION] Erreur ajout chariot_status: {e}")
                db.execute("ROLLBACK")
                db.commit()

        if not col_exists('item', 'active'):
            db.execute("ALTER TABLE item ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if not col_exists('item', 'st_repair'):
            db.execute("ALTER TABLE item ADD COLUMN st_repair INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'repair_snpa'):
            db.execute("ALTER TABLE item ADD COLUMN repair_snpa INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'serial_number'):
            db.execute("ALTER TABLE item ADD COLUMN serial_number TEXT")
        if not col_exists('item', 'tsn'):
            db.execute("ALTER TABLE item ADD COLUMN tsn INTEGER")
        if not col_exists('item', 'csn'):
            db.execute("ALTER TABLE item ADD COLUMN csn INTEGER")
        if not col_exists('item', 'pn'):
            db.execute("ALTER TABLE item ADD COLUMN pn TEXT")
        if not col_exists('item', 'is_nogo'):
            db.execute("ALTER TABLE item ADD COLUMN is_nogo INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'is_repair_snpa'):
            db.execute("ALTER TABLE item ADD COLUMN is_repair_snpa INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'sap_created'):
            db.execute("ALTER TABLE item ADD COLUMN sap_created INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'pool_ok'):
            db.execute("ALTER TABLE item ADD COLUMN pool_ok INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'std_exchange'):
            db.execute("ALTER TABLE item ADD COLUMN std_exchange INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'prepa_st_ok'):
            db.execute("ALTER TABLE item ADD COLUMN prepa_st_ok INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'hors_gabarit'):
            db.execute("ALTER TABLE item ADD COLUMN hors_gabarit INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'return_st'):
            db.execute("ALTER TABLE item ADD COLUMN return_st INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'int_repair_capa'):
            db.execute("ALTER TABLE item ADD COLUMN int_repair_capa INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'repair_int'):
            db.execute("ALTER TABLE item ADD COLUMN repair_int INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'prepa_st'):
            db.execute("ALTER TABLE item ADD COLUMN prepa_st INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'photo_ins'):
            db.execute("ALTER TABLE item ADD COLUMN photo_ins INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE item ADD COLUMN induction INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE item ADD COLUMN induction_ok INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE item ADD COLUMN inspection_ok INTEGER NOT NULL DEFAULT 0")
        if not col_exists('item', 'sous_douane'):
            db.execute("ALTER TABLE item ADD COLUMN sous_douane INTEGER NOT NULL DEFAULT 0")
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
                pn TEXT,
                description TEXT,
                serial_number TEXT,
                tsn INTEGER,
                csn INTEGER,
                photo_path TEXT,
                size TEXT CHECK(size IN ('GRAND','PETIT','HORS GABARIT') OR size IS NULL),
                status TEXT NOT NULL CHECK(status IN ({status_check})),
                active INTEGER NOT NULL DEFAULT 1,
                st_repair INTEGER NOT NULL DEFAULT 0,
                repair_snpa INTEGER NOT NULL DEFAULT 0,
                hors_gabarit INTEGER NOT NULL DEFAULT 0,
                location_id INTEGER,
                avis_no TEXT,
                order_no TEXT,
                bl_no TEXT,
                is_nogo INTEGER NOT NULL DEFAULT 0,
                is_repair_snpa INTEGER NOT NULL DEFAULT 0,
                sap_created INTEGER NOT NULL DEFAULT 0,
                pool_ok INTEGER NOT NULL DEFAULT 0,
                std_exchange INTEGER NOT NULL DEFAULT 0,
                prepa_st_ok INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(location_id) REFERENCES location(id) ON DELETE SET NULL
            );
            """)

            # Migration des données + mapping anciens statuts
            db.execute("""
            INSERT INTO item(
                id, sku, pn, description, serial_number, tsn, csn, photo_path, size,
                status, active, st_repair, repair_snpa, hors_gabarit,
                location_id, avis_no, order_no, bl_no,
                created_at, updated_at
            )
            SELECT
                id,
                sku,
                NULL,
                description,
                NULL,
                NULL,
                NULL,
                photo_path,
                size,
                CASE status
                    WHEN 'RECU'        THEN 'Attente Photo'
                    WHEN 'PHOTO'       THEN 'Attente Induction'
                    WHEN 'INSPECTION'  THEN 'Attente Inspection'
                    WHEN 'EMBALLAGE'   THEN 'Attente Emballage'
                    WHEN 'STOCK'       THEN 'STOCK'
                    ELSE 'Attente Photo'
                END,
                COALESCE(active,1),
                COALESCE(st_repair,0),
                COALESCE(repair_snpa,0),
                0,
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
        else:
            print("[MIGRATION] Ancien schéma non détecté : pas de migration item nécessaire.")

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


    # Call init_db to create tables and seed data
    with app.app_context():
        init_db()

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

    def generate_sku():
        db = get_db()
        row = db.execute("SELECT sku FROM item WHERE sku LIKE 'SKU-%' ORDER BY CAST(SUBSTR(sku,5) AS INTEGER) DESC LIMIT 1").fetchone()
        if not row:
            return 'SKU-00001'
        last = int(row['sku'].split('-')[1])
        return f'SKU-{last+1:05d}'

    def fetch_dynamics_info(qr_url):
        if not qr_url or 'dynamics' not in qr_url.lower():
            return None
        # Placeholder : si besoin, on peut faire un appel API Dynamics365 ici avec auth.
        identifier = qr_url.rstrip('/').split('/')[-1]
        return {
            'sku': f'SKU-{identifier[-5:]}' if len(identifier)>=5 else None,
            'pn': None,
            'serial_number': None,
            'tsn': None,
            'csn': None
        }

    def perform_ocr(image_path_or_bytes):
        """Lecture de la plaque d'identification via OCR (easyocr)."""
        global OCR_READER
        try:
            if OCR_READER is None:
                # Chargement une fois au démarrage du serveur ou au premier appel OCR
                OCR_READER = easyocr.Reader(['fr', 'en'], gpu=False)

            if isinstance(image_path_or_bytes, bytes):
                image = Image.open(BytesIO(image_path_or_bytes))
            else:
                image = Image.open(image_path_or_bytes)

            image = image.convert('RGB')
            image_np = np.array(image)
            results = OCR_READER.readtext(image_np, detail=0)
            extracted_text = '\n'.join(results)
            return extracted_text
        except Exception as e:
            # Erreur claire pour debug + sensibilité du reverse proxy
            return f"Erreur OCR: {str(e)}"

    def call_orbitview_api(item_id):
        """Appel à l'API OrbitView pour capturer les photos."""
        try:
            api_key = os.getenv('ORBITVIEW_API_KEY')
            api_url = os.getenv('ORBITVIEW_API_URL', 'https://orbitview.example.com/api/capture')
            
            payload = {
                'item_id': item_id,
                'capture_type': 'equipment_identification',
                'quality': 'high'
            }
            headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
            
            response = requests.post(api_url, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                return {'error': f'Status {response.status_code}: {response.text}'}
        except Exception as e:
            return {'error': f'Erreur API OrbitView: {str(e)}'}

    def verify_item_info(item_data, ocr_text):
        """Vérifier que les infos OCR correspondent aux données de l'item."""
        matches = []
        if item_data.get('sku') and item_data['sku'] in ocr_text:
            matches.append({'field': 'SKU', 'status': 'OK', 'value': item_data['sku']})
        if item_data.get('pn') and item_data['pn'] in ocr_text:
            matches.append({'field': 'PN', 'status': 'OK', 'value': item_data['pn']})
        if item_data.get('serial_number') and item_data['serial_number'] in ocr_text:
            matches.append({'field': 'SN', 'status': 'OK', 'value': item_data['serial_number']})
        return matches

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

    # -------- Auth OIDC --------
    @app.route('/login')
    def login():
        redirect_uri = url_for('auth_callback', _external=True)
        return oauth.safran.authorize_redirect(redirect_uri)

    @app.route('/auth/callback')
    def auth_callback():
        try:
            token = oauth.safran.authorize_access_token()
            userinfo = oauth.safran.parse_id_token(token)
            if not userinfo:
                flash('Erreur d\'authentification OIDC', 'error')
                return redirect(url_for('index'))

            email = userinfo.get('email') or userinfo.get('preferred_username')
            if not email:
                flash('Email manquant dans le token OIDC', 'error')
                return redirect(url_for('index'))

            db = get_db()
            row = db.execute('SELECT * FROM user WHERE email=?', (email,)).fetchone()
            if not row:
                # Créer un nouvel utilisateur avec rôle par défaut
                username = userinfo.get('preferred_username') or email
                display_name = userinfo.get('name') or username
                db.execute("""
                    INSERT INTO user(username, email, display_name, role, active, password_hash)
                    VALUES (?, ?, ?, 'user', 1, NULL)
                """, (username, email, display_name))
                db.commit()
                row = db.execute('SELECT * FROM user WHERE email=?', (email,)).fetchone()

            if row and row['active']:
                login_user(SimpleUser(row))
                flash('Connecté via SSO', 'ok')
                return redirect(url_for('index'))
            else:
                flash('Utilisateur inactif', 'error')
                return redirect(url_for('index'))
        except Exception as e:
            flash(f'Erreur OIDC: {str(e)}', 'error')
            return redirect(url_for('index'))

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        # Optionnel: logout du provider OIDC
        logout_url = os.getenv('OIDC_LOGOUT_URL')
        if logout_url:
            return redirect(logout_url)
        return redirect(url_for('index'))

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
            sku = generate_sku()  # Toujours générer automatiquement
            size=(request.form.get('size') or 'PETIT').upper()
            desc=(request.form.get('description') or '').strip(); avis=(request.form.get('avis_no') or '').strip() or None
            od=(request.form.get('order_no') or '').strip() or None; bl=(request.form.get('bl_no') or '').strip() or None
            status='Attente Douane' if sous_douane else 'Attente Photo'
            db.execute("""
                INSERT INTO item(
                    sku, pn, description, serial_number, tsn, csn, size,
                    avis_no, order_no, bl_no,
                    status, st_repair, repair_snpa, hors_gabarit,
                    active, location_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (sku, pn, desc, serial_number, tsn, csn, size, avis, od, bl, status, st_repair, repair_snpa, hors_gabarit, 1, None))
            new_id=db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']; db.commit()
            flash(f'Article créé {sku} — statut: {status}','ok'); return redirect(url_for('item_detail', item_id=new_id))
        q=(request.args.get('q') or '').strip(); base_sql="SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.active=1"; params=[]
        if q:
            base_sql += " AND (i.sku LIKE ? OR i.description LIKE ? OR i.avis_no LIKE ? OR i.order_no LIKE ? OR i.bl_no LIKE ?)"; params.extend([f'%{q}%']*5)
        base_sql += " ORDER BY i.created_at DESC LIMIT 200"; rows=db.execute(base_sql, tuple(params)).fetchall()
        return abort(404)

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
        sol_free=free_sol_slots(); return abort(404)

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
        locs=db.execute(sql, tuple(params)).fetchall(); return abort(404)

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

    # -------------------- WORKLOAD VIEWS --------------------
    @app.route('/storage-map')
    @login_required
    def storage_map():
        """Représentation graphique des emplacements de stockage"""
        db = get_db()
        locations = db.execute("""
            SELECT id, code, name, kind, capacity, size, active, COALESCE(chariot_status, 'libre') AS chariot_status
            FROM location
            ORDER BY kind, code
        """).fetchall()
        
        # Grouper par kind
        by_kind = {'SOL': {'GRAND': [], 'PETIT': []}, 'ETAGERE': [], 'POSTE': []}
        for loc in locations:
            if loc['kind'] == 'SOL':
                by_kind['SOL'][loc['size']].append(loc)
            else:
                by_kind[loc['kind']].append(loc)
        
        return render_template('storage_map.html', locations=locations, by_kind=by_kind)

    @app.route('/storage/<int:location_id>/status', methods=['POST'])
    @login_required
    @role_required('admin')
    def update_chariot_status(location_id):
        """Mettre à jour le statut d'un chariot"""
        db = get_db()
        loc = db.execute("SELECT * FROM location WHERE id=?", (location_id,)).fetchone()
        
        if not loc or loc['kind'] != 'SOL':
            return abort(404)
        
        new_status = request.form.get('status')
        if new_status not in ['libre', 'indisponible_vide', 'indisponible_plein']:
            return abort(400)
        
        db.execute("UPDATE location SET chariot_status=? WHERE id=?", (new_status, location_id))
        db.commit()
        
        flash(f"Statut du chariot {loc['code']} mis à jour : {new_status}", 'ok')
        return redirect(url_for('storage_map'))
    # @app.route('/workload/reception')  # Supprimé - réception directe sans encours
    # @login_required
    # @role_required('reception', 'admin')
    # def workload_reception():
    #     # Code supprimé - réception directe

    @app.route('/workload/douane')
    @login_required
    @role_required('douane', 'admin')
    def workload_douane():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente Douane' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_douane.html', items=items)

    @app.route('/workload/photo')
    @login_required
    @role_required('photo', 'admin', 'user')
    def workload_photo():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente Photo' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_photo.html', items=items)

    @app.route('/workload/inspection')
    @login_required
    @role_required('inspection', 'admin')
    def workload_inspection():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente Inspection' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_inspection.html', items=items)

    @app.route('/workload/rac')
    @login_required
    @role_required('rac', 'admin')
    def workload_rac():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente RAC' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_rac.html', items=items)

    @app.route('/workload/emballage')
    @login_required
    @role_required('emballage', 'admin')
    def workload_emballage():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente Emballage' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_emballage.html', items=items)

    @app.route('/workload/expe')
    @login_required
    @role_required('expedition', 'admin')
    def workload_expe():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Attente Expedition' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_expe.html', items=items)

    @app.route('/workload/repair')
    @login_required
    @role_required('admin')
    def workload_repair():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Repair' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_repair.html', items=items)



    @app.route('/workload/kardex')
    @login_required
    @role_required('admin')
    def workload_kardex():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Kardex' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_kardex.html', items=items)

    @app.route('/workload/input_st')
    @login_required
    @role_required('admin')
    def workload_input_st():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='Input ST' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_input_st.html', items=items)

    @app.route('/workload/nogo')
    @login_required
    @role_required('admin')
    def workload_nogo():
        db = get_db()
        items = db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status='NOGO' AND i.active=1 ORDER BY i.created_at ASC").fetchall()
        return render_template('workload_nogo.html', items=items)

    # -------------------- WORKFLOWS --------------------
    # 1. RÉCEPTION - Input Client
    @app.route('/work/reception', methods=['GET', 'POST'])
    @login_required
    @role_required('reception', 'admin')
    def work_reception():
        db = get_db()
        
        if request.method == 'GET':
            return render_template('work_reception.html')
        
        # POST: Créer un nouvel item en réception
        sku = (request.form.get('sku') or '').strip()
        qrcode_url = (request.form.get('qrcode_url') or '').strip() or None
        pn = (request.form.get('pn') or '').strip() or None
        serial_number = (request.form.get('serial_number') or '').strip() or None
        tsn_raw = (request.form.get('tsn') or '').strip()
        csn_raw = (request.form.get('csn') or '').strip()
        tsn = int(tsn_raw) if tsn_raw.isdigit() else None
        csn = int(csn_raw) if csn_raw.isdigit() else None
        size = (request.form.get('size') or 'PETIT').upper()
        avis = (request.form.get('avis_no') or '').strip() or None
        od = (request.form.get('order_no') or '').strip() or None
        bl = (request.form.get('bl_no') or '').strip() or None
        return_st = 1 if (request.form.get('return_st') == 'on') else 0
        sous_douane = 1 if (request.form.get('sous_douane') == 'on') else 0
        st_repair = 1 if (request.form.get('st_repair') == 'on') else 0
        repair_snpa = 0  # SNPA décision en induction, pas à la création
        
        # Remplissage depuis Dynamics365 (QR Code) si disponible
        dynamics_info = fetch_dynamics_info(qrcode_url)
        if dynamics_info:
            if not sku and dynamics_info.get('sku'):
                sku = dynamics_info.get('sku')
            if not pn and dynamics_info.get('pn'):
                pn = dynamics_info.get('pn')
            if not serial_number and dynamics_info.get('serial_number'):
                serial_number = dynamics_info.get('serial_number')
            if tsn is None and dynamics_info.get('tsn') is not None:
                tsn = dynamics_info.get('tsn')
            if csn is None and dynamics_info.get('csn') is not None:
                csn = dynamics_info.get('csn')

        # Si le SKU est manquant : auto-génération
        if not sku:
            sku = generate_sku()
        
        if not sku:
            flash('Erreur génération SKU', 'error')
            return redirect(url_for('work_reception'))
        
        if size not in ('GRAND', 'PETIT', 'HORS GABARIT'):
            flash('Taille requise (GRAND/PETIT/HORS GABARIT)', 'error')
            return redirect(url_for('work_reception'))

        # Logique de réception : création ou récupération selon return_st
        if return_st == 1:
            # RETOUR SOUS-TRAITANCE : récupérer l'item existant
            existing_item = db.execute("""
                SELECT id, status FROM item 
                WHERE sku = ? AND active = 1
                ORDER BY created_at DESC LIMIT 1
            """, (sku,)).fetchone()
            
            if not existing_item:
                flash(f'Erreur: Aucun article trouvé avec SKU {sku} pour retour ST', 'error')
                return redirect(url_for('work_reception'))
            
            item_id = existing_item['id']
            
            # Appliquer les règles workflow (TL12 OK → TL5)
            if sous_douane == 1:
                new_status = 'Attente Douane'
            else:
                new_status = 'Attente Inspection'
            
            # Mettre à jour l'item existant
            db.execute("""
                UPDATE item SET 
                    status = ?, 
                    sous_douane = ?,
                    st_repair = ?,
                    repair_snpa = ?,
                    serial_number = ?,
                    tsn = ?,
                    csn = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (new_status, sous_douane, st_repair, repair_snpa, serial_number, tsn, csn, item_id))
            
            _movement(item_id, 'RECEPTION_RETURN_ST', from_id=None)
            flash(f'Article {sku} récupéré (Retour ST) → {new_status}', 'ok')
            
        else:
            # NOUVEL ARTICLE : création normale
            # Appliquer les règles workflow (TL12 NOK → PR1 New_Item → TL5)
            if sous_douane == 1:
                initial_status = 'Attente Douane'
            else:
                initial_status = 'Attente Photo'
            
            db.execute("""
                INSERT INTO item(
                    sku, pn, serial_number, tsn, csn, size,
                    avis_no, order_no, bl_no,
                    status, st_repair, repair_snpa, return_st, sous_douane,
                    active, location_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (sku, pn, serial_number, tsn, csn, size, avis, od, bl, initial_status, st_repair, repair_snpa, return_st, sous_douane, 1, None))
            
            new_id = db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            flash(f'Article créé: {sku} (Status: {initial_status})', 'ok')
        
        db.commit()
        return redirect(url_for('index'))
    @app.route('/work/douane', methods=['GET', 'POST'])
    @login_required
    @role_required('douane', 'admin')
    def work_douane():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Douane'
                ORDER BY i.created_at ASC
            """).fetchall()
            return render_template('workload_douane.html', items=items)
    
    @app.route('/items/<int:item_id>/douane_ok', methods=['POST'])
    @login_required
    @role_required('douane', 'admin')
    def douane_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Douane':
            flash('Statut invalide pour douane', 'error')
            return redirect(url_for('work_douane'))
        
        # TL1: Return_ST =1 ?
        next_status = 'Attente Inspection' if it['return_st'] == 1 else 'Attente Photo'
        _set_status(item_id, next_status)
        _movement(item_id, 'DOUANE_OK', from_id=it['location_id'])
        flash(f'Douane OK → {next_status}', 'ok')
        return redirect(url_for('work_douane'))
    
    @app.route('/items/<int:item_id>/douane_nok', methods=['POST'])
    @login_required
    @role_required('douane', 'admin')
    def douane_nok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Douane':
            flash('Statut invalide pour douane', 'error')
            return redirect(url_for('work_douane'))
        
        # Déterminer le prochain statut selon Sous Douane
        next_status = 'Attente Photo' if it['sous_douane'] == 1 else 'Attente Emballage'
        _set_status(item_id, next_status)
        _movement(item_id, 'DOUANE_NOK', from_id=it['location_id'])
        flash(f'Douane NOK → {next_status}', 'nok')
        return redirect(url_for('work_douane'))
    
    # 3. RÉCEPTION (Contrôle) - SUPPRIMÉ : Réception directe sans encours
    # @app.route('/work/reception_check', methods=['GET', 'POST'])
    # @login_required
    # @role_required('reception', 'admin')
    # def work_reception_check():
    #     # Code supprimé - réception directe

    # @app.route('/items/<int:item_id>/reception_ok', methods=['POST'])
    # @login_required
    # @role_required('reception', 'admin')
    # def reception_ok(item_id):
    #     # Code supprimé - réception directe

    # @app.route('/items/<int:item_id>/reception_nok', methods=['POST'])
    # @login_required
    # @role_required('reception', 'admin')
    # def reception_nok(item_id):
    #     # Code supprimé - réception directe
    
    # 4. POSTE PHOTO
    @app.route('/work/photo', methods=['GET', 'POST'])
    @login_required
    @role_required('photo', 'admin')
    def work_photo():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Photo'
                ORDER BY i.created_at ASC
            """).fetchall()
            return render_template('workload_photo.html', items=items)

    @app.route('/work/photo/<int:item_id>', methods=['GET', 'POST'])
    @login_required
    @role_required('photo', 'admin')
    def photo_station(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1 or it['status'] != 'Attente Photo':
            abort(404)
        
        if request.method == 'GET':
            return render_template('photo_station.html', item=it)
        
        # POST: Traitement photos et OCR
        action = request.form.get('action')
        
        if action == 'capture':
            # Appel API OrbitView
            orbitview_result = call_orbitview_api(item_id)
            if 'error' in orbitview_result:
                flash(f'Erreur capture: {orbitview_result["error"]}', 'error')
            else:
                flash('Photos capturées avec succès', 'ok')
                # Sauvegarder URL photo si fournie
                photo_url = orbitview_result.get('photo_url')
                if photo_url:
                    db.execute('UPDATE item SET photo_path=? WHERE id=?', (photo_url, item_id))
                    db.commit()
            return redirect(url_for('photo_station', item_id=item_id))
        
        elif action == 'ocr':
            # Lecture OCR de la plaque
            photo_file = request.files.get('plaque_photo')
            if photo_file:
                try:
                    image_bytes = photo_file.read()
                    ocr_text = perform_ocr(image_bytes)
                    
                    # Vérifier correspondance
                    item_info = {'sku': it['sku'], 'pn': it['pn'], 'serial_number': it['serial_number']}
                    verification = verify_item_info(item_info, ocr_text)
                    
                    return jsonify({
                        'ocr_text': ocr_text,
                        'verification': verification,
                        'status': 'ok'
                    })
                except Exception as e:
                    return jsonify({'error': str(e), 'status': 'error'})
            return jsonify({'error': 'Pas de fichier fourni', 'status': 'error'})
        
        elif action == 'validate':
            # Valider et passer à l'étape suivante
            next_status = 'Prison' if it['photo_ins'] == 1 else 'Attente Induction'
            _set_status(item_id, next_status)
            _movement(item_id, 'PHOTO_OK', from_id=it['location_id'])
            flash(f'Photo validée → {next_status}', 'ok')
            return redirect(url_for('work_photo'))
    
    @app.route('/items/<int:item_id>/photo_ok', methods=['POST'])
    @login_required
    @role_required('photo', 'admin')
    def photo_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Photo':
            flash('Statut invalide pour photo', 'error')
            return redirect(url_for('work_photo'))
        
        # TL7: Photo_Ins =1 ?
        next_status = 'Prison' if it['photo_ins'] == 1 else 'Attente Induction'
        _set_status(item_id, next_status)
        _movement(item_id, 'PHOTO_OK', from_id=it['location_id'])
        flash(f'Photo OK → {next_status}', 'ok')
        return redirect(url_for('work_photo'))
    
    # 4. INDUCTION
    @app.route('/work/induction', methods=['GET', 'POST'])
    @login_required
    @role_required('induction', 'admin')
    def work_induction():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Induction'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/induction_ok', methods=['POST'])
    @login_required
    @role_required('induction', 'admin')
    def induction_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Induction':
            flash('Statut invalide pour induction', 'error')
            return redirect(url_for('work_induction'))
        
        # TL2: Induction_OK ? OK: TL3 Internal_Repair ?
        if it['induction_ok'] == 1:
            # TL3: Internal_Repair ? (int_repair_capa)
            if it['int_repair_capa'] == 1:
                next_status = 'Depart Atelier'
            else:
                # TL4: Process_Mixte ? (repair_int)
                next_status = 'Depart Atelier' if it['repair_int'] == 1 else 'Attente Emballage'
        else:
            next_status = 'NOGO'
        
        _set_status(item_id, next_status)
        _movement(item_id, 'INDUCTION_OK', from_id=it['location_id'])
        flash(f'Induction OK → {next_status}', 'ok')
        return redirect(url_for('work_induction'))
    
    @app.route('/items/<int:item_id>/induction_nok', methods=['POST'])
    @login_required
    @role_required('induction', 'admin')
    def induction_nok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Induction':
            flash('Statut invalide pour induction', 'error')
            return redirect(url_for('work_induction'))
        
        _set_status(item_id, 'NOGO')
        _movement(item_id, 'INDUCTION_NOK', from_id=it['location_id'])
        flash('Induction NOK → NOGO', 'nok')
        return redirect(url_for('work_induction'))
    
    # 5. DEPART ATELIER
    @app.route('/work/depart_atelier', methods=['GET', 'POST'])
    @login_required
    @role_required('atelier', 'admin')
    def work_depart_atelier():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Depart Atelier'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/depart_atelier_ok', methods=['POST'])
    @login_required
    @role_required('atelier', 'admin')
    def depart_atelier_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Depart Atelier':
            flash('Statut invalide pour depart atelier', 'error')
            return redirect(url_for('work_depart_atelier'))
        
        _set_status(item_id, 'Retour Atelier')
        _movement(item_id, 'DEPART_ATELIER_OK', from_id=it['location_id'])
        flash('Depart Atelier OK → Retour Atelier', 'ok')
        return redirect(url_for('work_depart_atelier'))
    
    # 6. RETOUR ATELIER
    @app.route('/work/retour_atelier', methods=['GET', 'POST'])
    @login_required
    @role_required('atelier', 'admin')
    def work_retour_atelier():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Retour Atelier'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/retour_atelier_ok', methods=['POST'])
    @login_required
    @role_required('atelier', 'admin')
    def retour_atelier_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Retour Atelier':
            flash('Statut invalide pour retour atelier', 'error')
            return redirect(url_for('work_retour_atelier'))
        
        # TL9: Int_Repair_Capa =1 ?
        if it['int_repair_capa'] == 1:
            db.execute('UPDATE item SET repair_int=1 WHERE id=?', (item_id,))
        else:
            db.execute('UPDATE item SET prepa_st=1 WHERE id=?', (item_id,))
        
        _set_status(item_id, 'Attente Emballage')
        _movement(item_id, 'RETOUR_ATELIER_OK', from_id=it['location_id'])
        flash('Retour Atelier OK → Attente Emballage', 'ok')
        return redirect(url_for('work_retour_atelier'))
    
    # 7. ATTENTE RAC / POSTE EMBALLAGE
    @app.route('/work/rac', methods=['GET', 'POST'])
    @login_required
    @role_required('rac', 'admin')
    def work_rac():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente RAC'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/rac_ok', methods=['POST'])
    @login_required
    @role_required('rac', 'admin')
    def rac_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente RAC':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_rac'))
        
        _set_status(item_id, 'Poste emballage')
        _movement(item_id, 'RAC_OK', from_id=it['location_id'])
        flash('RAC OK → Poste emballage', 'ok')
        return redirect(url_for('work_rac'))
    
    # 6. POSTE EMBALLAGE (Décision NOGO + Repair SNPA)
    @app.route('/work/emballage', methods=['GET', 'POST'])
    @login_required
    @role_required('emballage', 'admin')
    def work_emballage():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Emballage'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/emballage_nogo', methods=['POST'])
    @login_required
    @role_required('emballage', 'admin')
    def emballage_nogo(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Emballage':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_emballage'))
        
        db.execute('UPDATE item SET is_nogo=1, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Exp ST Return', item_id))
        _movement(item_id, 'EMBALLAGE_NOGO', from_id=it['location_id'])
        db.commit()
        flash('Article NOGO → Exp ST Return → FIN', 'ok')
        return redirect(url_for('work_emballage'))
    
    @app.route('/items/<int:item_id>/emballage_repair_decision', methods=['POST'])
    @login_required
    @role_required('emballage', 'admin')
    def emballage_repair_decision(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Emballage':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_emballage'))
        
        _set_status(item_id, 'Attente Expedition')
        _movement(item_id, f'EMBALLAGE_OK', from_id=it['location_id'])
        flash('Emballage OK → Attente Expedition', 'ok')
        return redirect(url_for('work_emballage'))
    
    # 7. ATTENTE SAP (Création Avis SAP)
    @app.route('/work/sap', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'reception')
    def work_sap():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente SAP'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/sap_created', methods=['POST'])
    @login_required
    @role_required('admin', 'reception')
    def sap_created(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente SAP':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_sap'))
        
        db.execute('UPDATE item SET sap_created=1, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Disponible', item_id))
        _movement(item_id, 'SAP_CREATED', from_id=it['location_id'])
        db.commit()
        flash('Avis SAP créé → Disponible', 'ok')
        return redirect(url_for('work_sap'))
    
    # 8. RÉPARATION SNPA
    @app.route('/work/repair', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def work_repair():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Réparation'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/repair_ok', methods=['POST'])
    @login_required
    @role_required('admin')
    def repair_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Réparation':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_repair'))
        
        _set_status(item_id, 'Attente Emballage')
        _movement(item_id, 'REPAIR_OK', from_id=it['location_id'])
        flash('Réparation OK → Attente Emballage', 'ok')
        return redirect(url_for('work_repair'))
    
    @app.route('/items/<int:item_id>/repair_nogo', methods=['POST'])
    @login_required
    @role_required('admin')
    def repair_nogo(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Réparation':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_repair'))
        
        db.execute('UPDATE item SET is_nogo=1, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Exp ST Return', item_id))
        _movement(item_id, 'REPAIR_NOGO', from_id=it['location_id'])
        db.commit()
        flash('Réparation NOGO → Exp ST Return → FIN', 'ok')
        return redirect(url_for('work_repair'))
    
    # 9. INSPECTION + RAC (boucle)
    @app.route('/work/inspection', methods=['GET', 'POST'])
    @login_required
    @role_required('inspection', 'admin')
    def work_inspection():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status IN ('Attente Inspection', 'Attente photo RAC')
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/inspection_ok', methods=['POST'])
    @login_required
    @role_required('inspection', 'admin')
    def inspection_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] not in ('Attente Inspection', 'Attente photo RAC'):
            flash('Statut invalide', 'error')
            return redirect(url_for('work_inspection'))
        
        # TL10: inspection ok ? OK: TL11 Go Pool ?
        if it['inspection_ok'] == 1:
            next_status = 'Disponible' if it['prepa_st'] == 1 else 'Attente Emballage'
        else:
            next_status = 'Attente Emballage'
        
        _set_status(item_id, next_status)
        _movement(item_id, 'INSPECTION_OK', from_id=it['location_id'])
        flash(f'Inspection OK → {next_status}', 'ok')
        return redirect(url_for('work_inspection'))
    
    @app.route('/items/<int:item_id>/inspection_nok', methods=['POST'])
    @login_required
    @role_required('inspection', 'admin')
    def inspection_nok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] not in ('Attente Inspection', 'Attente photo RAC'):
            flash('Statut invalide', 'error')
            return redirect(url_for('work_inspection'))
        
        # TL10 NOK: Photo_Ins =1 --> Attente_Photo
        db.execute('UPDATE item SET photo_ins=1 WHERE id=?', (item_id,))
        _set_status(item_id, 'Attente Photo')
        _movement(item_id, 'INSPECTION_NOK', from_id=it['location_id'])
        flash('Inspection NOK → Attente Photo', 'nok')
        return redirect(url_for('work_inspection'))
    
    @app.route('/items/<int:item_id>/rac_retry_ok', methods=['POST'])
    @login_required
    @role_required('rac', 'admin')
    def rac_retry_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente photo RAC':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_rac'))
        
        _set_status(item_id, 'Exp Client')
        _movement(item_id, 'RAC_RETRY_OK', from_id=it['location_id'])
        db.commit()
        flash('RAC Retry OK → Exp Client → FIN', 'ok')
        return redirect(url_for('work_rac'))
    
    @app.route('/items/<int:item_id>/rac_retry_nok', methods=['POST'])
    @login_required
    @role_required('rac', 'admin')
    def rac_retry_nok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente photo RAC':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_rac'))
        
        _set_status(item_id, 'Attente inspection')
        _movement(item_id, 'RAC_RETRY_NOK', from_id=it['location_id'])
        flash('RAC Retry NOK → Re-inspection', 'ok')
        return redirect(url_for('work_rac'))
    
    # 10. POOL (Décision Pool / Prison)
    @app.route('/work/pool', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def work_pool():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Pool'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/pool_yes', methods=['POST'])
    @login_required
    @role_required('admin')
    def pool_yes(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Pool':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_pool'))
        
        _set_status(item_id, 'Disponible')
        _movement(item_id, 'POOL_YES', from_id=it['location_id'])
        flash('Pool OUI → Disponible', 'ok')
        return redirect(url_for('work_pool'))
    
    @app.route('/items/<int:item_id>/pool_no', methods=['POST'])
    @login_required
    @role_required('admin')
    def pool_no(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Pool':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_pool'))
        
        _set_status(item_id, 'Attente Prison')
        _movement(item_id, 'POOL_NO', from_id=it['location_id'])
        flash('Pool NON → Attente Prison', 'ok')
        return redirect(url_for('work_pool'))
    
    # 11. PRISON (Suppression / Retour Inspection)
    @app.route('/work/prison', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def work_prison():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Prison'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/prison_delete', methods=['POST'])
    @login_required
    @role_required('admin')
    def prison_delete(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Prison':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_prison'))
        
        log_delete(item_id, user=getattr(current_user, 'username', None))
        flash('Item supprimé (Prison) → FIN', 'ok')
        return redirect(url_for('work_prison'))
    
    @app.route('/items/<int:item_id>/prison_retry', methods=['POST'])
    @login_required
    @role_required('admin')
    def prison_retry(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Prison':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_prison'))
        
        _set_status(item_id, 'Attente Inspection')
        _movement(item_id, 'PRISON_RETRY', from_id=it['location_id'])
        flash('Prison Retry → Attente Inspection', 'ok')
        return redirect(url_for('work_prison'))
    
    @app.route('/items/<int:item_id>/prison_ok', methods=['POST'])
    @login_required
    @role_required('admin')
    def prison_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Prison':
            flash('Statut invalide pour prison', 'error')
            return redirect(url_for('work_prison'))
        
        log_delete(item_id, user=getattr(current_user, 'username', None))
        db.execute('UPDATE item SET active=0, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('ARCHIVE', item_id))
        db.commit()
        flash('Prison OK → ARCHIVE', 'ok')
        return redirect(url_for('work_prison'))
    
    # 12. KARDEX & EXPEDITION
    @app.route('/work/kardex', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def work_kardex():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status IN ('Attente Kardex', 'Préparation ST', 'Kardex Output')
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/kardex_ok', methods=['POST'])
    @login_required
    @role_required('kardex', 'admin')
    def kardex_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Kardex':
            flash('Statut invalide pour kardex', 'error')
            return redirect(url_for('work_kardex'))
        
        # Déterminer le prochain statut selon Prepa_ST
        next_status = 'Attente Prison' if it['prepa_st'] == 1 else 'Attente Pool'
        _set_status(item_id, next_status)
        _movement(item_id, 'KARDEX_OK', from_id=it['location_id'])
        flash(f'Kardex OK → {next_status}', 'ok')
        return redirect(url_for('work_kardex'))
    
    @app.route('/items/<int:item_id>/kardex_std_exchange', methods=['POST'])
    @login_required
    @role_required('admin')
    def kardex_std_exchange(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Kardex Input':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_kardex'))
        
        is_std_exchange = request.form.get('is_std_exchange') == 'on'
        
        if is_std_exchange:
            db.execute('UPDATE item SET std_exchange=1, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Exp Externe', item_id))
            next_status = 'Exp Externe'
        else:
            db.execute('UPDATE item SET std_exchange=0, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Préparation ST', item_id))
            next_status = 'Préparation ST'
        
        _movement(item_id, f'KARDEX_DECISION_{next_status}', from_id=it['location_id'])
        db.commit()
        flash(f'Direction: {next_status}', 'ok')
        return redirect(url_for('work_kardex'))
    
    @app.route('/items/<int:item_id>/kardex_to_output', methods=['POST'])
    @login_required
    @role_required('admin')
    def kardex_to_output(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Kardex Input':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_kardex'))
        
        _set_status(item_id, 'Kardex Output')
        _movement(item_id, 'KARDEX_INPUT_TO_OUTPUT', from_id=it['location_id'])
        flash('Kardex Input → Kardex Output', 'ok')
        return redirect(url_for('work_kardex'))
    
    @app.route('/items/<int:item_id>/prepa_st_decision', methods=['POST'])
    @login_required
    @role_required('admin')
    def prepa_st_decision(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Préparation ST':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_kardex'))
        
        prepa_ok = request.form.get('prepa_ok') == 'on'
        
        if prepa_ok:
            db.execute('UPDATE item SET prepa_st_ok=1, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Appel FO', item_id))
            next_status = 'Appel FO'
        else:
            db.execute('UPDATE item SET prepa_st_ok=0, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('Kardex Output', item_id))
            next_status = 'Kardex Output'
        
        _movement(item_id, f'PREPA_ST_DECISION_{next_status}', from_id=it['location_id'])
        db.commit()
        flash(f'Direction: {next_status}', 'ok')
        return redirect(url_for('work_kardex'))
    
    @app.route('/items/<int:item_id>/appel_fo_done', methods=['POST'])
    @login_required
    @role_required('admin')
    def appel_fo_done(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Appel FO':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_kardex'))
        
        _set_status(item_id, 'Kardex Output')
        _movement(item_id, 'APPEL_FO_DONE', from_id=it['location_id'])
        flash('Appel FO → Kardex Output', 'ok')
        return redirect(url_for('work_kardex'))
    
    @app.route('/items/<int:item_id>/kardex_output_final', methods=['POST'])
    @login_required
    @role_required('admin')
    def kardex_output_final(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] not in ('Kardex Output', 'Exp Externe'):
            flash('Statut invalide', 'error')
            return redirect(url_for('work_kardex'))
        
        _set_status(item_id, 'Dossier Induction')
        _movement(item_id, 'KARDEX_FINAL', from_id=it['location_id'])
        
        # Finalisation
        db.execute('UPDATE item SET status=?, active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('STOCK', item_id))
        db.commit()
        
        flash('Item finalisé → STOCK → FIN', 'ok')
        return redirect(url_for('work_kardex'))
    
    # INPUT ST (Direct path for standard items)
    @app.route('/work/input_st', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def work_input_st():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Input ST'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)
    
    @app.route('/items/<int:item_id>/input_st_done', methods=['POST'])
    @login_required
    @role_required('admin')
    def input_st_done(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Input ST':
            flash('Statut invalide', 'error')
            return redirect(url_for('work_input_st'))
        
        _set_status(item_id, 'Attente inspection')
        _movement(item_id, 'INPUT_ST_DONE', from_id=it['location_id'])
        flash('Input ST done → Attente inspection', 'ok')
        return redirect(url_for('work_input_st'))

    # EXPÉDITION
    @app.route('/work/expe', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'expedition')
    def work_expe():
        db = get_db()
        
        if request.method == 'GET':
            items = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Expedition'
                ORDER BY i.created_at ASC
            """).fetchall()
            client = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Expe Client'
                ORDER BY i.created_at ASC
            """).fetchall()
            st = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente Expe ST'
                ORDER BY i.created_at ASC
            """).fetchall()
            return abort(404)

    @app.route('/items/<int:item_id>/expedition_ok', methods=['POST'])
    @login_required
    @role_required('expedition', 'admin')
    def expedition_ok(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        if it['status'] != 'Attente Expedition':
            flash('Statut invalide pour expedition', 'error')
            return redirect(url_for('work_expe'))
        
        # TL8: Return_ST =1 Or Repair_INT =1
        next_status = 'Attente Expe Client' if (it['return_st'] == 1 or it['repair_int'] == 1) else 'Attente Expe ST'
        _set_status(item_id, next_status)
        _movement(item_id, 'EXPEDITION_OK', from_id=it['location_id'])
        flash(f'Expedition OK → {next_status}', 'ok')
        return redirect(url_for('work_expe'))

    @app.route('/items/<int:item_id>/expe_ship', methods=['POST'])
    @login_required
    @role_required('admin', 'expedition')
    def expe_ship(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it or it['active'] != 1:
            abort(404)
        
        kind = (request.form.get('kind') or 'client')
        valid = {
            'client': 'Attente Expe Client',
            'st': 'Attente Expe ST'
        }
        
        if it['status'] not in valid.values():
            flash('Statut invalide pour expédition', 'error')
            return redirect(url_for('work_expe'))
        
        _movement(item_id, f'EXPE_{kind.upper()}', from_id=it['location_id'])
        if kind == 'st':
            # Attente Return_ST --> Reception
            _set_status(item_id, 'Attente Réception')
            flash('Exp ST → Attente Réception', 'ok')
        else:
            log_delete(item_id, user=getattr(current_user, 'username', None))
            db.execute('UPDATE item SET active=0, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', ('ARCHIVE', item_id))
            db.commit()
            flash('Exp Client → ARCHIVE', 'ok')
        return redirect(url_for('work_expe'))

    # ===================== WORKFLOWS & VISUALIZATION ===============================
    
    @app.route('/workflow/return-st')
    @login_required
    @role_required('admin', 'manager')
    def return_st_workflow():
        """Display the Return ST supply chain workflow diagram"""
        return abort(404)

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

        return abort(404)

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

    #-------------------Dashbaord WIP-------------------#

    @app.route("/Global_WIP")
    @login_required
    def Global_WIP():
        db = get_db()

        # Tous les articles actifs avec avis, statut et prochaine phase
        items = db.execute("""
            SELECT id, sku, avis_no, status, size, st_repair, repair_snpa, updated_at
            FROM item
            WHERE active = 1
            ORDER BY updated_at DESC
        """).fetchall()

        # Calcul de la prochaine étape (logicielle)
        def next_phase(row):
            st = row["status"]

            if st == "ATTENTE_DOUANE":
                return "Photo"
            if st == "ATTENTE_PHOTO":
                return "Inspection"
            if st == "ATTENTE_INSPECTION":
                return "Pool / Emballage"
            if st == "NOGO":
                return "Blocage / Re-inspection"
            if st == "ATTENTE_RAC":
                return "Emballage"
            if st == "ATTENTE_EMBALLAGE":
                return "Expédition"
            if st == "ATTENTE_EXPE_CLIENT":
                return "Client"
            if st == "ATTENTE_EXPE_ST":
                return "ST"
            if st == "ATTENTE_EXPE_T2":
                return "T2"
            return "—"

        enriched = []
        for row in items:
            enriched.append({
                "id": row["id"],
                "sku": row["sku"],
                "avis_no": row["avis_no"],
                "status": row["status"],
                "next": next_phase(row),
                "updated_at": row["updated_at"]
            })

        return abort(404)

    return app

app=create_app()
if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','5000')), debug=True)
