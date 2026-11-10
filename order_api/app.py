"""An educational order/inventory API. Not suitable for real transactions."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator


class OrderLine(BaseModel):
    sku: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9-]+$")
    quantity: int = Field(gt=0, le=100, strict=True)


class NewOrder(BaseModel):
    items: list[OrderLine] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_skus(self):
        if len({item.sku for item in self.items}) != len(self.items):
            raise ValueError("Each SKU may occur only once")
        return self


class StatusUpdate(BaseModel):
    status: Literal["confirmed", "cancelled", "shipped"]


SEED = [("NOTEBOOK-A5", "Notebook A5", 1290, 25),
        ("PEN-BLUE", "Blue pen", 250, 50),
        ("MUG-WHITE", "White mug", 1990, 10)]


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("ORDER_DB", "orders.sqlite3")

    def connection():
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction():
        db = connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # Initialize on app creation so tests and CLI behave consistently.
    with transaction() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
            stock INTEGER NOT NULL CHECK(stock >= 0));
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE NOT NULL,
            request_hash TEXT NOT NULL, status TEXT NOT NULL,
            total_cents INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS order_items (
            order_id INTEGER NOT NULL REFERENCES orders(id), sku TEXT NOT NULL REFERENCES products(sku),
            quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
            PRIMARY KEY (order_id, sku));
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
            status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        """)
        db.executemany("INSERT OR IGNORE INTO products(sku,name,price_cents,stock) VALUES(?,?,?,?)", SEED)

    app = FastAPI(title="Order & Inventory API", version="0.1.0", description="Portfolio demo; no real payments or customer data.")

    def get_order(db, order_id):
        row = db.execute("SELECT id,status,total_cents,created_at FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Order not found")
        result = dict(row)
        result["currency"] = "EUR"
        result["items"] = [dict(r) for r in db.execute("SELECT sku,quantity,unit_price_cents FROM order_items WHERE order_id=? ORDER BY sku", (order_id,))]
        result["events"] = [dict(r) for r in db.execute("SELECT status,created_at FROM order_events WHERE order_id=? ORDER BY id", (order_id,))]
        return result

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/products")
    def products():
        with closing(connection()) as db:
            return [dict(row) for row in db.execute("SELECT sku,name,price_cents,stock FROM products ORDER BY sku")]

    @app.post("/orders", status_code=status.HTTP_201_CREATED)
    def create_order(order: NewOrder, idempotency_key: Annotated[str, Header(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")]):
        # Canonicalize order of lines so semantically equivalent retries hash equally.
        items = sorted((item.model_dump() for item in order.items), key=lambda item: item["sku"])
        payload_hash = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with transaction() as db:
            existing = db.execute("SELECT id,request_hash FROM orders WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["request_hash"] != payload_hash:
                    raise HTTPException(409, "Idempotency key already used with different order")
                return get_order(db, existing["id"])
            lines = []
            for item in items:
                product = db.execute("SELECT * FROM products WHERE sku=?", (item["sku"],)).fetchone()
                if not product:
                    raise HTTPException(404, f"Unknown SKU: {item['sku']}")
                if product["stock"] < item["quantity"]:
                    raise HTTPException(409, f"Insufficient stock: {item['sku']}")
                lines.append((product, item["quantity"]))
            total = sum(p["price_cents"] * qty for p, qty in lines)
            cursor = db.execute("INSERT INTO orders(idempotency_key,request_hash,status,total_cents) VALUES(?,?,'pending',?)", (idempotency_key, payload_hash, total))
            order_id = cursor.lastrowid
            for product, qty in lines:
                db.execute("UPDATE products SET stock=stock-? WHERE sku=?", (qty, product["sku"]))
                db.execute("INSERT INTO order_items VALUES(?,?,?,?)", (order_id, product["sku"], qty, product["price_cents"]))
            db.execute("INSERT INTO order_events(order_id,status) VALUES(?,'pending')", (order_id,))
            return get_order(db, order_id)

    @app.get("/orders/{order_id}")
    def read_order(order_id: int):
        with closing(connection()) as db:
            return get_order(db, order_id)

    @app.patch("/orders/{order_id}/status")
    def update_status(order_id: int, update: StatusUpdate):
        allowed = {"pending": {"confirmed", "cancelled"}, "confirmed": {"shipped", "cancelled"}, "shipped": set(), "cancelled": set()}
        with transaction() as db:
            order = get_order(db, order_id)
            if update.status == order["status"]:
                return order
            if update.status not in allowed[order["status"]]:
                raise HTTPException(409, "Invalid status transition")
            if update.status == "cancelled":
                for item in order["items"]:
                    db.execute("UPDATE products SET stock=stock+? WHERE sku=?", (item["quantity"], item["sku"]))
            db.execute("UPDATE orders SET status=? WHERE id=?", (update.status, order_id))
            db.execute("INSERT INTO order_events(order_id,status) VALUES(?,?)", (order_id, update.status))
            return get_order(db, order_id)

    return app


app = create_app()
