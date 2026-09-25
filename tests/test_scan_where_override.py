"""Phone scan page "Adding to" override (Food Hub, Sept 2026).

Will adds batches of stock straight into a freezer in another room with the
phone scan page. Its "Adding to" picker sends ?storage= on the lookup and
storage_type on the quick-add, so the item lands in the area he picked (not
the product lookup's fridge/cupboard guess) and the suggested date follows
that area's shelf-life rule.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"
sys.path.insert(0, str(_SERVICE_DIR))


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
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture
def fridge_product(monkeypatch):
    """Every lookup resolves to a chilled product with a one-week date."""
    from app.models.food import FoodCategory, FoodItem, StorageType
    from app.routers import pending

    async def _lookup(barcode, db):
        return FoodItem(name="Chicken Breast Fillets", category=FoodCategory.meat,
                        storage_type=StorageType.refrigerated, barcode=barcode,
                        best_by_date=date.today() + timedelta(days=7),
                        best_by_source="default")

    monkeypatch.setattr(pending, "lookup_barcode", _lookup)


@pytest.fixture
def imported(monkeypatch):
    from app.services.grocy import GrocyClient

    seen = []

    async def _import(self, item):
        seen.append(item)
        return {"product_id": 900 + len(seen), "name": item.name}

    async def _empty(self):
        return []

    async def _no(self, name):
        return False

    monkeypatch.setattr(GrocyClient, "import_item", _import)
    monkeypatch.setattr(GrocyClient, "get_stock", _empty)
    monkeypatch.setattr(GrocyClient, "has_in_stock", _no)
    return seen


def test_lookup_without_override_keeps_guess(client, fridge_product):
    d = client.get("pending/lookup", params={"barcode": "5000000000001"}).json()
    assert d["storage_type"] == "refrigerated"
    assert d["best_by_date"] == (date.today() + timedelta(days=7)).isoformat()


def test_lookup_freezer_override_moves_area_and_date(client, fridge_product):
    d = client.get("pending/lookup",
                   params={"barcode": "5000000000001", "storage": "frozen"}).json()
    assert d["storage_type"] == "frozen"
    assert date.fromisoformat(d["best_by_date"]) > date.today() + timedelta(days=7)


def test_lookup_ignores_unknown_area(client, fridge_product):
    d = client.get("pending/lookup",
                   params={"barcode": "5000000000001", "storage": "garage"}).json()
    assert d["storage_type"] == "refrigerated"


def test_quick_add_lands_in_freezer_with_confirmed_date(client, fridge_product, imported):
    when = (date.today() + timedelta(days=200)).isoformat()
    r = client.post("pending/quick-add", json={"barcode": "5000000000001",
                                               "best_by_date": when,
                                               "storage_type": "frozen"})
    assert r.status_code == 200
    assert imported and imported[-1].storage_type.value == "frozen"
    assert imported[-1].best_by_date.isoformat() == when


def test_quick_add_without_override_unchanged(client, fridge_product, imported):
    client.post("pending/quick-add", json={"barcode": "5000000000001"})
    assert imported[-1].storage_type.value == "refrigerated"
