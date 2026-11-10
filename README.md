# Order & Inventory API

A portfolio demonstration of a small e-commerce backend using Python, FastAPI and SQLite. The products and transactions are synthetic. This is a new demo, not a representation of any client's proprietary code or a past commercial system.

## What it demonstrates

- REST endpoints with typed request validation and interactive OpenAPI docs.
- Transactional stock reservation and a simple order state machine: `pending → confirmed → shipped`, or cancellation from pending/confirmed with one-time restocking.
- Idempotency keys: identical requests return the existing order; reusing a key for a different request returns HTTP 409.
- Integer-cent EUR amounts, price snapshots per item, and an append-only demo status history.
- SQLite `BEGIN IMMEDIATE` serializes writers to prevent overselling within the same database. A concurrent reservation test exercises this behavior.

## Quick start

Requires Python 3.11 or newer. No API keys, payment provider or external service are needed.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e . pytest httpx
uvicorn order_api.app:app --reload
```

Open http://127.0.0.1:8000/docs, or try:

```bash
curl http://127.0.0.1:8000/products
curl -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-order-1' \
  -d '{"items":[{"sku":"NOTEBOOK-A5","quantity":2}]}'
curl http://127.0.0.1:8000/orders/1
curl -X PATCH http://127.0.0.1:8000/orders/1/status \
  -H 'Content-Type: application/json' -d '{"status":"confirmed"}'
pytest -q
```

On first startup the app creates `orders.sqlite3` and seeds three fictional products. Set `ORDER_DB` to a different file to use a separate database. Responses return current order state, including on repeated idempotent requests; the HTTP status for both initial requests and retries is 201 in this demo.

## Design and limits

Each write uses a single SQLite transaction; insufficient stock or an unknown product rolls it back. Status updates and their audit events are stored together. SKU prices are snapshotted when the order is created. Idempotency keys are global to the database, not scoped to a user, and are not expired.

This is an educational single-database demonstration, not a production storefront. It has no authentication, authorization, payment integration, tax, shipping, rate limiting or distributed infrastructure. Do not expose it publicly or process real customer data. SQLite's single-writer behavior is intentional here and is not a claim of multi-node scalability. The status event log is not tamper-proof.

## Project structure

`order_api/app.py` contains the API, transaction boundary and SQLite schema. `tests/test_orders.py` covers validation, idempotency, rollback, transitions, restocking and concurrent stock reservation. `.github/workflows/ci.yml` runs tests on two Python versions.
