"""Barcodes Will has taught Food Hub a name for (Food Hub, Sept 2026).

Will's own words: "when something comes up unknown i want to be able to
name it and then next time it comes round it will know what the item is...
building or adding to my own database."

Open Food Facts (and the optional LLM fallback in barcode.py) only know
published products -- a homemade dish, an own-brand item from a small local
shop, a damaged/unreadable label, or anything OFF genuinely has never
catalogued always comes back "not found" and lands in Pending needing a
manual name, on every single scan, forever, unless something remembers the
answer once it's given. This module is that memory: see KnownBarcode in
models/db_models.py for the full story and why Will's own printed "own
item" labels are deliberately excluded.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models.db_models import KnownBarcode
from ..models.food import FoodCategory, FoodItem, StorageType

# A row committed without ever being renamed still carries one of these
# placeholder names (see routers/pending.py); teaching THAT would just cache
# the placeholder forever instead of a real name, so it's refused.
_PLACEHOLDER_PREFIXES = ("Unknown (", "Store barcode (")


def _looks_like_placeholder(name: str) -> bool:
    name = (name or "").strip()
    return not name or name.startswith(_PLACEHOLDER_PREFIXES)


def lookup(barcode: str, db: Session) -> FoodItem | None:
    """A previously-taught FoodItem for ``barcode``, or None.

    Bumps the row's scan_count on a match -- best-effort, never lets a
    bookkeeping failure stop the lookup from returning the item it found."""
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    row = db.query(KnownBarcode).filter(KnownBarcode.barcode == barcode).first()
    if row is None:
        return None
    try:
        row.scan_count = (row.scan_count or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001 - never let this block the lookup
        db.rollback()

    item = FoodItem(
        name=row.name,
        quantity=1.0,
        unit=row.unit or "item",
        brand=row.brand or None,
        confidence=1.0,
    )
    try:
        item.category = FoodCategory(row.category)
    except (ValueError, TypeError):
        item.category = FoodCategory.other
    try:
        item.storage_type = StorageType(row.storage_type)
    except (ValueError, TypeError):
        item.storage_type = StorageType.refrigerated
    if row.default_shelf_life_days:
        item.best_by_date = date.today() + timedelta(days=row.default_shelf_life_days)
        item.best_by_source = "learned"
    return item


def remember(db: Session, barcode: str, *, name: str, brand: str | None,
             category: str | None, storage_type: str | None, unit: str | None,
             best_by_date: str | None, best_by_source: str | None) -> bool:
    """Teach (or re-teach) ``barcode``'s name from a row just committed to
    stock. False (and a no-op) for a name that still looks like the
    unresolved placeholder -- committing without renaming must never poison
    the table with junk. Re-teaching an existing barcode overwrites it, so
    scanning again and correcting the name always sticks."""
    barcode = (barcode or "").strip()
    if not barcode or _looks_like_placeholder(name):
        return False

    days = None
    if best_by_date:
        try:
            days = (date.fromisoformat(best_by_date) - date.today()).days
        except (TypeError, ValueError):
            days = None
        if days is not None and not (0 < days <= 3650):
            days = None  # a stale/backdated/absurd value: offer no date next time

    row = db.query(KnownBarcode).filter(KnownBarcode.barcode == barcode).first()
    if row is None:
        row = KnownBarcode(barcode=barcode, scan_count=0)
        db.add(row)
    row.name = name.strip()
    row.brand = (brand or None)
    row.category = category
    row.storage_type = storage_type
    row.unit = unit or "item"
    if days is not None:
        row.default_shelf_life_days = days
    row.taught_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.commit()
    return True


def forget(db: Session, barcode: str) -> bool:
    """Delete a taught barcode. True if a row was actually removed."""
    barcode = (barcode or "").strip()
    row = db.query(KnownBarcode).filter(KnownBarcode.barcode == barcode).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_all(db: Session) -> list[KnownBarcode]:
    return db.query(KnownBarcode).order_by(KnownBarcode.taught_at.desc()).all()
