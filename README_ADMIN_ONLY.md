
# Raspi Stock – Admin-Only Authentication (sans SSO)

## Fonctionnalités
- Connexion **locale** (username/mot de passe) pour tous les utilisateurs
- Rôles: `user` et `admin` (l’admin gère les comptes via /admin/users)
- Seed automatique d’un **admin local** si absent
- Normalisation des étagères (E1/E2 encours, E3 NOGO), règles SOL grand/petit
- Endpoints santé: `/healthz` et `/readyz`

## Variables d'environnement
- `SECRET_KEY` (requis en prod)
- `SESSION_COOKIE_SECURE=true` (si HTTPS)
- `ADMIN_USERNAME` (défaut: `admin`)
- `ADMIN_PASSWORD` (seed admin – conseillé en prod)
- `MIR_DRY_RUN=true` (pour simuler le robot MiR)

## Utilisation locale
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD="ChangeMe!"
python app.py
# → http://localhost:5000
# Connexion: identifiant/mot de passe
# Admin: menu en haut à droite si rôle=admin
```
