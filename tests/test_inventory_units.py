"""One row per unit on the Inventory page (Oct 2026).

Will: three of the same item with the same date must still be listed
individually, not lumped into one "3" row. get_full_stock(split_units=True)
emits one row per unit of every Grocy stock entry, each with its own date,
location and date added.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.grocy import GrocyClient  # noqa: E402

_AGG = [  # what /stock returns: one lumped row per product
    {"product_id": 7, "amount": 4.5, "amount_opened": 1.0,
     "best_before_date": "2026-10-10", "product": {"name": "Chicken Fillets"}},
    {"product_id": 8, "amount": 1.0, "best_before_date": "2026-10-20",
     "product": {"name": "Milk"}},
]
_ENTRIES = [  # /objects/stock: the real entries
    {"id": 11, "product_id": 7, "amount": 3.0, "location_id": 1,
     "best_before_date": "2026-10-10", "row_created_timestamp": "2026-10-06 10:00:00"},
    {"id": 12, "product_id": 7, "amount": 1.5, "location_id": 2,
     "best_before_date": "2027-10-06", "row_created_timestamp": "2026-10-07 09:00:00"},
    {"id": 13, "product_id": 8, "amount": 1.0, "location_id": 1,
     "best_before_date": "2026-10-20", "row_created_timestamp": "2026-10-05 08:00:00"},
]
_LOCS = [{"id": 1, "name": "Refrigerator"}, {"id": 2, "name": "Freezer"}]


def _rows(**kw):
    async def fake_get(self, path):
        return {"/stock": _AGG, "/objects/stock": _ENTRIES}.get(path, [])

    async def fake_cached(self, path):
        return _LOCS if path == "/objects/locations" else []

    with patch.object(GrocyClient, "_get", fake_get), \
         patch.object(GrocyClient, "_cached_list", fake_cached):
        return asyncio.run(GrocyClient().get_full_stock(**kw))


def test_each_unit_gets_its_own_row_with_its_own_date_and_place():
    rows = [r for r in _rows(split_locations=True, split_units=True)
            if r["name"] == "Chicken Fillets"]
    assert [r["amount"] for r in rows] == [1.0, 1.0, 1.0, 1.0, 0.5]
    assert [r["best_before_date"] for r in rows] == ["2026-10-10"] * 3 + ["2027-10-06"] * 2
    assert [r["location_name"] for r in rows] == ["Refrigerator"] * 3 + ["Freezer"] * 2
    assert [r["entry_id"] for r in rows] == [11, 11, 11, 12, 12]
    assert rows[4]["added_date"] == "2026-10-07 09:00:00"
    assert all(r["total_amount"] == 4.5 for r in rows)
    # One opened unit, so exactly one row carries the Opened badge.
    assert [r["amount_opened"] for r in rows] == [1.0, 0, 0, 0, 0]


def test_default_read_is_unchanged():
    rows = _rows(split_locations=True)
    chicken = [r for r in rows if r["name"] == "Chicken Fillets"]
    assert [r["amount"] for r in chicken] == [3.0, 1.5]
    assert "entry_id" not in chicken[0]
