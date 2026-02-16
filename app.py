import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, g, abort, send_from_directory, flash
from mir_client import MiRClient
from processus import Item, Location, can_move, next_status_for_location, choose_slot

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DATA_DIR=os.path.join(BASE_DIR,'data')
UPLOAD_DIR=os.path.join(BASE_DIR,'uploads')
os.makedirs(DATA_DIR,exist_ok=True)
os.makedirs(UPLOAD_DIR,exist_ok=True)
DATABASE=os.environ.get('DATABASE',os.path.join(DATA_DIR,'stock.db'))

MIR_AFTER_STOCK=os.getenv('MIR_MISSION_AFTER_STOCK')

def create_app():
    app=Flask(__name__)
    app.config['UPLOAD_FOLDER']=UPLOAD_DIR
    app.secret_key=os.environ.get('SECRET_KEY','dev-secret')

    def get_db():
        if 'db' not in g:
            g.db=sqlite3.connect(DATABASE,detect_types=sqlite3.PARSE_DECLTYPES)
            g.db.row_factory=sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db=g.pop('db',None)
        if db is not None:
            db.close()

    def init_db():
        db=get_db()
        db.execute('PRAGMA foreign_keys=ON;')
        db.execute("""CREATE TABLE IF NOT EXISTS location(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('SOL','ETAGERE','POSTE')),
          capacity INTEGER,
          size TEXT,
          active INTEGER NOT NULL DEFAULT 1
        );""")
        db.execute("""CREATE TABLE IF NOT EXISTS item(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sku TEXT NOT NULL,
          description TEXT,
          photo_path TEXT,
          size TEXT CHECK(size IN ('GRAND','PETIT') OR size IS NULL),
          status TEXT NOT NULL CHECK(status IN ('RECU','PHOTO','INSPECTION','EMBALLAGE','STOCK','NOGO')),
          location_id INTEGER,
          avis_no TEXT,
          order_no TEXT,
          bl_no TEXT,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(location_id) REFERENCES location(id) ON DELETE SET NULL
        );""")
        db.execute("""CREATE TABLE IF NOT EXISTS movement(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          from_location_id INTEGER,
          to_location_id INTEGER,
          action TEXT NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          user TEXT,
          FOREIGN KEY(item_id) REFERENCES item(id) ON DELETE CASCADE
        );""")
        db.commit()

        # MIGRATION
        def column_exists(table, col):
            rows=db.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r['name']==col for r in rows)
        if not column_exists('location','size'):
            db.execute('ALTER TABLE location ADD COLUMN size TEXT')
        if not column_exists('location','active'):
            db.execute('ALTER TABLE location ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        if not column_exists('item','size'):
            db.execute('ALTER TABLE item ADD COLUMN size TEXT')
        db.commit()
        db.execute("""UPDATE location SET size='GRAND' WHERE kind='SOL' AND (size IS NULL OR size='') AND (code GLOB 'S-A[1-4]' OR code GLOB 'S-B[1-4]')""")
        db.execute("""UPDATE location SET size='PETIT' WHERE kind='SOL' AND (size IS NULL OR size='') AND (
            code GLOB 'S-C[1-6]' OR code GLOB 'S-D[1-6]' OR code GLOB 'S-E[1-6]' OR
            code GLOB 'S-F[1-6]' OR code GLOB 'S-G[1-6]' OR code GLOB 'S-H[1-6]')""")
        db.commit()

        c=db.execute('SELECT COUNT(*) AS c FROM location').fetchone()['c']
        if c==0:
            for code in ['S-A1','S-A2','S-A3','S-A4','S-B1','S-B2','S-B3','S-B4']:
                db.execute("INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                           (code,f'Grand Chariot {code}','SOL',1,'GRAND'))
            for row in ['C','D','E','F','G','H']:
                for i in range(1,7):
                    code=f'S-{row}{i}'
                    db.execute("INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                               (code,f'Petit Chariot {code}','SOL',1,'PETIT'))
            for code,name in [('POSTE-PHOTO','Poste Photo'),('POSTE-INSPECTION','Poste Inspection'),('POSTE-EMBALLAGE','Poste Emballage')]:
                db.execute("INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                           (code,name,'POSTE',1,None))
            for e in [1,2,3]:
                for s in ['A','B','C','D']:
                    code=f'ETAGERE-{e}-{s}'
                    db.execute("INSERT INTO location(code,name,kind,capacity,size) VALUES (?,?,?,?,?)",
                               (code,f'Etagère {e} plateau {s}','ETAGERE',None,None))
            db.commit()

    with app.app_context():
        init_db()

    def item_by_id(item_id):
        return get_db().execute('SELECT * FROM item WHERE id=?',(item_id,)).fetchone()
    def location_by_id(loc_id):
        return get_db().execute('SELECT * FROM location WHERE id=?',(loc_id,)).fetchone()
    def location_by_code(code):
        return get_db().execute('SELECT * FROM location WHERE code=?',(code,)).fetchone()
    def count_items_in_location(loc_id):
        return get_db().execute('SELECT COUNT(*) AS c FROM item WHERE location_id=?',(loc_id,)).fetchone()['c']

    def row_to_item(row):
        code=None
        if row['location_id']:
            r=get_db().execute('SELECT code FROM location WHERE id=?',(row['location_id'],)).fetchone()
            code=r['code'] if r else None
        return Item(id=row['id'], sku=row['sku'], size=row['size'], status=row['status'], location_code=code)
    def row_to_location(row):
        return Location(id=row['id'], code=row['code'], kind=row['kind'], capacity=row['capacity'], size=row['size'])

    def free_sol_slots():
        return get_db().execute("""
          SELECT l.* FROM location l
          LEFT JOIN item it ON it.location_id=l.id
          WHERE l.kind='SOL' AND l.capacity=1 AND l.active=1
          GROUP BY l.id HAVING COUNT(it.id)=0
          ORDER BY l.code
        """).fetchall()

    def move_item(item_id,to_location_id,action='MOVE',user=None):
        db=get_db()
        it=item_by_id(item_id); dest=location_by_id(to_location_id)
        if not it or not dest: abort(400)
        occ=count_items_in_location(dest['id'])
        ok,msg=can_move(row_to_item(it),row_to_location(dest),occ)
        if not ok: raise ValueError(msg)
        db.execute('INSERT INTO movement(item_id,from_location_id,to_location_id,action,user) VALUES (?,?,?,?,?)',
                   (item_id,it['location_id'],to_location_id,action,user))
        new_status=next_status_for_location(row_to_location(dest)) or it['status']
        db.execute('UPDATE item SET location_id=?, status=? WHERE id=?',(to_location_id,new_status,item_id))
        db.commit(); return new_status

    @app.route('/')
    def index():
        db=get_db()
        kg={'GRAND':{},'PETIT':{}}
        for size in ['GRAND','PETIT']:
            total=db.execute("SELECT COUNT(*) AS c FROM location WHERE kind='SOL' AND size=?",(size,)).fetchone()['c']
            occ=db.execute("SELECT COUNT(*) AS c FROM item i JOIN location l ON i.location_id=l.id WHERE l.kind='SOL' AND l.size=?",(size,)).fetchone()['c']
            kg[size]={'total':total,'occupied':occ,'free':total-occ}
        statuses=db.execute('SELECT status, COUNT(*) AS c FROM item GROUP BY status').fetchall()
        return render_template('index.html',kg=kg,statuses=statuses)

    @app.route('/items',methods=['GET','POST'])
    def items():
        db=get_db()
        if request.method=='POST':
            sku=(request.form.get('sku') or '').strip()
            size=(request.form.get('size') or 'PETIT').upper()
            if size not in ('GRAND','PETIT'):
                flash('Taille requise (GRAND/PETIT)','error'); return redirect(url_for('items'))
            if not sku:
                row=db.execute("SELECT sku FROM item WHERE sku LIKE 'SKU-%' ORDER BY CAST(SUBSTR(sku,5) AS INTEGER) DESC LIMIT 1").fetchone()
                if row is None: sku='SKU-00001'
                else:
                    last=int(row['sku'].split('-')[1]); sku=f'SKU-{last+1:05d}'
            desc=(request.form.get('description') or '').strip()
            avis=(request.form.get('avis_no') or '').strip() or None
            od=(request.form.get('order_no') or '').strip() or None
            bl=(request.form.get('bl_no') or '').strip() or None
            db.execute('INSERT INTO item(sku,description,size,avis_no,order_no,bl_no,status,location_id) VALUES (?,?,?,?,?,?, "RECU", NULL)',(sku,desc,size,avis,od,bl))
            new_id=db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            db.commit(); flash(f'Article créé {sku} ({size})','ok')
            return redirect(url_for('item_detail',item_id=new_id))
        q=(request.args.get('q') or '').strip()
        if q:
            rows=db.execute("""
              SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id
              WHERE i.sku LIKE ? OR i.description LIKE ? OR i.avis_no LIKE ? OR i.order_no LIKE ? OR i.bl_no LIKE ?
              ORDER BY i.created_at DESC
            """,(f'%{q}%',)*5).fetchall()
        else:
            rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id ORDER BY i.created_at DESC LIMIT 200").fetchall()
        return render_template('items.html',items=rows,q=q)

    @app.route('/items/<int:item_id>')
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
        sol_free=free_sol_slots()
        return render_template('item_detail.html',it=it,loc=loc,moves=moves,sol_free=sol_free)

    @app.route('/items/<int:item_id>/move',methods=['POST'])
    def move(item_id):
        to_id=int(request.form.get('to_location_id'))
        try:
            st=move_item(item_id,to_id,'MOVE'); flash(f'Déplacé (statut: {st})','ok')
        except ValueError as e:
            flash(str(e),'error')
        return redirect(url_for('item_detail',item_id=item_id))

    @app.route('/work/photo')
    def work_photo():
        db=get_db(); rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('RECU','PHOTO') ORDER BY i.created_at ASC").fetchall()
        return render_template('work_photo.html',items=rows)

    @app.route('/work/inspection',methods=['GET','POST'])
    def work_inspection():
        db=get_db()
        if request.method=='POST':
            item_id=int(request.form.get('item_id')); result=request.form.get('result')
            dest=location_by_code('POSTE-EMBALLAGE') if result=='OK' else location_by_code('POSTE-INSPECTION')
            try: move_item(item_id,dest['id'],f'INSPECT_{result}'); flash('Inspection mise à jour','ok')
            except ValueError as e: flash(str(e),'error')
            return redirect(url_for('work_inspection'))
        rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('INSPECTION') ORDER BY i.created_at ASC").fetchall()
        return render_template('work_inspection.html',items=rows)

    @app.route('/work/emballage',methods=['GET','POST'])
    def work_emballage():
        db=get_db()
        if request.method=='POST':
            item_id=int(request.form.get('item_id')); it=item_by_id(item_id)
            slots=free_sol_slots()
            slot_id=request.form.get('slot_id')
            if slot_id:
                slot=location_by_id(int(slot_id)); slot_loc=Location(id=slot['id'],code=slot['code'],kind=slot['kind'],capacity=slot['capacity'],size=slot['size'])
            else:
                slot_loc=choose_slot([Location(id=s['id'],code=s['code'],kind=s['kind'],capacity=s['capacity'],size=s['size']) for s in slots], Item(id=it['id'],sku=it['sku'],size=it['size'],status=it['status'],location_code=None))
                slot=location_by_id(slot_loc.id) if slot_loc else None
            if not slot:
                flash('Aucun emplacement SOL compatible','error'); return redirect(url_for('work_emballage'))
            try:
                move_item(item_id,slot['id'],'PUT_STOCK')
                db.execute("UPDATE item SET status='STOCK' WHERE id=?",(item_id,)); db.commit(); flash('Article stocké','ok')
                if MIR_AFTER_STOCK:
                    try: MiRClient().start_mission(MIR_AFTER_STOCK); flash('MiR: mission post-stock envoyée','ok')
                    except Exception as e: flash(f'MiR post-stock: {e}','error')
            except ValueError as e:
                flash(str(e),'error')
            return redirect(url_for('work_emballage'))
        rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('EMBALLAGE') ORDER BY i.created_at ASC").fetchall()
        sol_slots=free_sol_slots()
        return render_template('work_emballage.html',items=rows,sol_slots=sol_slots)

    @app.route('/uploads/<path:filename>')
    def uploads(filename):
        return send_from_directory(UPLOAD_DIR,filename)

    @app.route('/locations')
    def locations():
        db=get_db(); kind=request.args.get('kind')
        if kind in ('SOL','ETAGERE','POSTE'):
            locs=db.execute("SELECT l.*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id) AS occ FROM location l WHERE kind=? ORDER BY code",(kind,)).fetchall()
        else:
            locs=db.execute("SELECT l.*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id) AS occ FROM location l ORDER BY kind, code").fetchall()
        return render_template('locations.html',locations=locs,kind=kind)

    @app.route('/api/mir/status')
    def api_mir_status():
        try: return MiRClient().status(),200
        except Exception as e: return {'error':str(e)},502

    @app.route('/api/mir/missions')
    def api_mir_missions():
        try: return {'missions':MiRClient().missions()},200
        except Exception as e: return {'error':str(e)},502

    @app.route('/api/mir/mission/<guid>',methods=['POST'])
    def api_mir_start(guid):
        try: return {'ok':True,'result':MiRClient().start_mission(guid)},200
        except Exception as e: return {'ok':False,'error':str(e)},502

    @app.route('/mir')
    def mir_dashboard():
        return render_template('mir_dashboard.html')
    # --- Stub: verrouillage rôles (sera effectif Bloc 3)
    def role_required(role_name: str):
        def _decorator(fn):
            def _wrap(*args, **kwargs):
                # TODO Bloc 3: vérifier current_user.role
                return fn(*args, **kwargs)
            _wrap.__name__ = fn.__name__
            return _wrap
        return _decorator
    def _ensure_active(it):
        if not it or it['active'] != 1:
            abort(404)

def _movement(item_id:int, action:str, user:str=None, from_id=None, to_id=None):
    db=get_db()
    db.execute("""
        INSERT INTO movement(item_id, from_location_id, to_location_id, action, user)
        VALUES (?,?,?,?,?)
    """, (item_id, from_id, to_id, action, user or getattr(current_user,'username',None)))
    db.commit()

def _set_status(item_id:int, new_status:str):
    db=get_db()
    db.execute("UPDATE item SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_status,item_id))
    db.commit()

def _compute_next_after_photo(it)->str:
    """Après PHOTO_OK : selon flags -> Attente inspection ou Attente RAC"""
    return 'Attente inspection' if (it['st_repair']==1 or it['repair_snpa']==1) else 'Attente RAC'

def _place_on_shelf(item_id:int, level:int, plate:str):
    """Place sur étagère level-plate (vérif côté règle NOGO déjà en place)"""
    db=get_db()
    code = f"ETAGERE-{level}-{plate.upper()}"
    dest = db.execute("SELECT * FROM location WHERE code=? AND active=1",(code,)).fetchone()
    if not dest: raise ValueError("Étagère/plateau introuvable.")
    it  = db.execute("SELECT * FROM item WHERE id=?",(item_id,)).fetchone()
    _ensure_active(it)
    try:
        move_item(item_id, dest['id'], action='PLACE_SHELF', user=getattr(current_user,'username',None))
    except ValueError as e:
        raise

def _place_on_sol_amr(item_id:int, size:str):
    """Choisit un SOL libre compatible (Grand/Petit)"""
    size=size.upper()
    it=item_by_id(item_id); _ensure_active(it)
    slots=free_sol_slots()
    # Choix simple par taille
    picked = None
    for s in slots:
        if s['kind']=='SOL' and s['size']==size:
            picked=s; break
    if not picked:
        raise ValueError(f"Aucun SOL {size} disponible.")
    move_item(item_id, picked['id'], action='PUT_STOCK_AMR', user=getattr(current_user,'username',None))
    @app.route('/work/photo')
    @login_required
    @role_required('photo')
    def work_photo():
        rows=get_db().execute("""
            SELECT i.*, l.code AS loc_code
            FROM item i LEFT JOIN location l ON i.location_id=l.id
            WHERE i.active=1 AND i.status='Attente photo'
            ORDER BY i.created_at ASC
        """).fetchall()
        # Ouverture modale si query params
        choose = request.args.get('choose')  # 'storage' si on force le placement
        item_id = request.args.get('item_id', type=int)
        return render_template('work_photo.html', items=rows, choose=choose, modal_item_id=item_id)

    @app.route('/work/photo/<int:item_id>/ok', methods=['POST'])
    @login_required
    @role_required('photo')
    def photo_ok(item_id):
        """Appui sur 'Photo OK' → ouvrir popup de placement (obligatoire)"""
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo':
            flash("Statut invalide pour Photo OK","error")
            return redirect(url_for('work_photo'))
        # on ne change pas encore le statut ; on force d'abord le placement
        return redirect(url_for('work_photo', choose='storage', item_id=item_id))

    @app.route('/work/photo/place_shelf', methods=['POST'])
    @login_required
    @role_required('photo')
    def photo_place_shelf():
        item_id = int(request.form.get('item_id'))
        level   = int(request.form.get('level'))
        plate   = (request.form.get('plate') or 'A').upper()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo':
            flash("Statut invalide.","error"); return redirect(url_for('work_photo'))
        try:
            _place_on_shelf(item_id, level, plate)
            _movement(item_id, 'PHOTO_DONE')
            _set_status(item_id, _compute_next_after_photo(it))
            flash("Photo OK + Placement étagère effectué.","ok")
        except ValueError as e:
            flash(str(e),'error')
        return redirect(url_for('work_photo'))

    @app.route('/work/photo/place_amr', methods=['POST'])
    @login_required
    @role_required('photo')
    def photo_place_amr():
        item_id = int(request.form.get('item_id'))
        size    = (request.form.get('amr_size') or 'PETIT').upper()
        it=item_by_id(item_id); _ensure_active(it)
        if it['status']!='Attente photo':
            flash("Statut invalide.","error"); return redirect(url_for('work_photo'))
        try:
            _place_on_sol_amr(item_id, size)
            _movement(item_id, 'PHOTO_DONE')
            _set_status(item_id, _compute_next_after_photo(it))
            flash("Photo OK + Placement SOL (AMR) enregistré.","ok")
        except ValueError as e:
            flash(str(e),'error')
        return redirect(url_for('work_photo'))
        @app.route('/work/inspection')
        @login_required
        @role_required('inspection')
        def work_inspection():
            rows=get_db().execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente inspection'
                ORDER BY i.created_at ASC
            """).fetchall()
            ask_pool = (request.args.get('pool')=='ask')
            item_id  = request.args.get('item_id', type=int)
            return render_template('work_inspection.html', items=rows, ask_pool=ask_pool, modal_item_id=item_id)

        @app.route('/work/inspection/<int:item_id>/nok', methods=['POST'])
        @login_required
        @role_required('inspection')
        def inspection_nok(item_id):
            it=item_by_id(item_id); _ensure_active(it)
            if it['status']!='Attente inspection':
                flash("Statut invalide.","error"); return redirect(url_for('work_inspection'))
            _movement(item_id,'INSPECTION_NOK')
            _set_status(item_id,'Prison')
            flash("Inspection NOK → item envoyé en Prison.","ok")
            return redirect(url_for('work_inspection'))

        @app.route('/work/inspection/<int:item_id>/ok', methods=['POST'])
        @login_required
        @role_required('inspection')
        def inspection_ok(item_id):
            it=item_by_id(item_id); _ensure_active(it)
            if it['status']!='Attente inspection':
                flash("Statut invalide.","error"); return redirect(url_for('work_inspection'))
            # Ouvre la popup pool Oui/Non
            return redirect(url_for('work_inspection', pool='ask', item_id=item_id))

        @app.route('/work/inspection/pool', methods=['POST'])
        @login_required
        @role_required('inspection')
        def inspection_pool_decision():
            item_id = int(request.form.get('item_id'))
            choice  = (request.form.get('choice') or 'non').lower()  # 'oui' / 'non'
            it=item_by_id(item_id); _ensure_active(it)
            if it['status']!='Attente inspection':
                flash("Statut invalide.","error"); return redirect(url_for('work_inspection'))
            _movement(item_id,'INSPECTION_OK')
            if choice=='oui':
                log_delete(item_id, user=getattr(current_user,'username',None))
                flash("Inspection OK → Placement en pool → Item supprimé (logique).","ok")
            else:
                _set_status(item_id,'Attente emballage')
                flash("Inspection OK → Attente emballage.","ok")
            return redirect(url_for('work_inspection'))

        @app.route('/work/rac')
        @login_required
        @role_required('rac')
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
        @role_required('rac')
        def rac_ok(item_id):
            it=item_by_id(item_id); _ensure_active(it)
            if it['status']!='Attente RAC':
                flash("Statut invalide.","error"); return redirect(url_for('work_rac'))
            _movement(item_id,'RAC_OK')
            _set_status(item_id,'Attente emballage')
            flash("RAC OK → Attente emballage.","ok")
            return redirect(url_for('work_rac'))

        @app.route('/work/rac/<int:item_id>/nok', methods=['POST'])
        @login_required
        @role_required('rac')
        def rac_nok(item_id):
            it=item_by_id(item_id); _ensure_active(it)
            if it['status']!='Attente RAC':
                flash("Statut invalide.","error"); return redirect(url_for('work_rac'))
            _movement(item_id,'RAC_NOK')
            _set_status(item_id,'Prison')
            flash("RAC NOK → Prison.","ok")
            return redirect(url_for('work_rac'))
            @app.route('/work/emballage')
            @login_required
            @role_required('emballage')
            def work_emballage():
             rows=get_db().execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente emballage'
                ORDER BY i.created_at ASC
            """).fetchall()
            ask_route = (request.args.get('route')=='ask')
            item_id = request.args.get('item_id', type=int)
            return render_template('work_emballage.html', items=rows, ask_route=ask_route, modal_item_id=item_id)

            @app.route('/work/emballage/<int:item_id>/done', methods=['POST'])
            @login_required
            @role_required('emballage')
            def emballage_done(item_id):
                it=item_by_id(item_id); _ensure_active(it)
                if it['status']!='Attente emballage':
                    flash("Statut invalide.","error"); return redirect(url_for('work_emballage'))
                _movement(item_id,'EMBALLAGE_OK')
                # Redirection selon flags
                if it['st_repair']==1 or it['repair_snpa']==1:
                    _set_status(item_id,'Attente expédition client')
                    flash("Emballage terminé → Attente expédition client.","ok")
                    return redirect(url_for('work_emballage'))
                # Sinon, on demande le choix ST vs T2 via modale
                return redirect(url_for('work_emballage', route='ask', item_id=item_id))

            @app.route('/work/emballage/route', methods=['POST'])
            @login_required
            @role_required('emballage')
            def emballage_choose_route():
                item_id = int(request.form.get('item_id'))
                route   = (request.form.get('route') or 'ST').upper()  # 'ST' / 'T2'
                it=item_by_id(item_id); _ensure_active(it)
                if it['status']!='Attente emballage':
                    flash("Statut invalide.","error"); return redirect(url_for('work_emballage'))
                if route=='ST':
                    _set_status(item_id,'Attente expédition ST')
                    flash("Emballage → Attente expédition ST.","ok")
                else:
                    _set_status(item_id,'Attente départ T2')
                    flash("Emballage → Attente départ T2.","ok")
                return redirect(url_for('work_emballage'))

                @app.route('/work/expe')
                @login_required
                @role_required('emballage')  # et/ou 'expedition' au Bloc 3
                def work_expe():
                    db=get_db()
                    client = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente expédition client'
                ORDER BY i.created_at ASC
            """).fetchall()
            st = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente expédition ST'
                ORDER BY i.created_at ASC
            """).fetchall()
            t2 = db.execute("""
                SELECT i.*, l.code AS loc_code
                FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.active=1 AND i.status='Attente départ T2'
                ORDER BY i.created_at ASC
            """).fetchall()
            return render_template('work_expe.html', client=client, st=st, t2=t2)

            @app.route('/work/expe/<int:item_id>/ship', methods=['POST'])
            @login_required
            @role_required('emballage')  # et/ou 'expedition'
            def expe_ship(item_id):
                kind = (request.form.get('kind') or 'client')  # 'client'|'st'|'t2'
                it=item_by_id(item_id); _ensure_active(it)
                valid = {
                    'client':'Attente expédition client',
                    'st':'Attente expédition ST',
                    't2':'Attente départ T2',
                }
                if it['status'] != valid.get(kind):
                    flash("Statut invalide pour expédition.","error"); return redirect(url_for('work_expe'))
                _movement(item_id, f"EXPE_{kind.upper()}")
                log_delete(item_id, user=getattr(current_user,'username',None))
                flash("Expédition effectuée → Item supprimé (logique).","ok")
                return redirect(url_for('work_expe'))
    return app

app=create_app()

if __name__=='__main__':
    app.run(host='0.0.0.0',port=5000,debug=True)
