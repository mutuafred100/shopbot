"""Order logic: cart, payment, status changes, and the messages sent at each step.
The AI never decides whether money was received - only the M-Pesa callback/query does."""
from __future__ import annotations

import logging
import re
import threading
import time

from . import catalog, db, mpesa, settings, whatsapp as wa
from .settings import money

log = logging.getLogger("shop.orders")

# status -> statuses the owner can move it to
NEXT = {
    "paid": {"packed", "out_for_delivery", "delivered", "cancelled"},
    "packed": {"out_for_delivery", "delivered", "cancelled"},
    "out_for_delivery": {"delivered", "cancelled"},
}
STOCK_HELD = {"awaiting_payment", "paid", "packed", "out_for_delivery", "delivered"}

FAIL_REASONS = {
    1: "the M-Pesa balance was not enough",
    1032: "the payment request was cancelled",
    1037: "the payment request timed out (no PIN entered)",
    2001: "the wrong M-Pesa PIN was entered",
}


# ---------- cart ----------
def cart_details(phone: str) -> dict:
    cust = db.customer(phone)
    lines, subtotal, problems = [], 0.0, []
    for pid, qty in cust["cart"].items():
        p = catalog.find(pid)
        if not p:
            problems.append(f"Item {pid} is no longer available and was left out.")
            continue
        if p["stock"] is not None and p["stock"] < qty:
            problems.append(f"Only {p['stock']} of {p['name']} in stock (cart has {qty}).")
        line_total = p["price"] * qty
        subtotal += line_total
        lines.append(dict(id=pid, name=p["name"], price=p["price"], qty=qty, line_total=line_total))
    fee = float(settings.get("delivery_fee") or 0) if lines else 0.0
    return dict(lines=lines, subtotal=subtotal, delivery_fee=fee, total=subtotal + fee, problems=problems,
                name=cust["name"], address=cust["address"])


def format_items(items: list[dict]) -> str:
    return "\n".join(f"- {i['qty']} x {i['name']} ({money(i['price'])} each)" for i in items)


# ---------- stock helpers ----------
def _reserve(items):
    for i in items:
        db.reduce_stock(i["id"], i["qty"])


def _release(items):
    for i in items:
        if db.stock_of(i["id"]) is not None:
            db.restore_stock(i["id"], i["qty"])


# ---------- placing an order ----------
def stk_mode() -> bool:
    return settings.get("payment_mode") == "stk"


def payment_instructions(order: dict) -> str:
    """Exact payment steps, sent by our code (not the AI) so numbers can never be garbled."""
    amount = money(order["total"])
    number, method, name = settings.get("pay_number"), settings.get("pay_method"), settings.get("pay_name")
    shown = f" ({name})" if name else ""
    if method == "paybill":
        account = settings.get("pay_account") or f"ORDER{order['id']}"
        how = f"1. M-Pesa > Lipa na M-Pesa > *Paybill*\n2. Business number: *{number}*{shown}\n3. Account number: *{account}*\n4. Amount: *{amount}*"
    elif method == "send_money":
        how = f"1. M-Pesa > *Send Money*\n2. Phone number: *{number}*{shown}\n3. Amount: *{amount}*"
    else:
        how = f"1. M-Pesa > Lipa na M-Pesa > *Buy Goods and Services*\n2. Till number: *{number}*{shown}\n3. Amount: *{amount}*"
    return (f"🧾 Order #{order['id']} - total {amount}\n\nTo pay:\n{how}\n\n"
            "After paying you'll get an M-Pesa SMS. Reply here with the *confirmation code* "
            "(10 letters/numbers, like UJB1C2D3E4) or paste the whole SMS, and I'll confirm your order. ✅")


