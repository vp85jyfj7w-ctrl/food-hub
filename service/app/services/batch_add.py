"""Batch add: scan a pile of stock away from the screen (Food Hub, Sept 2026).

Will keeps a freezer in another part of the house and wanted to stock it with
the wireless barcode scanner alone, with nobody at the kitchen screen to answer
the usual "confirm the best-by date" prompt. Turning batch add on (Manage
Pantry, "Batch add" card) picks a storage area; while it is on, every Stock-up
scan from any source is committed straight to stock in that area with that
area's shelf-life date, with no prompt. Anything the lookup can't resolve
still goes to Pending, placed in the batch area, so no scan is ever lost.

It switches itself off after IDLE_MINUTES with no scans, so a forgotten
"Freezer" setting can't quietly file next week's fridge shop in the freezer.
Every scan in batch mode pushes the timer back.

State lives in a small JSON file in data_dir (like scanner_mode.json) so the
two app containers (9284 and 9294) see the same setting.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

AREAS = ("frozen", "refrigerated", "dry", "room_temp")
AREA_LABELS = {"frozen": "Freezer", "refrigerated": "Fridge",
               "dry": "Cupboard", "room_temp": "Counter"}
IDLE_MINUTES = 60


def _file() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "batch_add.json"


def _read() -> dict:
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    f = _file()
    try:
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, f)
    except OSError:
        pass


def active_area() -> str | None:
    """The storage area batch add is filing into, or None when it is off."""
    data = _read()
    area = data.get("area")
    if area not in AREAS:
        return None
    if time.time() > float(data.get("until") or 0):
        return None
    return area


def state() -> dict:
    data = _read()
    area = active_area()
    return {
        "active": area is not None,
        "area": area,
        "label": AREA_LABELS.get(area) if area else None,
        "until": float(data.get("until") or 0) if area else None,
        "count": int(data.get("count") or 0) if area else 0,
        "idle_minutes": IDLE_MINUTES,
    }


def start(area: str) -> dict:
    if area not in AREAS:
        raise ValueError(f"unknown storage area {area!r}")
    _write({"area": area, "until": time.time() + IDLE_MINUTES * 60, "count": 0})
    return state()


def stop() -> dict:
    _write({})
    return state()


def touch(added: bool) -> None:
    """A batch-mode scan happened: push the idle timer back, count adds."""
    data = _read()
    if data.get("area") not in AREAS:
        return
    data["until"] = time.time() + IDLE_MINUTES * 60
    if added:
        data["count"] = int(data.get("count") or 0) + 1
    _write(data)
