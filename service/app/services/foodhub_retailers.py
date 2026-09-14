"""Retailer seeding and lookup helpers (Food Hub, FoodHub-0002, brief 3.4).

Retailer is a small, optional Food Hub-owned table: "which shop was this
bought from". It is never required anywhere it appears (nullable FK, no UI
gate before scanning/committing an item), matching the brief's "do not make
retailer mandatory" instruction. This module holds the first-run seed list
and the "which retailer does this barcode/product usually come from" lookup
used by the Manage page's optional "Bought From" suggestion.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models.db_models import ProductRetailer, Retailer

# Seed list from the brief. Top-up (like services.defaults.seed_defaults),
# never all-or-nothing, so a name added here later still reaches an existing
# install without disturbing a household's own edits (renames, reordering,
# soft-hides) to the retailers they already have.
_SEED_RETAILERS = [
    "ASDA", "Tesco", "Farmfoods", "Aldi", "Lidl", "Iceland", "Morrisons",
    "Sainsbury's", "Other",
]


def seed_retailers(db: Session) -> None:
    """Insert any seed retailer not already present (matched case-insensitively
    by name). Safe to call on every startup."""
    existing = {r.name.strip().lower() for r in db.query(Retailer).all()}
    added = False
    for i, name in enumerate(_SEED_RETAILERS):
        if name.strip().lower() in existing:
            continue
        db.add(Retailer(name=name, sort_order=i, active=1))
        added = True
    if added:
        db.commit()


def suggest_retailer(db: Session, barcode: str | None,
                      grocy_product_id: int | None = None) -> dict | None:
    """The most likely retailer for a barcode/product, or None.

    History-based only (brief 3.4's "own-brand barcode prefix heuristics" has
    no reliable general-purpose mapping to draw on outside GS1's own store-
    local ranges, which barcode.is_store_local_barcode already handles for a
    different purpose -- inventing a static prefix->retailer table here would
    be guessing, not a heuristic, so this deliberately sticks to real history:
    "you bought this exact barcode/product from X before"). Ties broken by
    most-recently-seen. Returns {"retailer_id", "name", "times_seen"} or None
    when nothing is known yet.
    """
    q = db.query(ProductRetailer)
    if barcode:
        by_barcode = (q.filter(ProductRetailer.barcode == barcode)
                      .order_by(ProductRetailer.times_seen.desc(),
                               ProductRetailer.last_seen.desc()).first())
        if by_barcode:
            retailer = db.query(Retailer).filter(
                Retailer.id == by_barcode.retailer_id).first()
            if retailer and retailer.active:
                return {"retailer_id": retailer.id, "name": retailer.name,
                        "times_seen": by_barcode.times_seen}
    if grocy_product_id:
        by_product = (db.query(ProductRetailer)
                      .filter(ProductRetailer.grocy_product_id == grocy_product_id)
                      .order_by(ProductRetailer.times_seen.desc(),
                               ProductRetailer.last_seen.desc()).first())
        if by_product:
            retailer = db.query(Retailer).filter(
                Retailer.id == by_product.retailer_id).first()
            if retailer and retailer.active:
                return {"retailer_id": retailer.id, "name": retailer.name,
                        "times_seen": by_product.times_seen}
    return None


def record_purchase(db: Session, retailer_id: int, barcode: str | None = None,
                     grocy_product_id: int | None = None) -> None:
    """Remember that ``barcode``/``grocy_product_id`` was bought from
    ``retailer_id`` (bumps times_seen on a repeat, else creates the row).

    Never raises past a bad id: a Retailer that no longer exists (deleted,
    not just soft-hidden) is silently skipped rather than failing the commit
    it rides along with -- this is bookkeeping, not the stock write itself.
    """
    if not retailer_id or not (barcode or grocy_product_id):
        return
    if not db.query(Retailer).filter(Retailer.id == retailer_id).first():
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q = db.query(ProductRetailer).filter(ProductRetailer.retailer_id == retailer_id)
    row = None
    if barcode:
        row = q.filter(ProductRetailer.barcode == barcode).first()
    if row is None and grocy_product_id:
        row = q.filter(ProductRetailer.grocy_product_id == grocy_product_id).first()
    if row:
        row.times_seen = (row.times_seen or 0) + 1
        row.last_seen = now
        if grocy_product_id and not row.grocy_product_id:
            row.grocy_product_id = grocy_product_id
        if barcode and not row.barcode:
            row.barcode = barcode
    else:
        db.add(ProductRetailer(barcode=barcode, grocy_product_id=grocy_product_id,
                               retailer_id=retailer_id, times_seen=1, last_seen=now))
    db.commit()


def match_store_name(db: Session, store_name: str | None) -> Retailer | None:
    """Fuzzy-match a receipt's OCR'd store string to a known Retailer.

    Reuses the same tolerant-matching idea services/receipt.py already applies
    to product lines (SequenceMatcher, not an exact string compare), since a
    receipt might read "Tesco" as "TESC0 STORES 2431" or similar. Returns None
    below the confidence bar rather than mistag a purchase.
    """
    if not store_name or not store_name.strip():
        return None
    import difflib
    needle = store_name.strip().lower()
    best: tuple[float, Retailer | None] = (0.0, None)
    for retailer in db.query(Retailer).filter(Retailer.active == 1).all():
        name = retailer.name.strip().lower()
        # A direct substring match (either direction) covers the common case
        # ("tesco" in "tesco stores 2431 ltd") without needing a high ratio.
        if name in needle or needle in name:
            score = 0.9
        else:
            score = difflib.SequenceMatcher(None, name, needle).ratio()
        if score > best[0]:
            best = (score, retailer)
    if best[0] >= 0.6:
        return best[1]
    return None
