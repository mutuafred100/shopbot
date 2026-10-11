"""Small SQLite database: customers (cart + chat memory), orders, ratings, stock."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from . import settings

DB_FILE = settings.DATA_DIR / "shop.db"

STATUS_LABELS = {
    "awaiting_payment": "Waiting for payment",
    "payment_failed": "Payment failed",
    "paid": "Paid - new order",
    "packed": "Packed",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


@contextmanager
def conn():
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers(
                phone TEXT PRIMARY KEY, name TEXT DEFAULT '', address TEXT DEFAULT '',
                cart TEXT DEFAULT '{}', history TEXT DEFAULT '[]', last_seen INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT, address TEXT,
                items TEXT, subtotal REAL, delivery_fee REAL, total REAL, status TEXT,
                mpesa_phone TEXT, checkout_id TEXT DEFAULT '', receipt TEXT DEFAULT '', claimed_code TEXT DEFAULT '',
                created INTEGER, updated INTEGER);
            CREATE TABLE IF NOT EXISTS ratings(
                order_id INTEGER PRIMARY KEY, phone TEXT, stars INTEGER, created INTEGER);
            CREATE TABLE IF NOT EXISTS inventory(
                pid TEXT PRIMARY KEY, sheet_stock INTEGER, stock INTEGER);
            CREATE TABLE IF NOT EXISTS sms_payments(
                code TEXT PRIMARY KEY, amount REAL, payer TEXT, raw TEXT, created INTEGER, used_order INTEGER);
            CREATE TABLE IF NOT EXISTS seen_messages(id TEXT PRIMARY KEY, t INTEGER);
            """
        )
        try:  # upgrade older databases
            c.execute("ALTER TABLE orders ADD COLUMN claimed_code TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass


# ---------- customers ----------
def customer(phone: str) -> dict:
    with conn() as c:
        row = c.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if not row:
            c.execute("INSERT INTO customers(phone,last_seen) VALUES(?,?)", (phone, int(time.time())))
            row = c.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        d = dict(row)
    d["cart"] = json.loads(d["cart"] or "{}")
    d["history"] = json.loads(d["history"] or "[]")
    return d


def save_customer(phone: str, **fields) -> None:
    customer(phone)  # make sure the row exists
    for k in ("cart", "history"):
        if k in fields:
            fields[k] = json.dumps(fields[k])
    fields["last_seen"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE customers SET {cols} WHERE phone=?", (*fields.values(), phone))


def seen_before(message_id: str) -> bool:
    """WhatsApp can deliver the same message twice. Returns True if we already handled it."""
    with conn() as c:
        if c.execute("SELECT 1 FROM seen_messages WHERE id=?", (message_id,)).fetchone():
            return True
        c.execute("INSERT INTO seen_messages VALUES(?,?)", (message_id, int(time.time())))
        c.execute("DELETE FROM seen_messages WHERE t < ?", (int(time.time()) - 7 * 86400,))
    return False


# ---------- orders ----------
def create_order(phone, name, address, items, subtotal, delivery_fee, total, mpesa_phone) -> int:
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO orders(phone,name,address,items,subtotal,delivery_fee,total,status,mpesa_phone,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (phone, name, address, json.dumps(items), subtotal, delivery_fee, total, "awaiting_payment", mpesa_phone, now, now),
        )
        return cur.lastrowid


def _order(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d


def get_order(order_id: int) -> dict | None:
    with conn() as c:
        return _order(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())


def order_by_checkout(checkout_id: str) -> dict | None:
    with conn() as c:
        return _order(c.execute("SELECT * FROM orders WHERE checkout_id=?", (checkout_id,)).fetchone())


def update_order(order_id: int, **fields) -> None:
    fields["updated"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id))


def list_orders(limit: int = 100, phone: str | None = None) -> list[dict]:
    with conn() as c:
        if phone:
            rows = c.execute("SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT ?", (phone, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_order(r) for r in rows]


# ---------- ratings ----------
def add_rating(order_id: int, phone: str, stars: int) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?)", (order_id, phone, stars, int(time.time())))


def rating_summary() -> tuple[float, int]:
    with conn() as c:
        row = c.execute("SELECT AVG(stars) a, COUNT(*) n FROM ratings").fetchone()
    return (row["a"] or 0.0, row["n"])


def rating_for(order_id: int) -> int | None:
    with conn() as c:
        row = c.execute("SELECT stars FROM ratings WHERE order_id=?", (order_id,)).fetchone()
    return row["stars"] if row else None


# ---------- stock ----------
def sync_inventory(products: list[dict]) -> None:
    """Our stock count follows the sheet whenever the owner changes the number there,
    and goes down by itself when we sell something."""
    with conn() as c:
        for p in products:
            if p["stock"] is None:
                continue
            row = c.execute("SELECT * FROM inventory WHERE pid=?", (p["id"],)).fetchone()
            if not row:
                c.execute("INSERT INTO inventory VALUES(?,?,?)", (p["id"], p["stock"], p["stock"]))
            elif row["sheet_stock"] != p["stock"]:
                c.execute("UPDATE inventory SET sheet_stock=?, stock=? WHERE pid=?", (p["stock"], p["stock"], p["id"]))


def stock_of(pid: str) -> int | None:
    with conn() as c:
        row = c.execute("SELECT stock FROM inventory WHERE pid=?", (pid,)).fetchone()
    return row["stock"] if row else None


def reduce_stock(pid: str, qty: int) -> None:
    with conn() as c:
        c.execute("UPDATE inventory SET stock = MAX(stock - ?, 0) WHERE pid=?", (qty, pid))


def restore_stock(pid: str, qty: int) -> None:
    with conn() as c:
        c.execute("UPDATE inventory SET stock = stock + ? WHERE pid=?", (qty, pid))


# ---------- payment SMS (forwarded from the owner's phone) ----------
def add_sms(code: str, amount: float, payer: str, raw: str) -> bool:
    """Returns False if we already have this code."""
    with conn() as c:
        if c.execute("SELECT 1 FROM sms_payments WHERE code=?", (code,)).fetchone():
            return False
        c.execute("INSERT INTO sms_payments VALUES(?,?,?,?,?,NULL)", (code, amount, payer, raw[:400], int(time.time())))
    return True


def get_sms(code: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM sms_payments WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def mark_sms_used(code: str, order_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE sms_payments SET used_order=? WHERE code=?", (order_id, code))


def order_by_claimed_code(code: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM orders WHERE claimed_code=? AND status IN ('awaiting_payment','payment_failed') ORDER BY id DESC", (code,)).fetchone()
    return _order(row)


def code_used_elsewhere(code: str, order_id: int) -> bool:
    with conn() as c:
        a = c.execute("SELECT 1 FROM orders WHERE id!=? AND (claimed_code=? OR receipt=?)", (order_id, code, code)).fetchone()
        b = c.execute("SELECT 1 FROM sms_payments WHERE code=? AND used_order IS NOT NULL AND used_order!=?", (code, order_id)).fetchone()
    return bool(a or b)
