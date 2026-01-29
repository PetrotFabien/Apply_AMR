
# Raspi Stock – Développement local (Visual Studio Code)

## Prérequis
- Python 3.10+ installé sur ta machine
- Visual Studio Code + extension **Python**

## Installation
1. Clone/dézippe ce projet.
2. Crée un environnement virtuel et installe les dépendances :
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Lancer
- Méthode 1 (terminal) :
```bash
python app.py
```
- Méthode 2 (VS Code) : **Run & Debug** → *Flask (app.py)*.

L'application est disponible sur http://localhost:5000

## Structure
- `app.py` : logique Flask + SQLite
- `templates/` : vues Jinja2
- `static/style.css` : style minimal
- `uploads/` : photos uploadées

## Notes
- La base SQLite est créée automatiquement dans `data/stock.db`.
- Les emplacements sont seedés : `SOL-01..44`, `ETG-01..50`, `POSTE-*`.
