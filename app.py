import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, g, abort, send_from_directory, flash
from werkzeug.utils import secure_filename
from mir_client import MiRClient
from processus import Item, Location, can_move, next_status_for_location, choose_slot

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DATA_DIR=os.path.join(BASE_DIR,'data')
UPLOAD_DIR=os.path.join(BASE_DIR,'uploads')
os.makedirs(DATA_DIR,exist_ok=True)
os.makedirs(UPLOAD_DIR,exist_ok=True)
DATABASE=os.environ.get('DATABASE',os.path.join(DATA_DIR,'stock.db'))

MIR_AFTER_STOCK=os.getenv('MIR_MISSION_AFTER_STOCK')
ALLOWED_EXTENSIONS={'png','jpg','jpeg','gif','webp'}

def allowed_file(filename:str)->bool:
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

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

    @app.route('/healthz')
    def healthz():
        return {'ok': True}, 200

    @app.route('/readyz')
    def readyz():
        try:
            db=get_db(); db.execute('SELECT 1')
            return {'ready': True}, 200
        except Exception as e:
            return {'ready': False, 'error': str(e)}, 500

    def init_db():
        db=get_db(); db.execute('PRAGMA foreign_keys=ON;')
        # baseline
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
        # migration colonnes
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
        # backfill SOL sizes
        db.execute("""
            UPDATE location SET size='GRAND'
            WHERE kind='SOL' AND (size IS NULL OR size='') AND (code GLOB 'S-A[1-4]' OR code GLOB 'S-B[1-4]')
        """)
        db.execute("""
            UPDATE location SET size='PETIT'
            WHERE kind='SOL' AND (size IS NULL OR size='') AND (
             code GLOB 'S-C[1-6]' OR code GLOB 'S-D[1-6]' OR code GLOB 'S-E[1-6]' OR
             code GLOB 'S-F[1-6]' OR code GLOB 'S-G[1-6]' OR code GLOB 'S-H[1-6]')
        """)
        db.commit()

        # seed if empty
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

        # === NORMALISATION DES EMPLACEMENTS (après seed) ===
        allowed_shelves = [f"ETAGERE-{e}-{s}" for e in (1,2,3) for s in ("A","B","C","D")]
        placeholders = ",".join(["?"]*len(allowed_shelves))
        # Désactiver les étagères non référencées ET vides
        db.execute(f"""
            UPDATE location
               SET active = 0
             WHERE kind = 'ETAGERE'
               AND code NOT IN ({placeholders})
               AND id NOT IN (
                    SELECT l.id
                      FROM location l
                      JOIN item i ON i.location_id = l.id
                     WHERE l.kind='ETAGERE'
               )
        """, allowed_shelves)
        db.commit()
        # Renommer les 12 plateaux standard
        for e in (1,2,3):
            for s in ("A","B","C","D"):
                code=f"ETAGERE-{e}-{s}"
                if e in (1,2):
                    name=f"Étagère {e} (Encours) – Plateau {s}"
                else:
                    name=f"Étagère 3 (NOGO) – Plateau {s}"
                db.execute("UPDATE location SET name=? WHERE code=? AND kind='ETAGERE'", (name, code))
        db.commit()
        # Renommer les SOL selon la taille
        db.execute("""
            UPDATE location
               SET name = 'Grand Chariot ' || code
             WHERE kind='SOL' AND size='GRAND'
        """)
        db.execute("""
            UPDATE location
               SET name = 'Petit Chariot ' || code
             WHERE kind='SOL' AND size='PETIT'
        """)
        db.commit()

    with app.app_context(): init_db()

    # helpers
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
        db=get_db(); it=item_by_id(item_id); dest=location_by_id(to_location_id)
        if not it or not dest: abort(400)
        occ=count_items_in_location(dest['id'])
        ok,msg=can_move(row_to_item(it),row_to_location(dest),occ)
        if not ok: raise ValueError(msg)
        db.execute('INSERT INTO movement(item_id,from_location_id,to_location_id,action,user) VALUES (?,?,?,?,?)',
                   (item_id,it['location_id'],to_location_id,action,user))
        new_status=next_status_for_location(row_to_location(dest)) or it['status']
        db.execute('UPDATE item SET location_id=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',(to_location_id,new_status,item_id))
        db.commit(); return new_status

    @app.route('/')
    def index():
        db=get_db(); kg={'GRAND':{},'PETIT':{}}
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
                else: last=int(row['sku'].split('-')[1]); sku=f'SKU-{last+1:05d}'
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
        except ValueError as e: flash(str(e),'error')
        return redirect(url_for('item_detail',item_id=item_id))

    # Actions rapides
    @app.route('/items/<int:item_id>/send_to_photo',methods=['POST'])
    def send_to_photo(item_id):
        dest=location_by_code('POSTE-PHOTO')
        if not dest: flash('POSTE-PHOTO non configuré','error'); return redirect(url_for('item_detail',item_id=item_id))
        try:
            st=move_item(item_id,dest['id'],'TO_PHOTO'); flash(f'Envoyé au poste PHOTO (statut: {st})','ok')
        except ValueError as e: flash(str(e),'error')
        return redirect(url_for('item_detail',item_id=item_id))

    @app.route('/items/<int:item_id>/send_to_inspection',methods=['POST'])
    def send_to_inspection(item_id):
        dest=location_by_code('POSTE-INSPECTION')
        if not dest: flash('POSTE-INSPECTION non configuré','error'); return redirect(url_for('item_detail',item_id=item_id))
        try:
            st=move_item(item_id,dest['id'],'TO_INSPECTION'); flash(f"Envoyé à l'INSPECTION (statut: {st})",'ok')
        except ValueError as e: flash(str(e),'error')
        return redirect(url_for('item_detail',item_id=item_id))

    @app.route('/items/<int:item_id>/send_to_emballage',methods=['POST'])
    def send_to_emballage(item_id):
        dest=location_by_code('POSTE-EMBALLAGE')
        if not dest: flash('POSTE-EMBALLAGE non configuré','error'); return redirect(url_for('item_detail',item_id=item_id))
        try:
            st=move_item(item_id,dest['id'],'TO_EMBALLAGE'); flash(f"Envoyé à l'EMBALLAGE (statut: {st})",'ok')
        except ValueError as e: flash(str(e),'error')
        return redirect(url_for('item_detail',item_id=item_id))

    @app.route('/work/photo')
    def work_photo():
        db=get_db(); rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('RECU','PHOTO') ORDER BY i.created_at ASC").fetchall()
        return render_template('work_photo.html',items=rows)

    @app.route('/items/<int:item_id>/upload_photo',methods=['POST'])
    def upload_photo(item_id):
        it=item_by_id(item_id)
        if not it: abort(404)
        if 'photo' not in request.files: flash('Aucun fichier reçu','error'); return redirect(url_for('item_detail',item_id=item_id))
        f=request.files['photo']
        if f.filename=='': flash('Fichier non sélectionné','error'); return redirect(url_for('item_detail',item_id=item_id))
        if not allowed_file(f.filename): flash('Extension non autorisée','error'); return redirect(url_for('item_detail',item_id=item_id))
        fname=secure_filename(f.filename); import os as _os
        base,ext=_os.path.splitext(fname)
        from datetime import datetime as _dt
        ts=_dt.utcnow().strftime('%Y%m%d-%H%M%S')
        final=f"{it['id']}_{base}_{ts}{ext}"
        path=_os.path.join(UPLOAD_DIR,final)
        f.save(path)
        db=get_db(); db.execute("UPDATE item SET photo_path=?, status='PHOTO', updated_at=CURRENT_TIMESTAMP WHERE id=?",(final,item_id))
        db.commit(); flash('Photo enregistrée (statut=PHOTO)','ok')
        return redirect(url_for('item_detail',item_id=item_id))

    @app.route('/work/inspection',methods=['GET','POST'])
    def work_inspection():
        db=get_db()
        if request.method=='POST':
            item_id=int(request.form.get('item_id')); result=request.form.get('result')
            dest=location_by_code('POSTE-EMBALLAGE') if result=='OK' else location_by_code('POSTE-INSPECTION')
            try:
                move_item(item_id,dest['id'],f'INSPECT_{result}'); flash('Inspection mise à jour','ok')
            except ValueError as e: flash(str(e),'error')
            return redirect(url_for('work_inspection'))
        rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('INSPECTION') ORDER BY i.created_at ASC").fetchall()
        return render_template('work_inspection.html',items=rows)

    @app.route('/work/emballage',methods=['GET','POST'])
    def work_emballage():
        db=get_db()
        if request.method=='POST':
            item_id=int(request.form.get('item_id')); it=item_by_id(item_id)
            slots=free_sol_slots(); slot_id=request.form.get('slot_id')
            if slot_id:
                slot=location_by_id(int(slot_id)); slot_loc=Location(id=slot['id'],code=slot['code'],kind=slot['kind'],capacity=slot['capacity'],size=slot['size'])
            else:
                slot_loc=choose_slot([Location(id=s['id'],code=s['code'],kind=s['kind'],capacity=s['capacity'],size=s['size']) for s in slots], Item(id=it['id'],sku=it['sku'],size=it['size'],status=it['status'],location_code=None))
                slot=location_by_id(slot_loc.id) if slot_loc else None
            if not slot: flash('Aucun emplacement SOL compatible','error'); return redirect(url_for('work_emballage'))
            try:
                move_item(item_id,slot['id'],'PUT_STOCK')
                db.execute("UPDATE item SET status='STOCK', updated_at=CURRENT_TIMESTAMP WHERE id=?",(item_id,)); db.commit(); flash('Article stocké','ok')
                if MIR_AFTER_STOCK:
                    try: MiRClient().start_mission(MIR_AFTER_STOCK); flash('MiR: mission post-stock envoyée','ok')
                    except Exception as e: flash(f'MiR post-stock: {e}','error')
            except ValueError as e: flash(str(e),'error')
            return redirect(url_for('work_emballage'))
        rows=db.execute("SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('EMBALLAGE') ORDER BY i.created_at ASC").fetchall()
        sol_slots=free_sol_slots(); return render_template('work_emballage.html',items=rows,sol_slots=sol_slots)

    @app.route('/uploads/<path:filename>')
    def uploads(filename): return send_from_directory(UPLOAD_DIR,filename)

    @app.route('/locations')
    def locations():
        db=get_db(); kind=request.args.get('kind'); show_all=(request.args.get('show')=='all')
        base="""
          SELECT l.*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id) AS occ
            FROM location l
        """
        where=[]; params=[]
        if kind in ('SOL','ETAGERE','POSTE'):
            where.append('l.kind = ?'); params.append(kind)
        if not show_all:
            where.append('l.active = 1')
        sql=base
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY l.kind, l.code'
        locs=db.execute(sql, tuple(params)).fetchall()
        return render_template('locations.html', locations=locs, kind=kind, show_all=show_all)

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
    def mir_dashboard(): return render_template('mir_dashboard.html')

    return app

app=create_app()
if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=True)
