from dataclasses import dataclass
from typing import Optional, Tuple, List

@dataclass
class Item:
    id: int
    sku: str
    size: Optional[str]
    status: str
    location_code: Optional[str]

@dataclass
class Location:
    id: int
    code: str
    kind: str
    capacity: Optional[int]
    size: Optional[str]

def can_move(item: Item, location: Location, occupied_count: int = 0) -> Tuple[bool, str]:
    # Étageres 3 = zone NOGO réservée
    if location.code.startswith('ETAGERE-3-') and item.status != 'NOGO':
        return False, "Seuls les items en statut NOGO peuvent être placés sur l'étagère 3."
    if location.capacity is not None and occupied_count >= location.capacity:
        return False, f"{location.code} est déjà occupé"
    if location.kind == 'SOL':
        if not item.size:
            return False, "La taille du chariot (GRAND/PETIT) est inconnue."
        if location.size != item.size:
            return False, f"Le chariot {item.size} ne peut pas aller sur {location.code} (attendu: {location.size})."
    return True, "OK"

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