def place_order(phone: str, mpesa_phone_raw: str = "") -> dict:
    mpesa_phone = ""
    if stk_mode():
        mpesa_phone = wa.normalize_phone(mpesa_phone_raw)
        if not mpesa_phone and wa.is_test(phone):
            mpesa_phone = "254708374149"  # Safaricom's public sandbox test number
        if not mpesa_phone:
            return {"ok": False, "error": "That M-Pesa number doesn't look right. Ask the customer for a number like 0712345678."}
    d = cart_details(phone)
    if not d["lines"]:
        return {"ok": False, "error": "The cart is empty."}
    if d["problems"]:
        return {"ok": False, "error": " ".join(d["problems"])}
    if not d["name"] or not d["address"]:
        return {"ok": False, "error": "Delivery name and address are missing. Save them first with set_delivery_details."}
    if not stk_mode() and not settings.get("pay_number"):
        return {"ok": False, "error": "The shop has not set up its payment number yet. Use talk_to_human."}

    items = [dict(id=l["id"], name=l["name"], price=l["price"], qty=l["qty"]) for l in d["lines"]]
    order_id = db.create_order(phone, d["name"], d["address"], items, d["subtotal"], d["delivery_fee"], d["total"], mpesa_phone)
    _reserve(items)
    db.save_customer(phone, cart={})
    return _charge(db.get_order(order_id), mpesa_phone)


def _charge(order: dict, mpesa_phone: str) -> dict:
    if not stk_mode():  # customer pays the till/paybill/number themselves
        wa.send_text(order["phone"], payment_instructions(order))
        return {"ok": True, "order_id": order["id"], "total": money(order["total"]),
                "message": "The exact payment steps were already sent to the customer in a separate message - do NOT repeat numbers. "
                           "Briefly tell them to pay and then reply with the M-Pesa confirmation code (or paste the SMS)."}
    if wa.is_test(order["phone"]) and not settings.get("mpesa_consumer_key"):  # test chat without Daraja keys: pretend they paid
        db.update_order(order["id"], checkout_id=f"TEST-{order['id']}", mpesa_phone=mpesa_phone)
        payment_success(order["id"], f"TEST{order['id']:05d}", order["total"])
        return {"ok": True, "order_id": order["id"], "message": "TEST MODE: payment simulated and confirmed."}
    checkout_id, err = mpesa.stk_push(mpesa_phone, round(order["total"]), order["id"])
    if not checkout_id:
        payment_failed(order["id"], None, silent=True)
        return {"ok": False, "order_id": order["id"], "error": f"Could not send the M-Pesa prompt: {err}"}
    db.update_order(order["id"], checkout_id=checkout_id, mpesa_phone=mpesa_phone, status="awaiting_payment")
    return {"ok": True, "order_id": order["id"],
            "message": f"M-Pesa prompt sent to {mpesa_phone} for {money(order['total'])}. Waiting for the customer to enter their PIN."}


def retry_payment(phone: str, order_id: int, mpesa_phone_raw: str = "") -> dict:
    """Reopen an unpaid/expired order and show how to pay (or resend the STK prompt)."""
    order = db.get_order(order_id)
    if not order or order["phone"] != phone:
        return {"ok": False, "error": "Order not found."}
    if order["status"] not in ("payment_failed", "awaiting_payment"):
        return {"ok": False, "error": f"Order is already '{db.STATUS_LABELS.get(order['status'])}'."}
    mpesa_phone = ""
    if stk_mode():
        mpesa_phone = wa.normalize_phone(mpesa_phone_raw) if mpesa_phone_raw else order["mpesa_phone"]
        if not mpesa_phone:
            return {"ok": False, "error": "Need a valid M-Pesa number."}
    if order["status"] == "payment_failed":
        for i in order["items"]:
            s = db.stock_of(i["id"])
            if s is not None and s < i["qty"]:
                return {"ok": False, "error": f"Sorry, {i['name']} is no longer in stock in that quantity."}
        _reserve(order["items"])
        db.update_order(order_id, status="awaiting_payment")
    return _charge(db.get_order(order_id), mpesa_phone)


def check_payment(phone: str, order_id: int) -> dict:
    order = db.get_order(order_id)
    if not order or order["phone"] != phone:
        return {"ok": False, "error": "Order not found."}
    if order["status"] == "awaiting_payment":
        if stk_mode() and order["checkout_id"] and not wa.is_test(phone):
            code, desc = mpesa.stk_query(order["checkout_id"])
            if code == 0:
                payment_success(order_id, "", None)
            elif code is not None:
                payment_failed(order_id, code)
        elif order["claimed_code"] and settings.get("payment_mode") == "manual_sms" and db.get_sms(order["claimed_code"]):
            _verify_sms(order, order["claimed_code"], db.get_sms(order["claimed_code"]))
    order = db.get_order(order_id)
    return {"ok": True, "status": order["status"], "status_text": db.STATUS_LABELS.get(order["status"]),
            "receipt": order["receipt"], "payment_code_received": bool(order["claimed_code"])}


