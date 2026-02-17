import sqlite3
import os

DB_PATH = "/projects/apply-amr/data/stock.db"

statuses = (
    'Attente douane', 'Attente photo', 'Poste photo',
    'Attente inspection', 'Attente RAC',
    'Prison', 'Attente emballage', 'Emballage',
    'Attente expédition client', 'Attente expédition ST', 'Attente départ T2',
    'STOCK', 'NOGO', 'Supprimé'
)
status_check = ",".join([f"'{s}'" for s in statuses])


print(f"[INFO] Migration forcée de {DB_PATH}")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

# Lire le SQL actuel
row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='item'").fetchone()
if not row:
    raise RuntimeError("La table item n'existe pas !")

sql = row["sql"]

# Détection de l'ancien schéma
old_keywords = ["RECU", "PHOTO", "INSPECTION", "EMBALLAGE", "STOCK"]
needs_migration = all(k in sql for k in old_keywords)

if not needs_migration:
    print("[OK] Migration inutile : le schéma est déjà moderne.")
    exit(0)

print("[MIGRATION] Ancien schéma détecté → migration forcée...")

db.execute("PRAGMA foreign_keys=OFF;")
db.execute("BEGIN;")

db.execute("ALTER TABLE item RENAME TO item_old;")

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

print("[OK] Migration terminée avec succès !")