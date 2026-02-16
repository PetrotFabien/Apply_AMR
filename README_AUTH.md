
# Raspi Stock – Auth (SSO + Admin local)

## Fonctionnalités
- SSO OpenID Connect (bouton *SSO*) pour les utilisateurs
- Login administrateur **local** avec mot de passe (chemin `/admin/login`)
- RBAC simple : `user` vs `admin` (accès /admin)
- Table `user` en SQLite (création à la première connexion SSO)

## Variables d'environnement principales
- `SECRET_KEY` (requis en prod)
- `SESSION_COOKIE_SECURE=true` (en prod HTTPS)
- `ADMIN_USERNAME` (par défaut `admin`) / `ADMIN_PASSWORD` (seed admin)
- `SSO_ENABLED=true`
- `OIDC_AUTH_URL`, `OIDC_TOKEN_URL`, `OIDC_USERINFO_URL`
- `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI`
- `OIDC_SCOPE` (par défaut `openid profile email`)

## Redirections
- `/login` → IdP (SSO)
- `/callback` → échange du code + userinfo
- `/logout` → fin de session app
- `/admin/login` → formulaire administrateur local

## Notes
- Si votre IdP "Darwin" est SAML-only, prévoir un connecteur SAML (ex. `python3-saml`). Ici, la stack implémente **OIDC** générique.
- Pensez à déclarer l'URL de redirection (`OIDC_REDIRECT_URI`) côté IdP (ex: `https://votre-app/callback`).