# ---------- paying the shop's own till/paybill/number: the customer sends the M-Pesa code ----------
CODE_RE = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}\b")


def submit_payment_code(phone: str, order_id: int, raw: str) -> dict:
    order = db.get_order(order_id)
    if not order or order["phone"] != phone:
        return {"ok": False, "error": "Order not found."}
    if order["status"] == "payment_failed":
        r = retry_payment(phone, order_id)  # reopen an expired order first
        if not r.get("ok"):
            return r
        order = db.get_order(order_id)
    if order["status"] != "awaiting_payment":
        return {"ok": False, "error": f"Order #{order_id} is already '{db.STATUS_LABELS.get(order['status'])}'."}
    m = CODE_RE.search((raw or "").upper())
    if not m:
        return {"ok": False, "error": "That doesn't look like an M-Pesa code. It is 10 letters/numbers, like UJB1C2D3E4, at the start of the M-Pesa SMS."}
    code = m.group(0)
    if db.code_used_elsewhere(code, order_id):
        return {"ok": False, "error": "That code was already used for another order. Ask the customer to check it."}
    db.update_order(order_id, claimed_code=code)
    order = db.get_order(order_id)

    if settings.get("payment_mode") == "manual_sms":
        row = db.get_sms(code)
        if row:
            return _verify_sms(order, code, row)
        threading.Timer(120, _alert_if_unverified, args=(order_id,)).start()
        return {"ok": True, "state": "verifying",
                "message": "Code saved. We confirm automatically when the payment shows up (usually under a minute). The customer is messaged by itself - tell them to wait a moment."}
    _tell_owner(f"💰 Order #{order_id}: {order['name']} says they paid.\nCode: *{code}*  Expected: {money(order['total'])}\n"
                f"Check your M-Pesa messages for that code and amount, then reply *OK {order_id}* (received) or *NO {order_id}* (not received).")
    return {"ok": True, "state": "owner_checking",
            "message": "Code saved. The shop owner is checking it now; the customer will be messaged automatically. Tell them to wait a moment."}


def _alert_if_unverified(order_id: int) -> None:
    o = db.get_order(order_id)
    if o and o["status"] == "awaiting_payment" and o["claimed_code"]:
        _tell_owner(f"💰 Order #{order_id}: {o['name']} says they paid (code *{o['claimed_code']}*, expected {money(o['total'])}) "
                    f"but I haven't seen that payment SMS. Check your M-Pesa messages, then reply *OK {order_id}* or *NO {order_id}*.")


def _verify_sms(order: dict, code: str, row: dict) -> dict:
    if row["amount"] + 0.5 < order["total"]:
        _tell_owner(f"⚠️ Order #{order['id']}: code {code} is for {money(row['amount'])} but the total is {money(order['total'])}. "
                    f"Reply *OK {order['id']}* to accept it anyway or *NO {order['id']}* to reject.")
        wa.notify(order["phone"], f"We found your payment of {money(row['amount'])} but order #{order['id']} is {money(order['total'])}. The owner will contact you shortly.")
        return {"ok": True, "state": "underpaid", "message": "Payment found but it is less than the total. The customer and owner were notified; say nothing more about it."}
    db.mark_sms_used(code, order["id"])
    payment_success(order["id"], code, row["amount"])
    return {"ok": True, "state": "confirmed", "message": "Payment confirmed. The customer already gets a confirmation message automatically - just say a short thanks."}


