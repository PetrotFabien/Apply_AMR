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
Attente SAP
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
     - ✅ OUI → `Attente SAP`
     - ❌ NON → `Input ST`

### 7️⃣ **Attente SAP** → Création Avis (`/work/sap`)
- **Entrée**: `Attente SAP`
- **Action**: ✅ SAP Créé → `Réparation SNPA`
- **Rôle**: `admin`, `reception`

### 8️⃣ **Réparation SNPA** → (`/work/repair`)
- **Entrée**: `Réparation SNPA`
- **Décisions**:
  - ✅ **OK** → `Attente inspection`
  - 🚫 **NOGO** → `Exp ST Return` → **FIN**
- **Rôle**: `admin`

### 9️⃣ **Input ST** → Chemin Standard (`/work/input_st`)
- **Entrée**: `Input ST`
- **Action**: ✅ Prêt → `Attente inspection`
- **Rôle**: `admin`

### 🔟 **Inspection** → (`/work/inspection`)
- **Entrée**: `Attente inspection` ou `Attente photo RAC`
- **Décisions**:
  - ✅ **OK** → `Pool`
  - ❌ **NOK** → `Attente photo RAC`
- **RAC Retry**:
  - ✅ **OK** → `Exp Client` → **FIN**
  - ❌ **NOK** → `Attente inspection`
- **Rôle**: `inspection`, `admin`

### 1️⃣1️⃣ **Pool** → Décision Pool (`/work/pool`)
- **Entrée**: `Pool`
- **Décisions**:
  - ✅ **OUI** → `Kardex Input`
  - ❌ **NON** → `Prison`
- **Rôle**: `admin`

### 1️⃣2️⃣ **Prison** → (`/work/prison`)
- **Entrée**: `Prison`
- **Décisions**:
  - 🗑 **Supprimer** → **FIN**
  - 🔄 **Retry** → `Attente inspection`
- **Rôle**: `admin`

### 1️⃣3️⃣ **Kardex & Expédition** → (`/work/kardex`)
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
sap_created          -- Avis SAP créé
pool_ok              -- Validation pool
std_exchange         -- Échange standard
prepa_st_ok          -- Préparation ST validée
```

---

## Routes disponibles

### Création & Douane
- `POST /work/reception` - Créer article + liste `Attente douane`
- `POST /items/<id>/douane_ok` - OK douane
- `POST /items/<id>/douane_nok` - NOK douane

### Réception & Photo
- `GET/POST /work/reception_check` - Contrôle réception
- `POST /items/<id>/reception_ok` - OK réception
- `POST /items/<id>/reception_nok` - NOK réception
- `POST /items/<id>/photo_ok` - OK photo

### RAC & Emballage
- `GET/POST /work/rac` - RAC
- `POST /items/<id>/rac_ok` - OK rac
- `POST /items/<id>/emballage_nogo` - NOGO emballage
- `POST /items/<id>/emballage_repair_decision` - Décision repair

### SAP & Repair
- `GET/POST /work/sap` - Attente SAP
- `POST /items/<id>/sap_created` - SAP créé
- `POST /items/<id>/repair_ok` - OK repair
- `POST /items/<id>/repair_nogo` - NOGO repair

### Inspection & Pool
- `GET/POST /work/inspection` - Inspection
- `POST /items/<id>/inspection_ok` - OK inspection
- `POST /items/<id>/inspection_nok` - NOK inspection
- `POST /items/<id>/rac_retry_ok` - RAC retry OK
- `POST /items/<id>/rac_retry_nok` - RAC retry NOK
- `GET/POST /work/pool` - Pool
- `POST /items/<id>/pool_yes` - Pool OUI
- `POST /items/<id>/pool_no` - Pool NON

### Prison & Kardex
- `GET/POST /work/prison` - Prison
- `POST /items/<id>/prison_delete` - Supprimer
- `POST /items/<id>/prison_retry` - Retry
- `GET/POST /work/kardex` - Kardex
- `POST /items/<id>/kardex_std_exchange` - Échange std
- `POST /items/<id>/prepa_st_decision` - Prepa ST
- `POST /items/<id>/appel_fo_done` - FO done
- `POST /items/<id>/kardex_output_final` - Finaliser

### Input ST
- `GET/POST /work/input_st` - Input ST
- `POST /items/<id>/input_st_done` - Done

---

## Structure de base de données

### Table `item` (colonnes clés)
```sql
id, sku, pn, description, photo_path, size, status,
active, st_repair, repair_snpa, hors_gabarit, location_id,
avis_no, order_no, bl_no, created_at, updated_at,
is_nogo, is_repair_snpa, sap_created, 
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

---

**Dernière mise à jour**: 2026-03-04
