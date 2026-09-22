""""Name it once, know it next time" -- taught barcodes (Food Hub, Sept 2026).

Will's own words: "when something comes up unknown i want to be able to name
it and then next time it comes round it will know what the item is... you get
me building or adding to my own database."

End to end through the real routes (Grocy mocked, mirroring
tests/test_expiry_learning_pending.py): a barcode Open Food Facts has never
heard of scans as "Unknown (...)", gets renamed and committed on the Pending
page, and the SECOND scan of the exact same barcode should resolve straight
away -- via services/known_barcodes.py, never by calling Open Food Facts
again -- with the name (and category/storage/shelf-life) it was taught.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"
sys.path.insert(0, str(_SERVICE_DIR))

from app.services.barcode import BarcodeNotFound  # noqa: E402


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
def _mock_grocy(monkeypatch):
    """Keep Grocy off the network; every import succeeds with a fresh id."""
    from app.services.grocy import GrocyClient

    counter = {"pid": 700}

    async def _empty_stock(self):
        return []

    async def _import_ok(self, item):
        counter["pid"] += 1
        return {"product_id": counter["pid"], "name": item.name}

    async def _has_in_stock_false(self, name):
        return False

    monkeypatch.setattr(GrocyClient, "get_stock", _empty_stock)
    monkeypatch.setattr(GrocyClient, "import_item", _import_ok)
    monkeypatch.setattr(GrocyClient, "has_in_stock", _has_in_stock_false)
    yield


def _clear_pending(client):
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")


def _always_unknown(monkeypatch):
    """Force every barcode lookup to genuinely fail, as Open Food Facts would
    for a barcode it has never catalogued -- unless known_barcodes.lookup()
    answers first (that check runs before this fallback, inside the real
    lookup_barcode(), so it is never patched out)."""
    from app.services import barcode as barcode_svc

    call_count = {"n": 0}

    async def _miss(code):
        call_count["n"] += 1
        return None

    monkeypatch.setattr(barcode_svc, "fetch_off_product", _miss)

    async def _off_miss(bc):
        call_count["n"] += 1
        raise BarcodeNotFound(f"no such product {bc}")

    # Patch the module-level OFF call inside lookup_barcode via httpx: simplest
    # is to monkeypatch httpx.AsyncClient.get to a 404-ish empty response, but
    # that also affects unrelated code; instead directly raise from the OFF
    # branch by making the request always come back "not found" (status==0).
    import httpx as _httpx

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"status": 0}

    async def _fake_get(self, url, *a, **kw):
        call_count["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(_httpx.AsyncClient, "get", _fake_get)
    return call_count


def test_unknown_barcode_taught_on_commit_then_recognised_next_scan(client, monkeypatch):
    """The full loop: unknown -> renamed -> committed -> remembered -> the
    next scan of the SAME barcode resolves instantly, without a second call
    to Open Food Facts, and lands back in Pending already correctly named
    (no "Unknown (...)", no lookup_failed)."""
    _clear_pending(client)
    off_calls = _always_unknown(monkeypatch)
    barcode = "9999911111111"

    # 1. First scan: genuinely unknown, queued as a placeholder.
    r = client.post("pending/scan", json={"barcode": barcode})
    assert r.status_code == 200
    rows = client.get("pending/").json()["items"]
    row = next(x for x in rows if x["barcode"] == barcode)
    assert row["name"].startswith("Unknown (")
    assert row["lookup_failed"] is True
    calls_after_first_scan = off_calls["n"]
    assert calls_after_first_scan >= 1  # Open Food Facts really was asked

    # 2. Will renames it on the Pending page and commits.
    client.patch(f"pending/{row['id']}", json={
        "name": "Nan's Chutney", "category": "Condiments",
        "storage_type": "room_temp", "unit": "jar",
    })
    commit = client.post("pending/commit", json={"ids": [row["id"]]})
    assert commit.status_code == 200
    assert commit.json()["results"][0]["status"] == "ok"

    # 3. It's remembered: visible on the Known Barcodes management page.
    known = client.get("ui/known-barcodes")
    assert known.status_code == 200
    assert "Nan" in known.text and barcode in known.text

    # 4. Second scan of the exact same barcode: resolves immediately, no OFF
    # call, correct name straight away -- this is the actual feature.
    r2 = client.post("pending/scan", json={"barcode": barcode})
    assert r2.status_code == 200
    rows2 = client.get("pending/").json()["items"]
    row2 = next(x for x in rows2 if x["barcode"] == barcode)
    assert row2["name"] == "Nan's Chutney", row2["name"]
    assert row2["lookup_failed"] is False
    assert off_calls["n"] == calls_after_first_scan, (
        "Open Food Facts was called again for an already-taught barcode")


def test_committing_without_renaming_does_not_teach_the_placeholder(client, monkeypatch):
    """Committing a row that was never renamed must not poison the table
    with the literal "Unknown (...)" placeholder text."""
    _clear_pending(client)
    _always_unknown(monkeypatch)
    barcode = "9999922222222"

    client.post("pending/scan", json={"barcode": barcode})
    row = next(x for x in client.get("pending/").json()["items"]
               if x["barcode"] == barcode)
    assert row["name"].startswith("Unknown (")
    client.post("pending/commit", json={"ids": [row["id"]]})

    known = client.get("ui/known-barcodes")
    assert barcode not in known.text


def test_own_item_label_is_never_taught(client, monkeypatch):
    """Will's own printed sequential labels (GS1 store-local range) must
    never be taught a fixed name: the same label number is reused for a
    different batch every time, so a remembered name would actively lie."""
    _clear_pending(client)
    _always_unknown(monkeypatch)
    barcode = "2000000000473"  # store-local prefix "20" -> Prepped Food #47

    client.post("pending/scan", json={"barcode": barcode})
    row = next(x for x in client.get("pending/").json()["items"]
               if x["barcode"] == barcode)
    client.patch(f"pending/{row['id']}", json={"name": "Chicken Tikka batch"})
    client.post("pending/commit", json={"ids": [row["id"]]})

    known = client.get("ui/known-barcodes")
    assert barcode not in known.text


def test_re_teaching_corrects_a_previous_mistake(client, monkeypatch):
    """Scanning the same barcode again with a different confirmed name
    overwrites what was taught before -- fixing a bad first name doesn't
    need the management page."""
    _clear_pending(client)
    _always_unknown(monkeypatch)
    barcode = "9999933333333"

    client.post("pending/scan", json={"barcode": barcode})
    row = next(x for x in client.get("pending/").json()["items"]
               if x["barcode"] == barcode)
    client.patch(f"pending/{row['id']}", json={"name": "Mystery Jar"})
    client.post("pending/commit", json={"ids": [row["id"]]})

    client.post("pending/scan", json={"barcode": barcode})
    row2 = next(x for x in client.get("pending/").json()["items"]
                if x["barcode"] == barcode)
    assert row2["name"] == "Mystery Jar"
    client.patch(f"pending/{row2['id']}", json={"name": "Actually Jam"})
    client.post("pending/commit", json={"ids": [row2["id"]]})

    client.post("pending/scan", json={"barcode": barcode})
    row3 = next(x for x in client.get("pending/").json()["items"]
                if x["barcode"] == barcode)
    assert row3["name"] == "Actually Jam", row3["name"]


def test_management_page_forget_makes_it_unknown_again(client, monkeypatch):
    """Deleting a taught barcode from the Known Barcodes page means the next
    scan goes through ordinary lookup (and Pending) again."""
    _clear_pending(client)
    _always_unknown(monkeypatch)
    barcode = "9999944444444"

    client.post("pending/scan", json={"barcode": barcode})
    row = next(x for x in client.get("pending/").json()["items"]
               if x["barcode"] == barcode)
    client.patch(f"pending/{row['id']}", json={"name": "Test Forgettable"})
    client.post("pending/commit", json={"ids": [row["id"]]})
    assert "Test Forgettable" in client.get("ui/known-barcodes").text

    d = client.post(f"ui/known-barcodes/{barcode}/delete", follow_redirects=False)
    assert d.status_code in (302, 303, 307, 200)
    assert "Test Forgettable" not in client.get("ui/known-barcodes").text

    client.post("pending/scan", json={"barcode": barcode})
    row2 = next(x for x in client.get("pending/").json()["items"]
                if x["barcode"] == barcode)
    assert row2["name"].startswith("Unknown ("), row2["name"]