def handle_sms(text: str, sender: str = "") -> str:
    """Called when the owner's phone forwards an M-Pesa SMS. Only incoming-payment messages count."""
    if sender and "mpesa" not in sender.lower():
        return "ignored: not from MPESA"
    low = text.lower()
    if "received" not in low or "sent to" in low or "paid to" in low:
        return "ignored: not an incoming payment"
    mc = re.search(r"\b([A-Z0-9]{10})\s+confirmed", text, re.I)
    ma = re.search(r"Ksh\.?\s?([\d,]+(?:\.\d+)?)", text, re.I)
    if not mc or not ma:
        return "ignored: could not read code/amount"
    code, amount = mc.group(1).upper(), float(ma.group(1).replace(",", ""))
    mp = re.search(r"from\s+(.+?)\s+(0\d{9}|\+?254\d{9})", text, re.I)
    db.add_sms(code, amount, f"{mp.group(1)} {mp.group(2)}" if mp else "", text)
    order = db.order_by_claimed_code(code)
    if order:
        _verify_sms(order, code, db.get_sms(code))
        return f"matched order #{order['id']}"
    return "stored: waiting for a customer to claim this code"


def owner_confirm(order_id: int) -> str:
    o = db.get_order(order_id)
    if not o:
        return f"Order #{order_id} not found."
    if o["status"] not in ("awaiting_payment", "payment_failed"):
        return f"Order #{order_id} is already '{db.STATUS_LABELS.get(o['status'])}'."
    payment_success(order_id, o["claimed_code"], None)
    return f"✅ Order #{order_id} marked as paid. Customer notified."


def owner_reject(order_id: int) -> str:
    o = db.get_order(order_id)
    if not o or o["status"] != "awaiting_payment":
        return f"Order #{order_id} is not waiting for payment."
    db.update_order(order_id, claimed_code="")
    wa.notify(o["phone"], f"We couldn't find your payment for order #{order_id}"
                          + (f" (code {o['claimed_code']})" if o["claimed_code"] else "") +
              ". Please check the code and send it again, or reply *retry* to see how to pay.")
    return f"Order #{order_id}: customer asked to re-check their code."


def expire_unpaid(minutes: int = 60) -> None:
    """Release stock of orders nobody paid for. Orders where the customer already sent a code are left for the owner."""
    cutoff = int(time.time()) - minutes * 60
    for o in db.list_orders(300):
        if o["status"] == "awaiting_payment" and not o["claimed_code"] and o["updated"] < cutoff:
            payment_failed(o["id"], None, expired=True)


# ---------- payment results (called from the M-Pesa callback) ----------
def payment_success(order_id: int, receipt: str, amount: float | None) -> None:
    order = db.get_order(order_id)
    if not order or order["status"] not in ("awaiting_payment", "payment_failed"):
        return  # already handled (M-Pesa can call twice)
    if amount is not None and amount + 0.5 < order["total"]:
        log.warning("Order %s underpaid: %s < %s", order_id, amount, order["total"])
        _tell_owner(f"⚠️ Order #{order_id}: customer paid {money(amount)} but the total is {money(order['total'])} (ref {receipt}). Please check manually.")
        return
    if order["status"] == "payment_failed":
        _reserve(order["items"])
    db.update_order(order_id, status="paid", receipt=receipt or "")
    wa.notify(order["phone"],
              f"✅ Payment received, thank you {order['name'].split()[0] if order['name'] else ''}!\n"
              f"Order #{order_id} - {money(order['total'])}" + (f" (M-Pesa ref {receipt})" if receipt else "") +
              f"\n\n{format_items(order['items'])}\n\nWe'll pack it now and keep you updated here. 📦")
    _tell_owner(
        f"🛒 NEW ORDER #{order_id} (PAID)\n"
        f"Customer: {order['name']} (+{order['phone']})\n"
        f"Deliver to: {order['address']}\n\n{format_items(order['items'])}\n\n"
        f"Total: {money(order['total'])} incl. delivery {money(order['delivery_fee'])}"
        + (f"\nM-Pesa ref: {receipt}" if receipt else "") +
        f"\n\nReply: PACK {order_id} | SHIP {order_id} | DONE {order_id} | CANCEL {order_id}")


def payment_failed(order_id: int, code: int | None, silent: bool = False, expired: bool = False) -> None:
    order = db.get_order(order_id)
    if not order or order["status"] != "awaiting_payment":
        return
    _release(order["items"])
    db.update_order(order_id, status="payment_failed")
    if silent:
        return
    if expired:
        wa.notify(order["phone"], f"⏰ Order #{order_id} was not paid in time, so we released the items. "
                                  f"Reply *retry* if you still want it.")
    else:
        reason = FAIL_REASONS.get(code, "the payment didn't go through")
        wa.notify(order["phone"], f"❌ Sorry, order #{order_id} was not paid: {reason}.\nReply *retry* and I'll send the M-Pesa prompt again.")


