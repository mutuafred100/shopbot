"""The AI shop assistant: the AI talks to the customer and uses 'tools' to look up products,
manage the cart and start payment. All real actions happen in our own code (orders.py)."""
from __future__ import annotations

import json
import logging

from . import catalog, db, llm, orders, settings, whatsapp as wa
from .settings import money

log = logging.getLogger("shop.agent")
MAX_HISTORY = 16  # messages remembered per customer

TOOLS = [
    {"name": "search_products",
     "description": "Search the shop's products by words (name, category, description). Empty query lists the first products. Returns price and stock.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "send_product_photo",
     "description": "Send the customer a photo of a product (only if it has an image).",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    {"name": "add_to_cart",
     "description": "Add a product to the customer's cart (or change quantity with set_quantity).",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
                      "required": ["product_id", "quantity"]}},
    {"name": "set_quantity",
     "description": "Set the exact quantity of a product in the cart. Use 0 to remove it.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 0}},
                      "required": ["product_id", "quantity"]}},
    {"name": "view_cart",
     "description": "Show the cart with totals, delivery fee and saved delivery details.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_delivery_details",
     "description": "Save the customer's name and delivery address/area (with landmark).",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}, "required": ["name", "address"]}},
    {"name": "place_order_and_request_payment",
     "description": "ONLY after the customer clearly confirmed the cart, total and delivery details. Creates the order and starts payment: the customer is sent the exact payment steps (or an M-Pesa pop-up when the shop uses STK push, in which case mpesa_number is required).",
     "input_schema": {"type": "object", "properties": {"mpesa_number": {"type": "string", "description": "Customer's M-Pesa number. Only needed when the shop uses the M-Pesa pop-up."}}}},
    {"name": "submit_payment_code",
     "description": "The customer says they paid and gives the M-Pesa confirmation code (10 letters/numbers, e.g. UJB1C2D3E4) or pastes the confirmation SMS. Pass whatever they sent as code_or_sms.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}, "code_or_sms": {"type": "string"}}, "required": ["order_id", "code_or_sms"]}},
    {"name": "my_orders",
     "description": "List this customer's recent orders with their current status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_payment_status",
     "description": "Check whether an order's M-Pesa payment went through (use if the customer says they paid but you have no confirmation).",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]}},
    {"name": "retry_payment",
     "description": "Reopen an unpaid or expired order and show the payment steps again (or resend the M-Pesa pop-up).",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}, "mpesa_number": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "talk_to_human",
     "description": "Pass the chat to the shop owner (complaints, refunds, special requests, anything you cannot handle).",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


def _system_prompt(phone: str) -> str:
    cust = db.customer(phone)
    known = f"Saved details for this customer: name={cust['name'] or 'unknown'}, address={cust['address'] or 'unknown'}." if (cust["name"] or cust["address"]) else "This customer has no saved details yet."
    cats = ", ".join(catalog.categories()) or "n/a"
    if orders.stk_mode():
        stk_ask = " and which M-Pesa number to pay with"
        pay_rules = ("- Payment is an M-Pesa pop-up on their phone. Never say a payment is received yourself: the system sends the confirmation automatically. "
                     "After sending the prompt, tell them to check their phone and enter their M-Pesa PIN.")
    else:
        stk_ask = ""
        pay_rules = ("- Customers pay the shop's own M-Pesa number themselves. The system sends them the exact payment steps by itself - never type payment numbers yourself. "
                     "When they send an M-Pesa code or paste the confirmation SMS, call submit_payment_code with their order id. "
                     "Never say a payment is received yourself: the system sends the confirmation automatically once it is verified; until then tell them it is being checked.")
    return f"""You are the friendly WhatsApp shopping assistant for "{settings.get('shop_name')}", an online shop in Kenya.

STYLE
- Warm, short, simple messages like a helpful shopkeeper on WhatsApp. 1-4 short lines. A few emojis are fine.
- Reply in the customer's language (English, Swahili or a mix/Sheng). Keep wording simple.
- WhatsApp formatting only: *bold*. No markdown tables, headers or links in brackets.
- Show prices as "{settings.get('currency')} 1,200".

RULES
- Never invent products, prices, stock, discounts or delivery times. Use the tools. If something isn't in the shop list, say so and suggest similar items.
- Product categories available: {cats}.
- Delivery: flat fee {money(float(settings.get('delivery_fee') or 0))}. {settings.get('delivery_notes')}
- Selling flow: help them choose -> add to cart -> collect full name and delivery address (area + landmark) -> read back the cart, delivery details and TOTAL and ask for a clear "yes"{stk_ask} -> then call place_order_and_request_payment.
- Never call place_order_and_request_payment before the customer has clearly confirmed.
{pay_rules}
- If payment didn't go through or the order expired, offer to retry (retry_payment). If they say they paid but you saw no confirmation, use check_payment_status.
- Order progress updates and rating requests are sent automatically; you only answer questions about status using my_orders.
- For complaints, refunds, bulk/special orders, or anything you can't do, use talk_to_human and tell them the owner will reply soon.
- Be honest if you don't know. Stay on topic (shopping); politely decline unrelated requests.

{known}"""


