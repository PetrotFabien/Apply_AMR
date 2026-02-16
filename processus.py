from dataclasses import dataclass
from typing import Optional, Tuple, List

@dataclass
class Item:
    id: int
    sku: str
    size: Optional[str]   # 'GRAND' | 'PETIT' | None
    status: str           # RECU, PHOTO, INSPECTION, EMBALLAGE, STOCK, NOGO
    location_code: Optional[str]

@dataclass
class Location:
    id: int
    code: str             # e.g. S-A1, POSTE-PHOTO, ETAGERE-1-A
    kind: str             # 'SOL' | 'POSTE' | 'ETAGERE'
    capacity: Optional[int]
    size: Optional[str]   # for SOL: 'GRAND'|'PETIT', else None

def can_move(item: Item, location: Location, occupied_count: int = 0) -> Tuple[bool, str]:
    # NOGO strict: seules les pièces NOGO sur ETAGERE-3-*
    if location.code.startswith('ETAGERE-3-') and item.status != 'NOGO':
        return False, "Seuls les items en statut NOGO peuvent être placés sur l'étagère 3."
    # Capacité
    if location.capacity is not None and occupied_count >= location.capacity:
        return False, f"{location.code} est déjà occupé"
    # Taille sur SOL
    if location.kind == 'SOL':
        if not item.size:
            return False, "La taille du chariot (GRAND/PETIT) est inconnue."
        if location.size != item.size:
            return False, f"Le chariot {item.size} ne peut pas aller sur {location.code} (attendu: {location.size})."
    return True, "OK"

def next_status_for_location(location: Location) -> Optional[str]:
    if location.code == 'POSTE-PHOTO':
        return 'PHOTO'
    if location.code == 'POSTE-INSPECTION':
        return 'INSPECTION'
    if location.code == 'POSTE-EMBALLAGE':
        return 'EMBALLAGE'
    if location.code.startswith('ETAGERE-3-'):
        return 'NOGO'
    if location.kind == 'SOL':
        return 'STOCK'
    return None

def choose_slot(slots: List[Location], item: Item) -> Optional[Location]:
    if item.size == 'GRAND':
        for loc in slots:
            if loc.kind == 'SOL' and loc.size == 'GRAND':
                return loc
    if item.size == 'PETIT':
        for loc in slots:
            if loc.kind == 'SOL' and loc.size == 'PETIT':
                return loc
    return None
