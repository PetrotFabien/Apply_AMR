# Raspi Stock – BLOC 1 (workflow strict)

- Pages opérateurs activées : Douane / Photo / Inspection / RAC / Emballage / Expédition
- Transitions strictes + mouvements + suppression logique
- Placement obligatoire après Photo (Étagère / AMR)
- Style Material (couleurs vives)

## Lancer
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD=ChangeMe!
python app.py
```
