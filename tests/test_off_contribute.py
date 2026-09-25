"""Food Hub, Sept 2026: sharing newly named products with Open Food Facts."""
import asyncio
import json

import pytest

from app.services import off_contribute as oc


class _Resp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.AsyncClient; records calls, never touches the network."""
    calls = []
    exists = False
    post_ok = True

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        _FakeClient.calls.append(("GET", url, None))
        if _FakeClient.exists:
            return _Resp(200, {"status": 1})
        return _Resp(404, {"status": 0})

    async def post(self, url, data=None):
        _FakeClient.calls.append(("POST", url, data))
        return _Resp(200, {"status": 1 if _FakeClient.post_ok else 0})


@pytest.fixture
def fake_off(monkeypatch, tmp_path):
    cred = tmp_path / "off_credentials.json"
    cred.write_text(json.dumps({"user_id": "will", "password": "pw"}))
    monkeypatch.setattr(oc, "CRED_FILE", cred)
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls, _FakeClient.exists, _FakeClient.post_ok = [], False, True
    return _FakeClient


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_valid_gtin():
    assert oc.valid_gtin("5000112637922")      # Coca-Cola can
    assert oc.valid_gtin("8711000596920")      # Kenco
    assert not oc.valid_gtin("5000112637923")  # bad check digit
    assert not oc.valid_gtin("12345")
    assert not oc.valid_gtin("abc")


def test_not_configured_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "CRED_FILE", tmp_path / "missing.json")
    assert not oc.enabled()
    assert run(oc.contribute("8711000596920", "Kenco Smooth")) == "not_configured"


def test_creates_new_product(fake_off):
    assert run(oc.contribute("8711000596920", "Kenco Smooth Instant Coffee", "Kenco")) == "added"
    post = [c for c in fake_off.calls if c[0] == "POST"]
    assert len(post) == 1
    form = post[0][2]
    assert form["code"] == "8711000596920"
    assert form["product_name"] == "Kenco Smooth Instant Coffee"
    assert form["brands"] == "Kenco"


def test_never_overwrites_existing_product(fake_off):
    fake_off.exists = True
    assert run(oc.contribute("8711000596920", "my coffee")) == "exists"
    assert not [c for c in fake_off.calls if c[0] == "POST"]


def test_skips_placeholder_and_bad_codes(fake_off):
    assert run(oc.contribute("8711000596920", "Unknown (8711000596920)")) == "skipped"
    assert run(oc.contribute("8711000596921", "Kenco")) == "skipped"   # bad checksum
    assert run(oc.contribute("123", "Kenco")) == "skipped"
    assert fake_off.calls == []


def test_rejection_reported_as_failed(fake_off):
    fake_off.post_ok = False
    assert run(oc.contribute("8711000596920", "Kenco Smooth")) == "failed"
