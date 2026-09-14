"""Food Hub-only API surface (FoodHub-0002), namespaced under /foodhub/ so it
stays obviously separate from core Pantry Raider routes across an upstream
merge (see FOODHUB_CHANGES.md). Covers:

  - Retailers: CRUD + "suggest a retailer for this barcode" (brief 3.4).
  - Shopping Session: start/finish/current (brief 3.5).
  - Expiry calendar: JSON for the UI + a read-only iCal feed (brief 3.7),
    both built live from Grocy's existing stock data -- no new table, so the
    calendar can never drift out of sync with a consume/waste/date edit.

Existing routers (pending, receipt) call the services this router wraps
directly rather than importing from here, so nothing outside this file
depends on FastAPI request/response shapes.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings, APP_NAME
from ..database import get_db
from ..models.db_models import Retailer
from ..services import foodhub_retailers, shopping_session
from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/foodhub", tags=["foodhub"])


# ---------------------------------------------------------------- Retailers --

class RetailerCreate(BaseModel):
    name: str
    sort_order: int = 0


class RetailerUpdate(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None


def _retailer_dict(r: Retailer) -> dict:
    return {"id": r.id, "name": r.name, "sort_order": r.sort_order,
            "active": bool(r.active)}


@router.get("/retailers")
def list_retailers(include_hidden: bool = False, db: Session = Depends(get_db)):
    q = db.query(Retailer)
    if not include_hidden:
        q = q.filter(Retailer.active == 1)
    rows = q.order_by(Retailer.sort_order, Retailer.name).all()
    return [_retailer_dict(r) for r in rows]


@router.post("/retailers", status_code=201)
def create_retailer(body: RetailerCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Retailer name is required")
    if db.query(Retailer).filter(Retailer.name == name).first():
        raise HTTPException(409, "A retailer with that name already exists")
    row = Retailer(name=name, sort_order=body.sort_order, active=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _retailer_dict(row)


@router.put("/retailers/{retailer_id}")
def update_retailer(retailer_id: int, body: RetailerUpdate, db: Session = Depends(get_db)):
    row = db.query(Retailer).filter(Retailer.id == retailer_id).first()
    if not row:
        raise HTTPException(404, "Retailer not found")
    data = body.model_dump(exclude_none=True)
    if "name" in data:
        data["name"] = data["name"].strip() or row.name
    if "active" in data:
        data["active"] = 1 if data["active"] else 0
    for field, value in data.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return _retailer_dict(row)


@router.delete("/retailers/{retailer_id}", status_code=204)
def delete_retailer(retailer_id: int, db: Session = Depends(get_db)):
    """Hard-delete a retailer. Prefer PUT .../active=false (soft-hide) when the
    retailer has purchase history you want to keep browsable; this is for
    cleaning up a duplicate/mistyped entry."""
    row = db.query(Retailer).filter(Retailer.id == retailer_id).first()
    if row:
        db.delete(row)
        db.commit()


@router.get("/retailers/suggest")
def suggest_retailer(barcode: str | None = None, product_id: int | None = None,
                     db: Session = Depends(get_db)):
    """Best-guess retailer for a barcode/product, from purchase history.

    Returns {"suggestion": null} rather than 404 when nothing is known yet --
    this is an optional hint, not a lookup that can fail.
    """
    suggestion = foodhub_retailers.suggest_retailer(db, barcode, product_id)
    return {"suggestion": suggestion}


# ----------------------------------------------------------- Shopping Session --

class SessionStart(BaseModel):
    retailer_id: int


@router.post("/shopping-session/start")
def start_shopping_session(body: SessionStart, db: Session = Depends(get_db)):
    try:
        session = shopping_session.start(db, body.retailer_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return shopping_session.as_dict(session, db)


@router.post("/shopping-session/finish")
def finish_shopping_session(db: Session = Depends(get_db)):
    session = shopping_session.finish(db)
    return {"finished": shopping_session.as_dict(session, db)}


@router.get("/shopping-session/current")
def get_current_shopping_session(db: Session = Depends(get_db)):
    return {"session": shopping_session.as_dict(shopping_session.current(db), db)}


# --------------------------------------------------------------- Calendar --
# Built entirely from live Grocy data (get_full_stock): no Food Hub table
# duplicates expiry/product/quantity data, so the calendar is automatically
# correct after any consume/waste/date-edit -- there is nothing to keep in
# sync because nothing here is a copy (brief 3.7 / "Not Doing" in the plan).

@router.get("/calendar")
async def calendar_data(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    """{"date": [ {name, amount, unit, product_id, urgency}, ... ]} for every
    dated stock entry, grouped by best-before date. ``from``/``to`` (ISO
    dates, both optional) narrow the range; omitted, every dated entry Grocy
    currently holds is returned and the page itself windows to the visible
    month."""
    grocy = GrocyClient()
    try:
        stock = await grocy.get_full_stock()
    except GrocyError as e:
        raise HTTPException(502, str(e))
    start_d = date.fromisoformat(from_) if from_ else None
    end_d = date.fromisoformat(to) if to else None
    by_date: dict[str, list[dict]] = {}
    for entry in stock:
        bbd = entry.get("best_before_date")
        if not bbd:
            continue
        try:
            d = date.fromisoformat(bbd)
        except ValueError:
            continue
        if start_d and d < start_d:
            continue
        if end_d and d > end_d:
            continue
        by_date.setdefault(bbd, []).append({
            "product_id": entry["product_id"],
            "name": entry["name"],
            "amount": entry["amount"],
            "unit": entry.get("unit"),
            "urgency": entry.get("urgency"),
        })
    return by_date


def _ics_escape(text: str) -> str:
    """Escape text per RFC 5545 3.3.11: backslash, semicolon, comma, then
    newlines, in that order so an escaping backslash is never itself escaped
    a second time."""
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Fold a content line at 75 octets per RFC 5545 3.1, continuation lines
    prefixed with a single space, since some calendar clients reject
    unfolded long lines (a long product name is the realistic case here)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    while len(encoded) > 75:
        # Fold on a byte boundary that doesn't split a multi-byte UTF-8
        # character: back off from 75 until the split point is not a
        # continuation byte (0b10xxxxxx).
        cut = 75
        while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(encoded[:cut])
        encoded = encoded[cut:]
    parts.append(encoded)
    return ("\r\n ").join(p.decode("utf-8") for p in parts)


def _build_ics(stock: list[dict]) -> str:
    """One all-day VEVENT per dated stock entry. Hand-rolled rather than a new
    'ics' PyPI dependency: this repo's requirements.lock is hash-pinned and
    enforced by tests/test_requirements_lock.py, so adding a package means
    regenerating that lock in the same commit -- a plain VCALENDAR/VEVENT
    writer for one all-day event type is a few lines and avoids that entirely.
    Regenerated fresh on every request from live data, never written to disk,
    so a consumed/wasted item simply is not in the next fetch.
    """
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Food Hub//Expiry Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(APP_NAME)} Expiry",
    ]
    for entry in stock:
        bbd = entry.get("best_before_date")
        if not bbd:
            continue
        try:
            d = date.fromisoformat(bbd)
        except ValueError:
            continue
        uid = f"foodhub-expiry-{entry['product_id']}-{bbd}@{settings.device_id or 'local'}"
        amount = entry.get("amount")
        amount_txt = f"{amount:g}" if isinstance(amount, (int, float)) else str(amount)
        summary = f"{entry['name']} expires"
        desc = f"{amount_txt} {entry.get('unit') or ''} best by {bbd}".strip()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            # All-day events in iCal are exclusive on DTEND: one calendar day
            # after DTSTART marks a single-day event, per RFC 5545 3.6.1.
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            _ics_fold(f"SUMMARY:{_ics_escape(summary)}"),
            _ics_fold(f"DESCRIPTION:{_ics_escape(desc)}"),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@router.get("/calendar/expiry.ics", response_class=PlainTextResponse)
async def calendar_ics(token: str = Query(...)):
    """Read-only iCal feed of every dated stock entry, for subscribing in a
    phone/desktop calendar app. Auth is the long per-install token in the URL
    (settings.foodhub_calendar_token), not the normal session/API-key auth,
    because most calendar clients cannot send either -- the same pattern
    Nextcloud/Radicale use for calendar subscriptions. See config.py's
    foodhub_calendar_token for why: treat a copy of this URL as a secret.
    """
    if not settings.foodhub_calendar_token or not secrets.compare_digest(
            token, settings.foodhub_calendar_token):
        raise HTTPException(403, "Invalid or missing calendar token")
    grocy = GrocyClient()
    try:
        stock = await grocy.get_full_stock()
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return PlainTextResponse(_build_ics(stock), media_type="text/calendar")


@router.post("/calendar/token/regenerate")
def regenerate_calendar_token():
    """Issue a new calendar token, invalidating every existing subscription
    URL (a lost/leaked link, or just wanting a fresh one)."""
    token = secrets.token_urlsafe(32)
    settings.save({"foodhub_calendar_token": token})
    return {"token": token}
