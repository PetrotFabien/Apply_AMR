
# Raspi Stock – Intégration MiR (Dry-Run inclus)

## Lancer en local
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Ouvre http://localhost:5000

## Variables d'environnement
- MIR_BASE_URL (ex: http://192.168.0.50/api/v2.0)
- MIR_USER, MIR_PASS
- MIR_DRY_RUN=true/false
- MIR_MISSION_PHOTO / MIR_MISSION_INSPECTION / MIR_MISSION_EMBALLAGE
- MIR_MISSION_AFTER_STOCK (optionnelle)

## OpenShift (exemple)
```bash
oc create secret generic mir-secret   --from-literal=MIR_BASE_URL="http://192.168.0.50/api/v2.0"   --from-literal=MIR_USER="miruser"   --from-literal=MIR_PASS="mirpassword"
oc set env deployment/raspi-stock --from=secret/mir-secret
oc set env deployment/raspi-stock MIR_DRY_RUN=true
```

## Remarques
- SQLite ⇒ 1 seul replica.
- Probes à ajouter côté OpenShift (readiness/liveness) selon ton endpoint préféré (/ ou /health).
