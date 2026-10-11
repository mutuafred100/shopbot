"""Web server: WhatsApp webhook, M-Pesa callback, and the owner's admin pages."""
import hashlib
import hmac
import html
import json
import logging
import secrets as pysecrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import agent, catalog, db, llm, mpesa, orders, sample, settings, whatsapp as wa
from .settings import money

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("shop.main")


_started = False


def startup() -> None:
    """Create tables/secrets and start the cleanup thread. Safe to call more than once."""
    global _started
    if _started:
        return
    _started = True
    settings.ensure_secrets()
    db.init()

    def sweeper():  # every 5 minutes, release stock of orders nobody paid for
        while True:
            time.sleep(300)
            try:
                orders.expire_unpaid()
            except Exception:
                log.exception("expire_unpaid failed")

    threading.Thread(target=sweeper, daemon=True).start()


@asynccontextmanager
async def lifespan(app):
    startup()
    yield


app = FastAPI(title="WhatsApp Shop Assistant", lifespan=lifespan)
security = HTTPBasic(auto_error=False)
EAT = timezone(timedelta(hours=3))  # Kenya time


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


# =====================================================================
# WhatsApp webhook
# =====================================================================
@app.get("/webhook")
def verify_webhook(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.get("verify_token"):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verify token does not match")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    secret = settings.get("whatsapp_app_secret")
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected):
            raise HTTPException(403, "Bad signature")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"status": "ignored"}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                background.add_task(process_message, msg)
    return {"status": "ok"}  # always answer fast so WhatsApp doesn't resend


def process_message(msg: dict) -> None:
    try:
        if db.seen_before(msg.get("id", "")):
            return
        sender = msg["from"]
        kind = msg.get("type")
        text, reply_id = "", ""
        if kind == "text":
            text = msg["text"]["body"]
        elif kind == "interactive":
            inter = msg["interactive"]
            picked = inter.get("list_reply") or inter.get("button_reply") or {}
            reply_id, text = picked.get("id", ""), picked.get("title", "")
        else:
            wa.send_text(sender, "Hi! 😊 I can only read text messages for now. Please type what you're looking for.")
            return

        owner = wa.normalize_phone(settings.get("owner_phone"))
        if owner and sender == owner:
            wa.send_text(sender, orders.owner_command(text))
            return
        if reply_id.startswith("rate:"):
            _, oid, val = reply_id.split(":")
            wa.send_text(sender, orders.record_rating(sender, int(oid), None if val == "skip" else int(val)))
            return
        agent.handle_customer_text(sender, text)
    except Exception:
        log.exception("could not process message")


# =====================================================================
# M-Pesa callback
# =====================================================================
@app.post("/mpesa/callback/{token}")
async def mpesa_callback(token: str, request: Request, background: BackgroundTasks):
    if not pysecrets.compare_digest(token, settings.get("callback_token")):
        raise HTTPException(403)
    try:
        cb = (await request.json())["Body"]["stkCallback"]
        order = db.order_by_checkout(cb["CheckoutRequestID"])
        if order:
            if int(cb["ResultCode"]) == 0:
                meta = {i["Name"]: i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
                background.add_task(orders.payment_success, order["id"], str(meta.get("MpesaReceiptNumber", "")),
                                    float(meta["Amount"]) if meta.get("Amount") is not None else None)
            else:
                background.add_task(orders.payment_failed, order["id"], int(cb["ResultCode"]))
    except Exception:
        log.exception("bad M-Pesa callback")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


async def _extract_sms(request: Request):
    """SMS-forwarder apps all post slightly different shapes; accept JSON, form or plain text."""
    data = {}
    ctype = request.headers.get("content-type", "")
    try:
        if "json" in ctype:
            data = await request.json()
        elif "form" in ctype:
            data = dict(await request.form())
        else:
            raw = (await request.body()).decode("utf-8", "ignore")
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"message": raw}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"message": str(data)}
    low = {str(k).lower(): v for k, v in {**dict(request.query_params), **data}.items()}
    text = next((str(low[k]) for k in ("message", "text", "body", "content", "msg", "sms") if low.get(k)), "")
    if not text:
        text = " ".join(str(v) for v in low.values() if isinstance(v, str))
    sender = str(next((low[k] for k in ("from", "sender", "address", "number") if low.get(k)), ""))
    return text, sender


