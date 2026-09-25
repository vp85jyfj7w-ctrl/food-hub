"""Share a newly named product with Open Food Facts (Food Hub, Sept 2026).

Will asked for the name he gives an unknown barcode on the Pending page to be
pushed to Open Food Facts too, so the product is findable for everyone (and
for Food Hub itself on any fresh install). The Pending card shows an
"Also add to Open Food Facts" tick box, ticked by default, on rows whose
lookup failed; commit_pending() calls contribute() for the ticked ones.

Guard rails, each deliberate:
- Off unless an Open Food Facts account is configured: /app/secrets/
  off_credentials.json holding {"user_id": "...", "password": "..."}. The
  secrets folder is mounted read-only and never served, so the login is not
  exposed to any web page (unlike an API key in client-side JS).
- CREATE ONLY, never edit: the product is re-checked on Open Food Facts right
  before sending, and skipped if it already exists there. Food Hub must never
  overwrite someone else's (usually better) entry with a kitchen nickname.
- Only real retail barcodes: a valid GTIN-8/12/13/14 checksum, and never Will's
  own printed labels or store/scale codes (is_own_item_code), whose numbers are
  reused for different food and would be wrong for everyone.
- Never the "Unknown (...)" placeholder, only a name Will actually typed.
- Best effort: runs in the background after the commit has already succeeded,
  so an Open Food Facts outage or error can never block adding stock.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx

from .barcode import is_own_item_code
from .known_barcodes import _looks_like_placeholder

log = logging.getLogger(__name__)

CRED_FILE = Path("/app/secrets/off_credentials.json")
OFF_BASE = "https://world.openfoodfacts.org"
# Open Food Facts asks every app to identify itself on writes.
USER_AGENT = "FoodHub/1.0 (self-hosted pantry app, fork of PantryRaider)"

# Background tasks must be referenced until they finish, or asyncio may
# garbage-collect them mid-flight.
_tasks: set[asyncio.Task] = set()


def credentials() -> tuple[str, str] | None:
    try:
        data = json.loads(CRED_FILE.read_text())
    except (OSError, ValueError):
        return None
    user, pw = (data.get("user_id") or "").strip(), data.get("password") or ""
    return (user, pw) if user and pw else None


def enabled() -> bool:
    return credentials() is not None


def valid_gtin(code: str | None) -> bool:
    code = (code or "").strip()
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def shareable(barcode: str | None) -> bool:
    """Whether a Pending row's barcode could ever be shared (ignores the name)."""
    return bool(barcode) and valid_gtin(barcode) and not is_own_item_code(barcode)


async def contribute(barcode: str, name: str | None, brand: str | None = None) -> str:
    """Create the product on Open Food Facts. Returns a short outcome word."""
    creds = credentials()
    if creds is None:
        return "not_configured"
    name = (name or "").strip()
    if not shareable(barcode) or _looks_like_placeholder(name):
        return "skipped"
    user, pw = creds
    try:
        async with httpx.AsyncClient(timeout=20.0,
                                     headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(f"{OFF_BASE}/api/v2/product/{barcode}.json",
                                 params={"fields": "code"})
            if r.status_code == 200 and r.json().get("status") == 1:
                log.info("Open Food Facts already has %s; not sending", barcode)
                return "exists"
            form = {
                "code": barcode,
                "user_id": user,
                "password": pw,
                "lang": "en",
                "product_name": name,
                "countries": "en:united-kingdom",
                "comment": "Added from Food Hub (self-hosted pantry app)",
            }
            if brand and brand.strip():
                form["brands"] = brand.strip()
            r = await client.post(f"{OFF_BASE}/cgi/product_jqm2.pl", data=form)
            ok = r.status_code == 200 and r.json().get("status") == 1
    except Exception as e:  # noqa: BLE001 - best effort, never raise
        log.warning("Open Food Facts contribution for %s failed: %s", barcode, e)
        return "failed"
    if ok:
        log.info("Added %s (%s) to Open Food Facts", barcode, name)
        return "added"
    log.warning("Open Food Facts rejected %s: HTTP %s %s", barcode,
                r.status_code, r.text[:200])
    return "failed"


def contribute_in_background(barcode: str, name: str | None, brand: str | None) -> bool:
    """Fire-and-forget wrapper for use after a commit. False if not started."""
    if not enabled():
        return False
    try:
        task = asyncio.get_running_loop().create_task(contribute(barcode, name, brand))
    except RuntimeError:
        return False  # no running loop (sync test harness)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
