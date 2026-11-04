"""POST /inventory/consume-entry/{product_id}/{entry_id} (Nov 2026).

Takes stock off one exact entry; the numeric row id from the dashboard is
translated to Grocy's stock_id. No Grocy or network needed."""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.routers import inventory  # noqa: E402


class FakeGrocy:
    calls = []

    async def _get(self, path):
        return [{"id": 232, "stock_id": "abc123"}, {"id": 233, "stock_id": "def456"}]

    async def consume_stock_entry(self, pid, sid, amount=1.0, spoiled=False):
        FakeGrocy.calls.append((pid, sid, amount, spoiled))
        return [{"ok": True}]


def test_numeric_row_id_resolves_to_stock_id(monkeypatch):
    monkeypatch.setattr(inventory, "GrocyClient", FakeGrocy)
    FakeGrocy.calls.clear()
    asyncio.run(inventory.consume_entry(144, "232", 1.0, True))
    assert FakeGrocy.calls == [(144, "abc123", 1.0, True)]


def test_missing_entry_is_404(monkeypatch):
    monkeypatch.setattr(inventory, "GrocyClient", FakeGrocy)
    with pytest.raises(HTTPException) as e:
        asyncio.run(inventory.consume_entry(144, "999", 1.0, False))
    assert e.value.status_code == 404