@app.post("/sms/{token}")
async def sms_in(token: str, request: Request, background: BackgroundTasks):
    """The owner's Android phone forwards each M-Pesa SMS here (mode: automatic confirmation)."""
    if not pysecrets.compare_digest(token, settings.get("sms_token")):
        raise HTTPException(403)
    text, sender = await _extract_sms(request)
    background.add_task(orders.handle_sms, text, sender)
    return {"ok": True}


@app.get("/")
def home():
    return PlainTextResponse("Shop assistant is running. Owner: go to /admin")


@app.get("/health")
def health():
    return {"ok": True}


# =====================================================================
# Admin pages (owner)
# =====================================================================
def admin(creds: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    password = settings.get("admin_password")
    if not password:
        return  # first run: no password yet (the dashboard tells the owner to set one)
    ok = creds and pysecrets.compare_digest(creds.username, "admin") and pysecrets.compare_digest(creds.password, password)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Shop admin"'})


CSS = """
*{box-sizing:border-box}body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f4f6f5;color:#1c2b24}
header{background:#1f7a4d;color:#fff;padding:12px 18px;display:flex;gap:16px;flex-wrap:wrap;align-items:center}
header b{font-size:18px;margin-right:auto}header a{color:#fff;text-decoration:none;opacity:.9}header a:hover{opacity:1;text-decoration:underline}
main{max-width:980px;margin:18px auto;padding:0 14px}
.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px #0001}
h2{margin:0 0 10px;font-size:18px}h3{margin:14px 0 6px;font-size:15px;color:#1f7a4d}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #e6ebe8;vertical-align:top}
label{display:block;font-weight:600;margin-top:12px}small{color:#5c6b64;display:block;margin:2px 0 4px}
input[type=text],input[type=password],select,textarea{width:100%;padding:9px;border:1px solid #c5d0ca;border-radius:6px;font-size:15px}
button,.btn{background:#1f7a4d;color:#fff;border:0;padding:8px 14px;border-radius:6px;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
button.grey{background:#6b7b73}button.red{background:#b3372f}
.warn{background:#fff4d6;border-left:4px solid #e0a800;padding:10px 12px;margin-bottom:10px;border-radius:4px}
.bad{background:#fde4e1;border-left:4px solid #b3372f;padding:10px 12px;margin-bottom:10px;border-radius:4px}
.good{background:#e3f5ea;border-left:4px solid #1f7a4d;padding:10px 12px;margin-bottom:10px;border-radius:4px}
code{background:#eef2f0;padding:2px 5px;border-radius:4px;word-break:break-all}
.pill{padding:2px 8px;border-radius:99px;font-size:12px;background:#e6ebe8}
.st-paid{background:#ffe9b3}.st-delivered{background:#cdeedb}.st-cancelled,.st-payment_failed{background:#f8d2cf}.st-out_for_delivery{background:#d3e6fa}.st-packed{background:#e5dcfa}
#chat{height:420px;overflow:auto;background:#e8efe9;border-radius:8px;padding:10px}
.m{max-width:80%;padding:8px 11px;border-radius:10px;margin:6px 0;white-space:pre-wrap;line-height:1.35}
.m.me{background:#d6f5c2;margin-left:auto}.m.bot{background:#fff}
"""


def page(title: str, body: str) -> HTMLResponse:
    nav = ('<header><b>🛍️ ' + esc(settings.get("shop_name")) + ' - assistant</b>'
           '<a href="/admin">Orders</a><a href="/admin/products">Products</a><a href="/admin/setup">Settings</a>'
           '<a href="/admin/chat">Test chat</a></header>')
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{nav}<main>{body}</main></body></html>")


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, EAT).strftime("%d %b %H:%M")


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin)])
def dashboard():
    todo = settings.missing_items()
    body = ""
    if not settings.get("admin_password"):
        body += '<div class="bad"><b>Set an admin password now.</b> Until you do, anyone who finds this page can open it. <a href="/admin/setup">Go to Settings</a></div>'
    if todo:
        body += '<div class="warn"><b>Still to do:</b><ul>' + "".join(f"<li>{esc(t)}</li>" for t in todo) + '</ul><a href="/admin/setup">Open Settings</a></div>'
    else:
        body += '<div class="good">✅ Setup looks complete. Try the <a href="/admin/chat">test chat</a>, then message your WhatsApp number from a phone.</div>'
    if not catalog.load():
        body += '<div class="warn">No products found. <a href="/admin/products">Add your products</a>.</div>'
    avg, n = db.rating_summary()
    allo = db.list_orders(100)
    open_n = sum(1 for o in allo if o["status"] in ("paid", "packed", "out_for_delivery"))
    body += f'<div class="card"><b>{open_n}</b> open orders &nbsp;·&nbsp; <b>{len(allo)}</b> recent orders &nbsp;·&nbsp; rating: <b>{avg:.1f}/5</b> from {n} reviews</div>'

    rows = ""
    for o in allo:
        acts = ""
        if o["status"] == "awaiting_payment" and not orders.stk_mode():
            acts += (f'<form method=post action="/admin/orders/{o["id"]}/confirm_payment" style="display:inline"><button>✅ Payment received</button></form> '
                     f'<form method=post action="/admin/orders/{o["id"]}/reject_payment" style="display:inline"><button class=grey>Not received</button></form> ')
        for label, st in (("Packed", "packed"), ("Out for delivery", "out_for_delivery"), ("Delivered", "delivered")):
            if st in orders.NEXT.get(o["status"], set()):
                acts += f'<form method=post action="/admin/orders/{o["id"]}/{st}" style="display:inline"><button>{label}</button></form> '
        if "cancelled" in orders.NEXT.get(o["status"], set()):
            acts += f'<form method=post action="/admin/orders/{o["id"]}/cancelled" style="display:inline" onsubmit="return confirm(\'Cancel this order? Refund must be done by you in M-Pesa.\')"><button class=red>Cancel</button></form>'
        items = "<br>".join(f"{i['qty']} × {esc(i['name'])}" for i in o["items"])
        stars = db.rating_for(o["id"])
        rows += (f"<tr><td>#{o['id']}<br><small>{fmt_time(o['created'])}</small></td>"
                 f"<td>{esc(o['name'])}<br><small>+{esc(o['phone'])}<br>{esc(o['address'])}</small></td><td>{items}</td>"
                 f"<td>{esc(money(o['total']))}<br><small>{esc(o['receipt'] or ('code: ' + o['claimed_code'] if o['claimed_code'] else ''))}</small></td>"
                 f"<td><span class='pill st-{o['status']}'>{esc(db.STATUS_LABELS[o['status']])}</span>"
                 f"{'<br>' + '⭐' * stars if stars else ''}</td><td>{acts}</td></tr>")
    body += ('<div class="card"><h2>Orders</h2><div style="overflow-x:auto"><table><tr><th>Order</th><th>Customer</th><th>Items</th><th>Total</th><th>Status</th><th>Update</th></tr>'
             + (rows or '<tr><td colspan=6>No orders yet.</td></tr>') + "</table></div>"
             "<small>Customers get a WhatsApp message automatically each time you press a button. You can also reply to the bot on WhatsApp: ORDERS, PACK 12, SHIP 12, DONE 12.</small></div>")
    return page("Orders", body)


