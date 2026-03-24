# Flux de Processus – Raspi Stock V2

## Architecture complète du workflow

Ce document détaille le flux complet implémenté basé sur le diagramme Visio.

---

## Statuts validés

```
Attente douane
Attente réception
Poste photo
Attente RAC
Poste emballage
Réparation SNPA
Attente inspection
Attente photo RAC
Pool
Prison
Kardex Input
Kardex Output
Préparation ST
Appel FO
Échange Standard
Exp Externe
Exp ST Return
Exp Client
Dossier Induction
STOCK, NOGO, Supprimé
```

---

## Étapes du Processus

### 1️⃣ **Réception** → Input Client (`/work/reception`)
- **Statut initial**: Article créé en `Attente douane`
- **Rôle**: `reception`, `admin`
- **Données**: Référence (Avis N° ou SKU), PN, Description, Taille, Avis, Commande, BL
- **Flags**: st_repair, repair_snpa

### 2️⃣ **Contrôle Douane** → (`/work/douane`)
- **Entrée**: `Attente douane`
- **Décisions**:
  - ✅ **OK** → `Attente réception`
  - ❌ **NOK** → Retour `Attente douane`
- **Rôle**: `douane`, `admin`

### 3️⃣ **Contrôle Réception** → (`/work/reception_check`)
- **Entrée**: `Attente réception`
- **Décisions**:
  - ✅ **OK** → `Poste photo`
  - ❌ **NOK** → Retour `Attente réception`
- **Rôle**: `reception`, `admin`

### 4️⃣ **Poste Photo** → (`/work/photo`)
- **Entrée**: `Poste photo`
- **Action**: ✅ OK → `Attente RAC`
- **Rôle**: `photo`, `admin`

### 5️⃣ **Attente RAC** → (`/work/rac`)
- **Entrée**: `Attente RAC`
- **Action**: ✅ OK → `Poste emballage`
- **Rôle**: `rac`, `admin`

### 6️⃣ **Poste Emballage** → Décisions majeure (`/work/emballage`)
- **Entrée**: `Poste emballage`
- **Décisions**:
  1. **🚫 NOGO** → `Exp ST Return` → **FIN**
  2. **🔧 Repair SNPA?**
     - ✅ OUI → `Réparation SNPA`
     - ❌ NON → `Input ST`

### 7️⃣ **Réparation SNPA** → (`/work/repair`)
- **Entrée**: `Réparation SNPA`
- **Décisions**:
  - ✅ **OK** → `Attente inspection`
  - 🚫 **NOGO** → `Exp ST Return` → **FIN**
- **Rôle**: `admin`

### 8️⃣ **Input ST** → Chemin Standard (`/work/input_st`)
- **Entrée**: `Input ST`
- **Action**: ✅ Prêt → `Attente inspection`
- **Rôle**: `admin`

### 9️⃣ **Inspection** → (`/work/inspection`)
- **Entrée**: `Attente inspection` ou `Attente photo RAC`
- **Décisions**:
  - ✅ **Pool OUI** → `Kardex Input`
  - ✅ **Pool NON** → `Prison` (mise en stock spécifique)
  - ❌ **NOK** → `Attente photo RAC`
- **RAC Retry**:
  - ✅ **OK** → `Exp Client` → **FIN**
  - ❌ **NOK** → `Attente inspection`
- **Prison (depuis Pool NON)**:
  - 🗑 **Supprimer** → **FIN**
  - 🔄 **Retry** → `Attente inspection`
- **Rôle**: `inspection`, `admin`
- **Note**: Pool et Prison sont des décisions intégrées au processus d'Inspection, ce ne sont pas des étapes indépendantes avec leur propre plan de charge.

### 🔟 **Kardex & Expédition** → (`/work/kardex`)
- **Chemin 1: Kardex Input**
  - ✅ Échange Standard? → `Exp Externe` → **FIN**
  - ❌ → `Préparation ST`
  
- **Chemin 2: Préparation ST**
  - ✅ Prepa OK? → `Appel FO`
  - ❌ → `Kardex Output`
  
- **Chemin 3: Appel FO**
  - Complete → `Kardex Output`
  
- **Chemin 4: Kardex Output**
  - ✅ Finaliser → `STOCK` (archive, active=0) → **FIN**

- **Rôle**: `admin`

---

## Flags de Décision (Colonnes ajoutées)

```sql
is_nogo              -- Article NOGO (redirection exp ST)
is_repair_snpa       -- Article pour repair SNPA
pool_ok              -- Validation pool
std_exchange         -- Échange standard
prepa_st_ok          -- Préparation ST validée
```

---

## Pages de charge de travail

### Visualisation des charges
- `GET /workload/douane` - Charge Douane (rôles: `douane`, `admin`)
- `GET /workload/photo` - Charge Photo (rôles: `photo`, `admin`)
- `GET /workload/inspection` - Charge Inspection (rôles: `inspection`, `admin`)
- `GET /workload/rac` - Charge RAC (rôles: `rac`, `admin`)
- `GET /workload/emballage` - Charge Emballage (rôles: `emballage`, `admin`)
- `GET /workload/expe` - Charge Expédition (rôles: `expedition`, `admin`)
- `GET /workload/repair` - Charge Repair (rôles: `admin`)
- `GET /workload/kardex` - Charge Kardex (rôles: `admin`)
- `GET /workload/input_st` - Charge Input ST (rôles: `admin`)
- `GET /workload/nogo` - Charge NOGO (rôles: `admin`)

### Autres pages
- `GET /manager_dashboard` - Dashboard Manager (rôles: `manager`, `admin`)
- `GET /robot_status` - Statut Robot MiR (rôles: `admin`, `photo`, `inspection`, `emballage`)
- `GET /admin_users` - Gestion Utilisateurs (rôles: `admin`)
- `GET /archives` - Archives (rôles: `admin`, `emballage`, `inspection`)

---

## Structure de base de données

### Table `item` (colonnes clés)
```sql
id, sku, pn, description, photo_path, size, status,
active, st_repair, repair_snpa, hors_gabarit, location_id,
avis_no, order_no, bl_no, created_at, updated_at,
is_nogo, is_repair_snpa, 
pool_ok, std_exchange, prepa_st_ok
```

### Table `movement`
```sql
id, item_id, from_location_id, to_location_id, action, created_at, user
```

---

## Migration

La BD s'auto-initialise avec :
1. Création des tables si absentes
2. Ajout des colonnes manquantes (ALTER TABLE)
3. Migration forcée pour ancien schéma

---

## Notes

- **Suppressions logiques**: Les items finalisés gardent `active=0` et le statut `STOCK` ou `Supprimé`
- **Historique**: Tous les changements sont tracés dans la table `movement`
- **Part Number (PN)**: Champ textuel pour identifier les articles
- **Boucles**: Plusieurs niveaux de retry (photo RAC, prison, etc.)
- **État actuel**: Les routes de traitement (`/work/*`) retournent 404. L'application affiche désormais les charges de travail via les pages `/workload/*` pour visualisation uniquement.

---

**Dernière mise à jour**: 2026-03-23