# ---------- tools ----------
def _fmt_product(p: dict) -> dict:
    stock = p["stock"]
    return {"id": p["id"], "name": p["name"], "price": money(p["price"]), "category": p["category"],
            "description": p["description"], "in_stock": True if stock is None else stock > 0,
            "stock_left": stock, "has_photo": bool(p["image"])}


def _run_tool(phone: str, name: str, args: dict) -> dict:
    cust = db.customer(phone)
    if name == "search_products":
        found = catalog.search(args.get("query", ""))
        return {"products": [_fmt_product(p) for p in found]} if found else {"products": [], "note": "No matching products."}
    if name == "send_product_photo":
        p = catalog.find(args["product_id"])
        if not p or not p["image"]:
            return {"ok": False, "error": "No photo for that product."}
        return {"ok": wa.send_image(phone, p["image"], f"{p['name']} - {money(p['price'])}")}
    if name in ("add_to_cart", "set_quantity"):
        p = catalog.find(args["product_id"])
        if not p:
            return {"ok": False, "error": "Unknown product id. Search first."}
        cart = dict(cust["cart"])
        qty = int(args["quantity"])
        new_qty = cart.get(p["id"], 0) + qty if name == "add_to_cart" else qty
        if new_qty > 0 and p["stock"] is not None and new_qty > p["stock"]:
            return {"ok": False, "error": f"Only {p['stock']} of {p['name']} in stock."}
        if new_qty <= 0:
            cart.pop(p["id"], None)
        else:
            cart[p["id"]] = new_qty
        db.save_customer(phone, cart=cart)
        return {"ok": True, **_cart_for_ai(phone)}
    if name == "view_cart":
        return _cart_for_ai(phone)
    if name == "set_delivery_details":
        db.save_customer(phone, name=args["name"].strip(), address=args["address"].strip())
        return {"ok": True}
    if name == "place_order_and_request_payment":
        return orders.place_order(phone, args.get("mpesa_number", ""))
    if name == "submit_payment_code":
        return orders.submit_payment_code(phone, int(args["order_id"]), args["code_or_sms"])
    if name == "my_orders":
        return {"orders": [{"order_id": o["id"], "status": db.STATUS_LABELS[o["status"]], "total": money(o["total"]),
                            "items": ", ".join(f"{i['qty']}x {i['name']}" for i in o["items"])}
                           for o in db.list_orders(5, phone)]}
    if name == "check_payment_status":
        return orders.check_payment(phone, int(args["order_id"]))
    if name == "retry_payment":
        return orders.retry_payment(phone, int(args["order_id"]), args.get("mpesa_number", ""))
    if name == "talk_to_human":
        owner = wa.normalize_phone(settings.get("owner_phone"))
        if owner:
            wa.notify(owner, f"🙋 Customer needs you: +{phone} ({cust['name'] or 'unknown'})\nReason: {args.get('reason', '')}\nPlease reply to them on WhatsApp.")
        return {"ok": True}
    return {"ok": False, "error": f"Unknown tool {name}"}


def _cart_for_ai(phone: str) -> dict:
    d = orders.cart_details(phone)
    return {"items": [{"id": l["id"], "name": l["name"], "qty": l["qty"], "unit_price": money(l["price"]), "line_total": money(l["line_total"])} for l in d["lines"]],
            "subtotal": money(d["subtotal"]), "delivery_fee": money(d["delivery_fee"]), "total": money(d["total"]),
            "problems": d["problems"], "delivery_name": d["name"], "delivery_address": d["address"]}


# ---------- the conversation ----------
def _safe_tool(phone: str, name: str, args: dict) -> dict:
    try:
        return _run_tool(phone, name, args or {})
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"ok": False, "error": f"Something went wrong: {e}"}


def reply_to(phone: str, text: str) -> str:
    """Run one customer message through the chosen AI (with tool use) and return the text reply."""
    cust = db.customer(phone)
    history = cust["history"][-MAX_HISTORY:]
    messages = history + [{"role": "user", "content": text}]
    answer = llm.run(_system_prompt(phone), messages, TOOLS, lambda n, a: _safe_tool(phone, n, a))
    if not answer:
        answer = "Sorry, I got a bit confused. Could you say that again? 🙏"
    new_history = (history + [{"role": "user", "content": text}, {"role": "assistant", "content": answer}])[-MAX_HISTORY:]
    while new_history and new_history[0]["role"] != "user":  # history must start with a customer turn
        new_history.pop(0)
    db.save_customer(phone, history=new_history)
    return answer


def handle_customer_text(phone: str, text: str) -> None:
    text = text.strip()[:1000]
    low = text.lower()
    if low.startswith("rate ") and wa.is_test(phone):  # test-chat shortcut: "rate 12 5"
        p = low.split()
        if len(p) == 3 and p[1].isdigit() and p[2].isdigit():
            wa.send_text(phone, orders.record_rating(phone, int(p[1]), int(p[2])))
            return
    if not llm.api_key():
        wa.send_text(phone, "The shop assistant isn't fully set up yet. Please try again later 🙏")
        return
    try:
        wa.send_text(phone, reply_to(phone, text))
    except Exception:
        log.exception("agent failed")
        wa.send_text(phone, "Sorry, I'm having a small problem right now. Please try again in a minute 🙏")