@app.post("/admin/orders/{order_id}/{status}", dependencies=[Depends(admin)])
def change_status(order_id: int, status: str, background: BackgroundTasks):
    if status == "confirm_payment":
        orders.owner_confirm(order_id)
    elif status == "reject_payment":
        orders.owner_reject(order_id)
    else:
        orders.set_status(order_id, status)
    return RedirectResponse("/admin", status_code=303)


# ---------- settings ----------
@app.get("/admin/setup", response_class=HTMLResponse, dependencies=[Depends(admin)])
def setup_form(saved: int = 0, ai: str = ""):
    base = settings.get("public_url").rstrip("/") or "https://YOUR-ADDRESS"
    body = ""
    if saved:
        body += '<div class="good">✅ Saved.</div>'
    if ai:
        body += f'<div class="{"good" if ai.startswith("✅") else "bad"}">{esc(ai)}</div>'
    body += (f'<div class="card"><h2>Addresses to paste into other websites</h2>'
             f'<label>WhatsApp webhook (Meta &gt; WhatsApp &gt; Configuration)</label><code>{esc(base)}/webhook</code>'
             f'<label>Verify token</label><code>{esc(settings.get("verify_token"))}</code>')
    if settings.get("payment_mode") == "manual_sms":
        body += f'<label>Address your phone forwards M-Pesa SMS to (see the guide)</label><code>{esc(base)}/sms/{esc(settings.get("sms_token"))}</code>'
    body += '</div>'
    body += ('<div class="card"><b>AI connection:</b> <form method=post action="/admin/setup/test-ai" style="display:inline">'
             '<button class=grey>Test AI connection</button></form> <small style="display:inline">Save your settings first, then test.</small></div>')
    body += '<form method=post action="/admin/setup"><div class="card">'
    group = None
    for f in sorted(settings.FIELDS, key=lambda x: x["group"]):
        if f["group"] != group:
            group = f["group"]
            body += f"<h3>{esc(group)}</h3>"
        val = settings.get(f["key"]) if f["default"] == "" or f.get("options") else settings.get(f["key"])
        body += f'<label>{esc(f["label"])}</label><small>{esc(f["help"])}</small>'
        if f.get("options"):
            body += f'<select name="{f["key"]}">' + "".join(
                f'<option value="{esc(settings.option_value(o))}" {"selected" if settings.option_value(o) == val else ""}>{esc(o[1] if isinstance(o, (tuple, list)) else o)}</option>'
                for o in f["options"]) + "</select>"
        elif f.get("secret"):
            ph = "(saved - leave empty to keep)" if val else ""
            body += f'<input type=password name="{f["key"]}" placeholder="{ph}" autocomplete="new-password">'
        else:
            body += f'<input type=text name="{f["key"]}" value="{esc(val)}">'
    body += '<p><button>Save settings</button></p></div></form>'
    return page("Settings", body)


