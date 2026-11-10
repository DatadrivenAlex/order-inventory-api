from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from order_api.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.sqlite3"))) as c:
        yield c


def place(client, key, items=None):
    return client.post("/orders", headers={"Idempotency-Key": key}, json={"items": items or [{"sku": "NOTEBOOK-A5", "quantity": 2}]})


def stock(client, sku="NOTEBOOK-A5"):
    return next(p["stock"] for p in client.get("/products").json() if p["sku"] == sku)


def test_create_snapshots_prices_and_reserves_stock(client):
    r = place(client, "order-1")
    assert r.status_code == 201
    assert r.json()["total_cents"] == 2580
    assert r.json()["currency"] == "EUR"
    assert r.json()["events"][0]["status"] == "pending"
    assert stock(client) == 23
    assert client.get(f"/orders/{r.json()['id']}").json() == r.json()


def test_retry_same_key_is_idempotent_and_conflicting_payload_rejected(client):
    first = place(client, "same")
    again = place(client, "same")
    assert again.json()["id"] == first.json()["id"]
    assert stock(client) == 23
    different = place(client, "same", [{"sku": "NOTEBOOK-A5", "quantity": 1}])
    assert different.status_code == 409
    assert stock(client) == 23


def test_canonical_request_hash(client):
    items = [{"sku": "PEN-BLUE", "quantity": 1}, {"sku": "NOTEBOOK-A5", "quantity": 1}]
    assert place(client, "key", items).json()["id"] == place(client, "key", list(reversed(items))).json()["id"]


def test_rejected_order_does_not_change_any_stock(client):
    items = [{"sku": "NOTEBOOK-A5", "quantity": 1}, {"sku": "PEN-BLUE", "quantity": 51}]
    assert place(client, "bad", items).status_code == 409
    assert stock(client) == 25
    assert stock(client, "PEN-BLUE") == 50
    assert place(client, "missing", [{"sku": "MISSING", "quantity": 1}]).status_code == 404


def test_cancel_restores_once_and_state_machine_enforced(client):
    order_id = place(client, "cancel").json()["id"]
    url = f"/orders/{order_id}/status"
    assert client.patch(url, json={"status": "cancelled"}).status_code == 200
    assert client.patch(url, json={"status": "cancelled"}).status_code == 200
    assert stock(client) == 25
    assert client.patch(url, json={"status": "confirmed"}).status_code == 409
    assert [e["status"] for e in client.get(f"/orders/{order_id}").json()["events"]] == ["pending", "cancelled"]


def test_confirm_ship_no_restoration(client):
    order_id = place(client, "ship").json()["id"]
    url = f"/orders/{order_id}/status"
    assert client.patch(url, json={"status": "shipped"}).status_code == 409
    assert client.patch(url, json={"status": "confirmed"}).status_code == 200
    assert client.patch(url, json={"status": "shipped"}).status_code == 200
    assert client.patch(url, json={"status": "cancelled"}).status_code == 409
    assert stock(client) == 23


def test_validation(client):
    assert client.post("/orders", json={"items": [{"sku": "PEN-BLUE", "quantity": 1}]}).status_code == 422
    for quantity in (0, -1, "2"):
        assert place(client, "a", [{"sku": "PEN-BLUE", "quantity": quantity}]).status_code == 422
    assert place(client, "dup", [{"sku": "PEN-BLUE", "quantity": 1}] * 2).status_code == 422
    assert client.get("/orders/999").status_code == 404


def test_concurrent_reservations_never_oversell(client):
    def buy(i):
        return place(client, f"parallel-{i}", [{"sku": "MUG-WHITE", "quantity": 1}]).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(buy, range(16)))
    assert outcomes.count(201) == 10
    assert outcomes.count(409) == 6
    assert stock(client, "MUG-WHITE") == 0
