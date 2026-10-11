"""Offline self-test of cart, payment, status and ratings logic (no internet, no real WhatsApp/M-Pesa).
Run:  python tests_logic.py"""
import os, sys, tempfile, types

os.environ["DATA_DIR"] = tempfile.mkdtemp()
# stand-ins so the test runs without the web libraries installed
for name in ("httpx", "anthropic"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)

from app import catalog, db, orders, sample, settings, whatsapp as wa  # noqa

settings.ensure_secrets(); db.init()
settings.save({"owner_phone": "0711000111", "delivery_fee": "200", "shop_name": "Test Shop", "payment_mode": "stk"})

# catalog from the sample sheet
catalog.XLSX_FILE.write_bytes(sample.build())
items = catalog.load(force=True)
assert len(items) == 6, len(items)
assert catalog.search("sneakers")[0]["id"] == "S001"
assert catalog.search("sneekers")[0]["id"] == "S001"          # typo tolerated
assert catalog.find("S006")["stock"] is None                    # untracked stock

# phone numbers
assert wa.normalize_phone("0712345678") == "254712345678"
assert wa.normalize_phone("+254 712 345 678") == "254712345678"
assert wa.normalize_phone("12345") is None

# cart -> order -> paid (test customer = simulated payment)
phone = "test-abc"
db.save_customer(phone, cart={"S001": 2, "S002": 1}, name="Jane Wanjiku", address="Kilimani, near Yaya")
d = orders.cart_details(phone)
assert d["subtotal"] == 2500 * 2 + 900 and d["total"] == 5900 + 200, d
r = orders.place_order(phone, "0712345678")
assert r["ok"], r
oid = r["order_id"]
o = db.get_order(oid)
assert o["status"] == "paid" and o["receipt"].startswith("TEST"), o
assert catalog.find("S001")["stock"] == 10                      # 12 - 2
assert db.customer(phone)["cart"] == {}
texts = [m["text"] for m in wa.TEST_INBOX[phone]]
assert any("Payment received" in t for t in texts), texts

# status flow + notifications + rating
assert orders.set_status(oid, "delivered")[0]
ok, _ = orders.set_status(oid, "packed"); assert not ok          # can't go backwards
texts = [m["text"] for m in wa.TEST_INBOX[phone]]
assert any("delivered" in t for t in texts) and any(m["kind"] == "rating" for m in wa.TEST_INBOX[phone])
assert "Thank you" in orders.record_rating(phone, oid, 5)
assert db.rating_summary() == (5.0, 1)
assert orders.record_rating("someone-else", oid, 1).startswith("Sorry")  # can't rate others' orders

# failed payment releases stock, retry re-reserves
db.save_customer(phone, cart={"S003": 3})
oid2 = db.create_order(phone, "Jane", "x", [dict(id="S003", name="Kikoy", price=1200, qty=3)], 3600, 200, 3800, "254712345678")
orders._reserve(db.get_order(oid2)["items"])
assert catalog.find("S003")["stock"] == 5
orders.payment_failed(oid2, 1032)
assert catalog.find("S003")["stock"] == 8 and db.get_order(oid2)["status"] == "payment_failed"
r = orders.retry_payment(phone, oid2)                            # test phone -> simulated success
assert r["ok"] and db.get_order(oid2)["status"] == "paid" and catalog.find("S003")["stock"] == 5

# duplicate / underpaid callbacks
orders.payment_success(oid2, "X", None)                          # already paid -> ignored
oid3 = db.create_order(phone, "Jane", "x", [dict(id="S004", name="Case", price=450, qty=1)], 450, 200, 650, "254712345678")
orders.payment_success(oid3, "ABC", 100.0)                       # underpaid -> stays unpaid
assert db.get_order(oid3)["status"] == "awaiting_payment"

# owner commands
assert "open orders" in orders.owner_command("orders").lower() or "#" in orders.owner_command("orders")
assert "Order commands" in orders.owner_command("hello")

# ================= manual payment modes (customer pays own till/paybill/number) =================
import time as _t
settings.save({"payment_mode": "manual_sms", "pay_method": "till", "pay_number": "123456", "pay_name": "JANE SHOP"})
p2 = "test-manual"
db.save_customer(p2, cart={"S002": 1}, name="Otieno K", address="Westlands")
r = orders.place_order(p2, "")
assert r["ok"], r
o1 = r["order_id"]
instr = [m["text"] for m in wa.TEST_INBOX[p2] if "To pay" in m["text"]][0]
assert "Till number: *123456*" in instr and "KES 1,100" in instr and "JANE SHOP" in instr, instr
assert db.get_order(o1)["status"] == "awaiting_payment"