@app.post("/admin/setup/test-ai", dependencies=[Depends(admin)])
def test_ai():
    from urllib.parse import quote
    ok, msg = llm.test_connection()
    return RedirectResponse("/admin/setup?ai=" + quote(("✅ " if ok else "❌ ") + msg), status_code=303)


@app.post("/admin/setup", dependencies=[Depends(admin)])
async def setup_save(request: Request):
    form = await request.form()
    updates = {}
    for f in settings.FIELDS:
        if f["key"] not in form:
            continue
        v = str(form[f["key"]]).strip()
        if f.get("secret") and v == "":
            continue  # keep existing secret
        if f["key"] in ("public_url",):
            v = v.rstrip("/")
        if f["key"] == "owner_phone" and v:
            v = wa.normalize_phone(v) or v
        updates[f["key"]] = v
    settings.save(updates)
    catalog.load(force=True)
    return RedirectResponse("/admin/setup?saved=1", status_code=303)


# ---------- products ----------
@app.get("/admin/products", response_class=HTMLResponse, dependencies=[Depends(admin)])
def products_page(msg: str = ""):
    items = catalog.load(force=True)
    src = ("Google Sheet" if settings.get("sheet_url") else "Uploaded Excel file" if catalog.XLSX_FILE.exists() else "none yet")
    body = f'<div class="card"><h2>Products</h2><p>Source: <b>{src}</b> · {len(items)} products found.</p>'
    if msg:
        body += f'<div class="good">{esc(msg)}</div>'
    for w in catalog.warnings():
        body += f'<div class="warn">{esc(w)}</div>'
    body += ('<h3>Option A: upload an Excel file</h3>'
             '<p><a class="btn grey" href="/admin/sample.xlsx">Download sample Excel sheet</a></p>'
             '<form method=post action="/admin/products/upload" enctype="multipart/form-data">'
             '<input type=file name=file accept=".xlsx" required> <button>Upload</button></form>'
             '<small>Uploading replaces the old list. Using a Google Sheet instead? Clear the Google link below.</small>'
             '<h3>Option B: Google Sheet link</h3>'
             '<small>Open your sheet &gt; Share &gt; "Anyone with the link" can view &gt; copy link. Same columns as the sample. Changes appear within a minute.</small>'
             f'<form method=post action="/admin/products/sheet"><input type=text name=sheet_url value="{esc(settings.get("sheet_url"))}" placeholder="https://docs.google.com/spreadsheets/d/..."><p><button>Save link</button></p></form></div>')
    rows = "".join(f"<tr><td>{esc(p['id'])}</td><td>{esc(p['name'])}</td><td>{esc(money(p['price']))}</td>"
                   f"<td>{'-' if p['stock'] is None else catalog.with_stock(p)['stock']}</td><td>{esc(p['category'])}</td></tr>" for p in items)
    body += ('<div class="card"><h2>What the assistant sees</h2><div style="overflow-x:auto"><table><tr><th>ID</th><th>Name</th><th>Price</th><th>Stock now</th><th>Category</th></tr>'
             + (rows or "<tr><td colspan=5>Nothing yet.</td></tr>") + "</table></div></div>")
    return page("Products", body)


