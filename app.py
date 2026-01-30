
import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, g, abort, send_from_directory, flash
from mir_client import MiRClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATABASE = os.environ.get("DATABASE", os.path.join(DATA_DIR, "stock.db"))

# Mapping missions par poste (à adapter avec tes GUID réels)
MIR_MISSIONS = {
    "POSTE-PHOTO":      os.getenv("MIR_MISSION_PHOTO",      "11111111-2222-3333-4444-555555555555"),
    "POSTE-INSPECTION": os.getenv("MIR_MISSION_INSPECTION", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    "POSTE-EMBALLAGE":  os.getenv("MIR_MISSION_EMBALLAGE",  "99999999-8888-7777-6666-555555555555"),
}
MIR_AFTER_STOCK = os.getenv("MIR_MISSION_AFTER_STOCK")  # optionnel : mission après mise en stock


def create_app():
    app = Flask(__name__)
    app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db():
        db = get_db()
        db.executescript(
            """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS location (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('SOL','ETAGERE','POSTE')),
            capacity INTEGER,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            description TEXT,
            photo_path TEXT,
            status TEXT NOT NULL CHECK(status IN ('RECU','PHOTO','INSPECTION','EMBALLAGE','STOCK')),
            location_id INTEGER,
            avis_no TEXT,
            order_no TEXT,
            bl_no TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(location_id) REFERENCES location(id) ON DELETE SET NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_item_updated
        AFTER UPDATE ON item
        FOR EACH ROW
        BEGIN
            UPDATE item SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
        END;
        CREATE TABLE IF NOT EXISTS movement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            from_location_id INTEGER,
            to_location_id INTEGER,
            action TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user TEXT,
            FOREIGN KEY(item_id) REFERENCES item(id) ON DELETE CASCADE,
            FOREIGN KEY(from_location_id) REFERENCES location(id),
            FOREIGN KEY(to_location_id) REFERENCES location(id)
        );
            """
        )
        db.commit()
        for col in ("avis_no", "order_no", "bl_no"):
            try:
                db.execute(f"ALTER TABLE item ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        db.execute("CREATE INDEX IF NOT EXISTS idx_item_avis ON item(avis_no)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_item_order ON item(order_no)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_item_bl ON item(bl_no)")
        db.commit()
        count = db.execute("SELECT COUNT(*) AS c FROM location").fetchone()["c"]
        if count == 0:
            for i in range(1, 45):
                code = f"SOL-{i:02d}"
                db.execute("INSERT INTO location(code,name,kind,capacity) VALUES (?,?, 'SOL', 1)", (code, f"Emplacement {code}"))
            for i in range(1, 51):
                code = f"ETG-{i:02d}"
                db.execute("INSERT INTO location(code,name,kind,capacity) VALUES (?,?, 'ETAGERE', 1)", (code, f"Etagère {code}"))
            posts = [("POSTE-PHOTO", "Poste Photo"),("POSTE-INSPECTION", "Poste Inspection"),("POSTE-EMBALLAGE", "Poste Emballage")]
            for code, name in posts:
                db.execute("INSERT INTO location(code,name,kind,capacity) VALUES (?,?, 'POSTE', NULL)", (code, name))
            db.commit()

    with app.app_context():
        init_db()

    def location_by_code(code):
        return get_db().execute("SELECT * FROM location WHERE code=?", (code,)).fetchone()

    def item_by_id(item_id):
        return get_db().execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()

    def count_items_in_location(loc_id):
        return get_db().execute("SELECT COUNT(*) AS c FROM item WHERE location_id=?", (loc_id,)).fetchone()["c"]

    def first_free_slot(kind):
        db = get_db()
        return db.execute(
            """
            SELECT l.* FROM location l
            LEFT JOIN item it ON it.location_id = l.id
            WHERE l.kind=? AND l.capacity=1 AND l.active=1
            GROUP BY l.id HAVING COUNT(it.id)=0
            ORDER BY l.code ASC
            """, (kind,)
        ).fetchone()

    def free_slots(kind):
        db = get_db()
        return db.execute(
            """
            SELECT l.id, l.code, l.name FROM location l
            LEFT JOIN item it ON it.location_id = l.id
            WHERE l.kind=? AND l.capacity=1 AND l.active=1
            GROUP BY l.id HAVING COUNT(it.id)=0
            ORDER BY l.code ASC
            """, (kind,)
        ).fetchall()

    def move_item(item_id, to_location_id, action="MOVE", user=None):
        db = get_db()
        it = item_by_id(item_id)
        if not it: abort(404)
        loc = db.execute("SELECT * FROM location WHERE id=?", (to_location_id,)).fetchone()
        if not loc: abort(400)
        if loc["capacity"] == 1 and count_items_in_location(loc["id"]) >= 1:
            raise ValueError(f"Emplacement {loc['code']} occupé")
        db.execute("INSERT INTO movement(item_id, from_location_id, to_location_id, action, user) VALUES (?,?,?,?,?)",
                   (item_id, it["location_id"], to_location_id, action, user))
        new_status = it["status"]
        if loc["kind"] == "POSTE":
            if loc["code"] == "POSTE-PHOTO": new_status = "PHOTO"
            elif loc["code"] == "POSTE-INSPECTION": new_status = "INSPECTION"
            elif loc["code"] == "POSTE-EMBALLAGE": new_status = "EMBALLAGE"
        db.execute("UPDATE item SET location_id=?, status=? WHERE id=?", (to_location_id, new_status, item_id))
        db.commit()

    @app.route("/")
    def index():
        db = get_db()
        kpi = {}
        for kind in ("SOL", "ETAGERE"):
            total = db.execute("SELECT COUNT(*) AS c FROM location WHERE kind=?", (kind,)).fetchone()["c"]
            occ = db.execute("""SELECT COUNT(*) AS c FROM item i JOIN location l ON i.location_id=l.id WHERE l.kind=? AND l.capacity=1""", (kind,)).fetchone()["c"]
            kpi[kind] = {"total": total, "occupied": occ, "free": total - occ}
        statuses = db.execute("SELECT status, COUNT(*) AS c FROM item GROUP BY status").fetchall()
        return render_template("index.html", kpi=kpi, statuses=statuses)

    @app.route("/items", methods=["GET", "POST"])
    def items():
        db = get_db()
        if request.method == "POST":
            sku = (request.form.get("sku") or "").strip()
            if not sku:
                row = db.execute("SELECT sku FROM item WHERE sku LIKE 'SKU-%' ORDER BY CAST(SUBSTR(sku,5) AS INTEGER) DESC LIMIT 1").fetchone()
                if row is None: sku = "SKU-00001"
                else:
                    last_num = int(row["sku"].split('-')[1]); sku = f"SKU-{last_num+1:05d}"
            desc = (request.form.get("description") or "").strip()
            avis_no = (request.form.get("avis_no") or "").strip()
            order_no = (request.form.get("order_no") or "").strip()
            bl_no = (request.form.get("bl_no") or "").strip()
            db.execute("""
                INSERT INTO item(sku, description, avis_no, order_no, bl_no, status, location_id)
                VALUES (?,?,?,?,?, 'RECU', NULL)
            """, (sku, desc, avis_no or None, order_no or None, bl_no or None))
            new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.commit()
            flash(f"Article créé avec SKU : {sku}", "ok")
            return redirect(url_for("item_detail", item_id=new_id))
        q = (request.args.get("q") or "").strip()
        if q:
            rows = db.execute(
                """
                SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id
                WHERE i.sku LIKE ? OR i.description LIKE ? OR i.avis_no LIKE ? OR i.order_no LIKE ? OR i.bl_no LIKE ?
                ORDER BY i.created_at DESC
                """, (f"%{q}%",)*5
            ).fetchall()
        else:
            rows = db.execute("""SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id ORDER BY i.created_at DESC LIMIT 200""").fetchall()
        return render_template("items.html", items=rows, q=q)

    @app.route("/items/<int:item_id>")
    def item_detail(item_id):
        db = get_db()
        it = item_by_id(item_id)
        if not it: abort(404)
        loc = db.execute("SELECT * FROM location WHERE id=?", (it["location_id"],)).fetchone() if it["location_id"] else None
        moves = db.execute(
            """SELECT m.*, lf.code AS from_code, lt.code AS to_code FROM movement m
                 LEFT JOIN location lf ON m.from_location_id=lf.id LEFT JOIN location lt ON m.to_location_id=lt.id
                 WHERE m.item_id=? ORDER BY m.created_at DESC""", (item_id,)
        ).fetchall()
        sol_free = free_slots("SOL"); etg_free = free_slots("ETAGERE")
        return render_template("item_detail.html", it=it, loc=loc, moves=moves, sol_free=sol_free, etg_free=etg_free)

    @app.route("/items/<int:item_id>/move", methods=["POST"])
    def move(item_id):
        to_id = int(request.form.get("to_location_id"))
        try:
            move_item(item_id, to_id, action="MOVE"); flash("Mouvement effectué.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/items/<int:item_id>/update_refs", methods=["POST"])
    def update_refs(item_id):
        db = get_db(); it = item_by_id(item_id)
        if not it: abort(404)
        avis_no = (request.form.get("avis_no") or "").strip() or None
        order_no = (request.form.get("order_no") or "").strip() or None
        bl_no = (request.form.get("bl_no") or "").strip() or None
        db.execute("UPDATE item SET avis_no=?, order_no=?, bl_no=? WHERE id=?", (avis_no, order_no, bl_no, item_id))
        db.commit(); flash("Références mises à jour.", "ok")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/work/photo", methods=["GET"])
    def work_photo():
        db = get_db()
        rows = db.execute("""SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('RECU','PHOTO') ORDER BY i.created_at ASC""").fetchall()
        return render_template("work_photo.html", items=rows)

    @app.route("/work/photo/<int:item_id>/send", methods=["POST"])
    def send_to_photo(item_id):
        poste = location_by_code("POSTE-PHOTO"); move_item(item_id, poste["id"], action="TO_PHOTO"); flash("Envoyé au poste Photo.", "ok")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/work/photo/<int:item_id>/upload", methods=["POST"])
    def upload_photo(item_id):
        file = request.files.get("photo")
        if not file or file.filename == "": flash("Aucun fichier donné.", "error"); return redirect(url_for("item_detail", item_id=item_id))
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]: flash("Format non supporté (jpg, png, webp).", "error"); return redirect(url_for("item_detail", item_id=item_id))
        fname = f"item_{item_id}_{int(datetime.now().timestamp())}{ext}"; path = os.path.join(UPLOAD_DIR, fname)
        file.save(path); db = get_db(); db.execute("UPDATE item SET photo_path=?, status='PHOTO' WHERE id=?", (fname, item_id)); db.commit(); flash("Photo enregistrée.", "ok")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/uploads/<path:filename>")
    def uploads(filename):
        return send_from_directory(UPLOAD_DIR, filename)

    @app.route("/work/inspection", methods=["GET", "POST"])
    def work_inspection():
        db = get_db()
        if request.method == "POST":
            item_id = int(request.form.get("item_id")); result = request.form.get("result")
            if result == "OK":
                poste = location_by_code("POSTE-EMBALLAGE"); move_item(item_id, poste["id"], action="INSPECT_OK"); flash("Inspection OK → vers Emballage.", "ok")
            else:
                poste = location_by_code("POSTE-INSPECTION"); move_item(item_id, poste["id"], action="INSPECT_NOK"); flash("Inspection NOK (reste au poste).", "error")
            return redirect(url_for("work_inspection"))
        rows = db.execute("""SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('INSPECTION') ORDER BY i.created_at ASC""").fetchall()
        return render_template("work_inspection.html", items=rows)

    @app.route("/work/emballage", methods=["GET", "POST"])
    def work_emballage():
        db = get_db()
        if request.method == "POST":
            item_id = int(request.form.get("item_id")); target_kind = request.form.get("target_kind"); slot_id = request.form.get("slot_id")
            if not slot_id:
                slot = first_free_slot(target_kind)
                if not slot: flash(f"Aucun emplacement {target_kind} libre.", "error"); return redirect(url_for("work_emballage"))
                slot_id = slot["id"]
            try:
                move_item(item_id, int(slot_id), action="PUT_STOCK"); db.execute("UPDATE item SET status='STOCK' WHERE id=?", (item_id,)); db.commit(); flash("Article stocké.", "ok")
                if MIR_AFTER_STOCK:
                    try:
                        MiRClient().start_mission(MIR_AFTER_STOCK)
                        flash("MiR: mission post-stock envoyée.", "ok")
                    except Exception as e:
                        flash(f"MiR post-stock: {e}", "error")
            except ValueError as e:
                flash(str(e), "error")
            return redirect(url_for("work_emballage"))
        rows = db.execute("""SELECT i.*, l.code AS loc_code FROM item i LEFT JOIN location l ON i.location_id=l.id WHERE i.status IN ('EMBALLAGE') ORDER BY i.created_at ASC""").fetchall()
        sol_slots = free_slots("SOL"); etg_slots = free_slots("ETAGERE")
        return render_template("work_emballage.html", items=rows, sol_slots=sol_slots, etg_slots=etg_slots)

    @app.route("/items/<int:item_id>/to/<string:poste_code>", methods=["POST"])
    def to_poste(item_id, poste_code):
        poste = location_by_code(poste_code)
        if not poste or poste["kind"] != "POSTE": abort(400)
        move_item(item_id, poste["id"], action=f"TO_{poste_code}"); flash(f"Envoyé à {poste['name']}", "ok")
        mission_guid = MIR_MISSIONS.get(poste_code)
        if mission_guid:
            try:
                MiRClient().start_mission(mission_guid)
                flash(f"MiR: mission envoyée ({poste['name']}).", "ok")
            except Exception as e:
                flash(f"MiR: échec envoi mission ({e})", "error")
        return redirect(url_for("item_detail", item_id=item_id))

    @app.route("/locations")
    def locations():
        db = get_db(); kind = request.args.get("kind")
        if kind in ("SOL", "ETAGERE", "POSTE"):
            locs = db.execute("""SELECT l.*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id) AS occ FROM location l WHERE kind=? ORDER BY code""", (kind,)).fetchall()
        else:
            locs = db.execute("""SELECT l.*, (SELECT COUNT(*) FROM item i WHERE i.location_id=l.id) AS occ FROM location l ORDER BY kind, code""").fetchall()
        return render_template("locations.html", locations=locs, kind=kind)

    # === API MiR pour UI ===
    @app.route("/api/mir/status")
    def api_mir_status():
        try: return MiRClient().status(), 200
        except Exception as e: return {"error": str(e)}, 502

    @app.route("/api/mir/missions")
    def api_mir_missions():
        try: return {"missions": MiRClient().missions()}, 200
        except Exception as e: return {"error": str(e)}, 502

    @app.route("/api/mir/mission/<string:guid>", methods=["POST"])
    def api_mir_start(guid):
        try: return {"ok": True, "result": MiRClient().start_mission(guid)}, 200
        except Exception as e: return {"ok": False, "error": str(e)}, 502

    @app.route("/mir")
    def mir_dashboard():
        return render_template("mir_dashboard.html")

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