assert "doesn't look like" in orders.submit_payment_code(p2, o1, "hello")["error"]
# customer sends code first, SMS arrives afterwards -> auto confirm
r = orders.submit_payment_code(p2, o1, "I paid, code is ujb1c2d3e4 thanks")
assert r["state"] == "verifying" and db.get_order(o1)["claimed_code"] == "UJB1C2D3E4", r
SMS = "UJB1C2D3E4 Confirmed. You have received Ksh1,100.00 from JOHN DOE 0712345678 on 11/10/26 at 3:20 PM. New business balance is Ksh9,100.00."
assert orders.handle_sms(SMS, "MPESA").startswith("matched order")
assert db.get_order(o1)["status"] == "paid" and db.get_order(o1)["receipt"] == "UJB1C2D3E4"

# code cannot be reused for another order
db.save_customer(p2, cart={"S004": 1})
o2 = orders.place_order(p2, "")["order_id"]
assert "already used" in orders.submit_payment_code(p2, o2, "UJB1C2D3E4")["error"]

# SMS arrives first, customer claims later; underpayment is not accepted
assert orders.handle_sms("QWE9R8T7Y6 Confirmed. You have received Ksh100.00 from A B 0700111222 on 11/10/26.", "MPESA").startswith("stored")
r = orders.submit_payment_code(p2, o2, "QWE9R8T7Y6")
assert r["state"] == "underpaid" and db.get_order(o2)["status"] == "awaiting_payment", r
assert orders.handle_sms("ZXC1V2B3N4 Confirmed. You have received Ksh650.00 from A B 0700111222 on 11/10/26.", "MPESA").startswith("stored")
r = orders.submit_payment_code(p2, o2, "ZXC1V2B3N4")
assert r["state"] == "confirmed" and db.get_order(o2)["status"] == "paid", r

# SMS filters: outgoing payments, other senders and junk are ignored
assert orders.handle_sms("AAA1BBB2CC Confirmed. Ksh500.00 sent to SHOP 0711 on 1/1/26", "MPESA").startswith("ignored")
assert orders.handle_sms("AAA1BBB2CC Confirmed. You have received Ksh500.00 from X", "SCAMMER").startswith("ignored")
assert orders.handle_sms("Hello there", "MPESA").startswith("ignored")

# owner-confirm mode
settings.save({"payment_mode": "manual_owner", "pay_method": "paybill", "pay_number": "400200", "pay_account": ""})
db.save_customer(p2, cart={"S002": 2})
o3 = orders.place_order(p2, "")["order_id"]
instr = [m["text"] for m in wa.TEST_INBOX[p2] if "Paybill" in m["text"]][-1]
assert "Business number: *400200*" in instr and f"ORDER{o3}" in instr, instr
assert orders.submit_payment_code(p2, o3, "KLM3N4P5Q6")["state"] == "owner_checking"
assert "re-check" in orders.owner_command(f"no {o3}")
assert db.get_order(o3)["claimed_code"] == "" and db.get_order(o3)["status"] == "awaiting_payment"
orders.submit_payment_code(p2, o3, "KLM3N4P5Q6")
assert "marked as paid" in orders.owner_command(f"OK {o3}") and db.get_order(o3)["status"] == "paid"

# unpaid orders expire and give the stock back; retry reopens them
db.save_customer(p2, cart={"S005": 2})
before = catalog.find("S005")["stock"]
o4 = orders.place_order(p2, "")["order_id"]
assert catalog.find("S005")["stock"] == before - 2
with db.conn() as c:
    c.execute("UPDATE orders SET updated=? WHERE id=?", (int(_t.time()) - 7200, o4))
orders.expire_unpaid()
assert db.get_order(o4)["status"] == "payment_failed" and catalog.find("S005")["stock"] == before
assert orders.submit_payment_code(p2, o4, "RTY6U7I8O9")["ok"]              # claiming a code reopens it
assert db.get_order(o4)["status"] == "awaiting_payment" and catalog.find("S005")["stock"] == before - 2
os.environ["RENDER_EXTERNAL_URL"] = "https://shop-x.onrender.com"
settings.save({"public_url": ""})
assert settings.get("public_url") == "https://shop-x.onrender.com"
print("ALL TESTS PASSED")
