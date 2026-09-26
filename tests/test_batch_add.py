"""Batch add (Food Hub, Sept 2026): stock a freezer in another room with just
the wireless scanner. While on, a resolved Stock-up scan goes straight to
stock in the chosen area with that area's date, no prompt; an unresolved one
still goes to Pending, placed in that area; it switches itself off after an
idle hour."""
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"
sys.path.insert(0, str(_SERVICE_DIR))

KNOWN = "5000000000001"
UNKNOWN = "5000000000002"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.auth_required = False
        settings.auth_password = ""
        settings.quick_add_mode = False
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def world(client, monkeypatch):
    """A chilled product for KNOWN, nothing for UNKNOWN, Grocy mocked."""
    from app.models.food import FoodCategory, FoodItem, StorageType
    from app.routers import pending
    from app.services import scanner_mode
    from app.services.barcode import BarcodeNotFound
    from app.services.grocy import GrocyClient

    scanner_mode.reset()

    async def _lookup(barcode, db):
        if barcode != KNOWN:
            raise BarcodeNotFound(barcode)
        return FoodItem(name="Chicken Breast Fillets", category=FoodCategory.meat,
                        storage_type=StorageType.refrigerated, barcode=barcode,
                        best_by_date=date.today() + timedelta(days=7),
                        best_by_source="default")

    monkeypatch.setattr(pending, "lookup_barcode", _lookup)
    imported = []

    async def _import(self, item):
        imported.append(item)
        return {"product_id": 900 + len(imported), "name": item.name}

    async def _empty(self):
        return []

    async def _no(self, name):
        return False

    monkeypatch.setattr(GrocyClient, "import_item", _import)
    monkeypatch.setattr(GrocyClient, "get_stock", _empty)
    monkeypatch.setattr(GrocyClient, "has_in_stock", _no)
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")
    client.delete("pending/batch")
    yield imported
    client.delete("pending/batch")


def test_off_by_default_scan_queues(client, world):
    assert client.get("pending/batch").json()["active"] is False
    r = client.post("pending/scan", json={"barcode": KNOWN}).json()
    assert r["status"] == "queued"
    assert world == []


def test_freezer_batch_adds_straight_to_freezer(client, world):
    s = client.post("pending/batch", json={"area": "frozen"}).json()
    assert s["active"] and s["area"] == "frozen" and s["label"] == "Freezer"
    r = client.post("pending/scan", json={"barcode": KNOWN}).json()
    assert r["status"] == "instant_added" and r["batch_area"] == "frozen"
    item = world[-1]
    assert item.storage_type.value == "frozen"
    assert item.best_by_date > date.today() + timedelta(days=7)
    assert client.get("pending/batch").json()["count"] == 1
    assert client.get("pending/").json()["items"] == []


def test_unknown_in_batch_goes_to_pending_in_freezer(client, world):
    client.post("pending/batch", json={"area": "frozen"})
    r = client.post("pending/scan", json={"barcode": UNKNOWN}).json()
    assert r["status"] == "queued"
    assert r["item"]["storage_type"] == "frozen"
    assert world == []


def test_batch_ignored_outside_stock_up(client, world):
    from app.services import scanner_mode
    client.post("pending/batch", json={"area": "frozen"})
    scanner_mode.set_mode("audit")
    try:
        r = client.post("pending/scan", json={"barcode": KNOWN}).json()
        assert r.get("status") != "instant_added"
        assert world == []
    finally:
        scanner_mode.reset()


def test_switches_itself_off_when_idle(client, world):
    from app.config import settings
    client.post("pending/batch", json={"area": "frozen"})
    f = Path(settings.data_dir) / "batch_add.json"
    data = json.loads(f.read_text())
    data["until"] = time.time() - 1
    f.write_text(json.dumps(data))
    assert client.get("pending/batch").json()["active"] is False
    assert client.post("pending/scan", json={"barcode": KNOWN}).json()["status"] == "queued"


def test_stop_and_bad_area(client, world):
    client.post("pending/batch", json={"area": "frozen"})
    assert client.delete("pending/batch").json()["active"] is False
    assert client.post("pending/batch", json={"area": "garage"}).status_code == 400


def test_ask_date_setting_and_confirmed_add_counts(client, world):
    s = client.post("pending/batch", json={"area": "frozen", "ask_date": True}).json()
    assert s["ask_date"] is True and s["count"] == 0
    when = (date.today() + timedelta(days=150)).isoformat()
    r = client.post("pending/quick-add", json={"barcode": KNOWN, "best_by_date": when,
                                               "storage_type": "frozen"}).json()
    assert r["status"] == "instant_added" and r["batch_area"] == "frozen"
    assert world[-1].storage_type.value == "frozen"
    assert world[-1].best_by_date.isoformat() == when
    assert client.get("pending/batch").json()["count"] == 1
    # Unticking keeps the running count for the same area.
    s = client.post("pending/batch", json={"area": "frozen", "ask_date": False}).json()
    assert s["ask_date"] is False and s["count"] == 1