@app.post("/admin/products/upload", dependencies=[Depends(admin)])
async def products_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data[:2] == b"PK":
        return RedirectResponse("/admin/products?msg=That+does+not+look+like+an+.xlsx+file", status_code=303)
    catalog.XLSX_FILE.write_bytes(data)
    settings.save({"sheet_url": ""})
    return RedirectResponse("/admin/products?msg=Uploaded", status_code=303)


@app.post("/admin/products/sheet", dependencies=[Depends(admin)])
def products_sheet(sheet_url: str = Form("")):
    settings.save({"sheet_url": sheet_url})
    return RedirectResponse("/admin/products?msg=Saved", status_code=303)


@app.get("/admin/sample.xlsx", dependencies=[Depends(admin)])
def sample_xlsx():
    return Response(sample.build(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=products-sample.xlsx"})


# ---------- test chat (no WhatsApp needed) ----------
@app.get("/admin/chat", response_class=HTMLResponse, dependencies=[Depends(admin)])
def chat_page():
    body = """<div class="card"><h2>Test chat</h2>
<div class="warn">This pretends to be a customer. Nothing is charged: payment is simulated. The new-order alert still goes to your real WhatsApp number if it is set up.</div>
<div id=chat></div>
<p><input type=text id=msg placeholder="Type like a customer, e.g. 'Hi, do you have sneakers?'" onkeydown="if(event.key=='Enter')send()"></p>
<button onclick="send()">Send</button> <button class=grey onclick="newCustomer()">New customer</button> <small id=status></small></div>
<div class="card"><h3>Pretend the money arrived (for testing payments)</h3>
<small>When you choose "confirmed automatically from my M-Pesa SMS", the shop's phone forwards each M-Pesa SMS. This box fakes one. Use the same code the test customer typed.</small>
<input type=text id=scode placeholder="M-Pesa code, e.g. UJB1C2D3E4" style="width:48%"> <input type=text id=samt placeholder="Amount, e.g. 5900" style="width:28%"> <button onclick="simSms()">Fake SMS</button> <small id=sres></small>
<small>In the other mode ("I confirm each payment") use the green "Payment received" button on the Orders page instead.</small></div>
<script>
let phone=sessionStorage.getItem('tp')||('test-'+Math.random().toString(36).slice(2,8));sessionStorage.setItem('tp',phone);
let shown=0,box=document.getElementById('chat');
function add(who,text){const d=document.createElement('div');d.className='m '+who;d.textContent=text;box.appendChild(d);box.scrollTop=box.scrollHeight}
async function poll(){const r=await fetch('/admin/chat/messages?phone='+phone);const l=await r.json();for(;shown<l.length;shown++)add('bot',l[shown].text)}
async function send(){const i=document.getElementById('msg'),t=i.value.trim();if(!t)return;i.value='';add('me',t);
 document.getElementById('status').textContent='typing...';
 await fetch('/admin/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:phone,text:t})});
 document.getElementById('status').textContent='';await poll()}
async function simSms(){const r=await fetch('/admin/chat/sim-sms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.getElementById('scode').value,amount:document.getElementById('samt').value})});
 document.getElementById('sres').textContent=(await r.json()).result;setTimeout(poll,1500)}
function newCustomer(){sessionStorage.removeItem('tp');location.reload()}
setInterval(poll,3000);poll();
</script>"""
    return page("Test chat", body)


@app.post("/admin/chat/send", dependencies=[Depends(admin)])
def chat_send(payload: dict):
    phone = str(payload.get("phone", ""))
    if not wa.is_test(phone):
        raise HTTPException(400)
    agent.handle_customer_text(phone, str(payload.get("text", "")))
    return {"ok": True}


@app.get("/admin/chat/messages", dependencies=[Depends(admin)])
def chat_messages(phone: str):
    if not wa.is_test(phone):
        raise HTTPException(400)
    return JSONResponse(wa.TEST_INBOX.get(phone, []))


@app.post("/admin/chat/sim-sms", dependencies=[Depends(admin)])
def chat_sim_sms(payload: dict):
    code = str(payload.get("code", "")).strip().upper()
    try:
        amount = float(str(payload.get("amount", "0")).replace(",", ""))
    except ValueError:
        return {"result": "Amount must be a number."}
    sms = (f"{code} Confirmed. You have received Ksh{amount:,.2f} from JOHN DOE 0712345678 on 11/10/26 at 3:20 PM. "
           f"New business balance is Ksh{amount:,.2f}.")
    return {"result": orders.handle_sms(sms, "MPESA")}