# ---------- status updates by the shop owner ----------
def set_status(order_id: int, new_status: str) -> tuple[bool, str]:
    order = db.get_order(order_id)
    if not order:
        return False, f"Order #{order_id} not found."
    if new_status not in NEXT.get(order["status"], set()):
        return False, f"Order #{order_id} is '{db.STATUS_LABELS.get(order['status'])}' - can't change it to '{db.STATUS_LABELS.get(new_status)}'."
    if new_status == "cancelled":
        _release(order["items"])
    db.update_order(order_id, status=new_status)
    phone, n = order["phone"], order_id
    if new_status == "packed":
        wa.notify(phone, f"📦 Order #{n} is packed and ready. We'll tell you when it's on the way.")
    elif new_status == "out_for_delivery":
        wa.notify(phone, f"🚚 Order #{n} is on its way to {order['address']}! Please keep your phone close.")
    elif new_status == "delivered":
        wa.notify(phone, f"🎉 Order #{n} has been delivered. Thank you for shopping with {settings.get('shop_name')}!")
        wa.send_rating_list(phone, n)
    elif new_status == "cancelled":
        wa.notify(phone, f"Order #{n} was cancelled. If you already paid, we will contact you about your refund shortly.")
    return True, f"Order #{n} is now: {db.STATUS_LABELS[new_status]}."


def record_rating(phone: str, order_id: int, stars: int | None) -> str:
    order = db.get_order(order_id)
    if not order or order["phone"] != phone:
        return "Sorry, I couldn't find that order."
    if stars is None:
        return "No problem! Thanks again 😊"
    db.add_rating(order_id, phone, stars)
    _tell_owner(f"⭐ Order #{order_id} rated {stars}/5 by {order['name']}.")
    return "Thank you for rating us! 💚" if stars >= 4 else "Thank you for the feedback - we'll work to do better. 🙏"


# ---------- owner ----------
def _tell_owner(text: str) -> None:
    owner = wa.normalize_phone(settings.get("owner_phone"))
    if owner:
        wa.notify(owner, text)
    else:
        log.warning("Owner phone not set. Message not delivered: %s", text[:80])


def owner_command(text: str) -> str:
    parts = text.strip().lower().split()
    if not parts:
        return owner_help()
    cmd = parts[0]
    cmds = {"pack": "packed", "packed": "packed", "ship": "out_for_delivery", "send": "out_for_delivery",
            "done": "delivered", "delivered": "delivered", "cancel": "cancelled"}
    if cmd in cmds and len(parts) >= 2 and parts[1].lstrip("#").isdigit():
        return set_status(int(parts[1].lstrip("#")), cmds[cmd])[1]
    if cmd in ("ok", "confirm", "yes") and len(parts) >= 2 and parts[1].lstrip("#").isdigit():
        return owner_confirm(int(parts[1].lstrip("#")))
    if cmd in ("no", "reject", "notpaid") and len(parts) >= 2 and parts[1].lstrip("#").isdigit():
        return owner_reject(int(parts[1].lstrip("#")))
    if cmd in ("orders", "list", "new"):
        active = [o for o in db.list_orders(30) if o["status"] in ("paid", "packed", "out_for_delivery")]
        if not active:
            return "No open orders right now. 👍"
        return "\n\n".join(f"#{o['id']} - {db.STATUS_LABELS[o['status']]}\n{o['name']}: " +
                           ", ".join(f"{i['qty']}x {i['name']}" for i in o["items"]) +
                           f"\n{money(o['total'])} - {o['address']}" for o in active)
    return owner_help()


def owner_help() -> str:
    return ("Order commands:\n*ORDERS* - list open orders\n*PACK 12* - order 12 is packed\n"
            "*SHIP 12* - out for delivery\n*DONE 12* - delivered\n*CANCEL 12* - cancel it\n*OK 12* / *NO 12* - payment received / not received")
