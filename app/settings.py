"""All configuration lives here. The shop owner fills it in on the /admin/setup web page;
values are saved in data/settings.json. Environment variables (UPPERCASE of the key) also work."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_FILE = DATA_DIR / "settings.json"

LLM_PROVIDERS = {
    "anthropic": dict(label="Anthropic (Claude)", base="https://api.anthropic.com", model="claude-sonnet-5-5"),
    "openai": dict(label="OpenAI (ChatGPT)", base="https://api.openai.com/v1", model="gpt-5-mini"),
    "gemini": dict(label="Google Gemini", base="https://generativelanguage.googleapis.com/v1beta/openai", model="gemini-flash-latest"),
    "zai": dict(label="Z.ai (GLM)", base="https://api.z.ai/api/paas/v4", model="glm-4.6"),
    "custom": dict(label="Other (OpenAI-compatible: DeepSeek, Groq, OpenRouter...)", base="", model=""),
}

# Each field: key, label, help text (plain language), secret?, default, group, optional dropdown options
FIELDS = [
    # ---- Your shop
    dict(key="shop_name", label="Shop name", help="Customers will see this name in chats.", default="My Shop", group="1. Your shop"),
    dict(key="owner_phone", label="Your WhatsApp number (shop owner)", help="Format: 2547XXXXXXXX. You will receive new-order alerts here and can update orders by replying.", default="", group="1. Your shop"),
    dict(key="currency", label="Currency", help="Usually KES.", default="KES", group="1. Your shop"),
    dict(key="delivery_fee", label="Delivery fee (flat)", help="Number only, e.g. 200. Use 0 for free delivery.", default="0", group="1. Your shop"),
    dict(key="delivery_notes", label="Delivery information", help="Told to customers when they ask, e.g. 'We deliver within Nairobi in 1-2 days. Outside Nairobi via courier.'", default="", group="1. Your shop"),
    dict(key="sheet_url", label="Google Sheet link (optional)", help="Share the sheet as 'Anyone with the link can view' and paste the link. Leave empty if you upload an Excel file on the Products page.", default="", group="1. Your shop"),
    # ---- WhatsApp
    dict(key="whatsapp_token", label="WhatsApp access token", help="From Meta (developers.facebook.com) > your app > WhatsApp > API Setup. Use a permanent token (System User).", secret=True, default="", group="2. WhatsApp"),
    dict(key="whatsapp_phone_number_id", label="WhatsApp Phone number ID", help="Shown on the same API Setup page.", default="", group="2. WhatsApp"),
    dict(key="whatsapp_app_secret", label="Meta App Secret (recommended)", help="Meta app > Settings > Basic. Used to check messages really come from WhatsApp.", secret=True, default="", group="2. WhatsApp"),
    dict(key="template_name", label="Message template name (optional)", help="Needed to message customers more than 24 hours after their last message. See SETUP_GUIDE.md, step 6.", default="", group="2. WhatsApp"),
    dict(key="template_lang", label="Template language code", help="e.g. en", default="en", group="2. WhatsApp"),
    # ---- AI
    dict(key="llm_provider", label="Which AI should answer customers?", help="Pick one. You need an account and API key with that company. Use the 'Test AI connection' button after saving.", default="anthropic", group="3. AI assistant",
         options=[(k, v["label"]) for k, v in LLM_PROVIDERS.items()]),
    dict(key="llm_api_key", label="AI API key", help="Created in the provider's console (see the guide). Kept private.", secret=True, default="", group="3. AI assistant"),
    dict(key="llm_model", label="Model name (optional)", help="Leave empty to use the suggested default. Model names change over time: if the test says 'model not found', copy a current name from the provider's website.", default="", group="3. AI assistant"),
    dict(key="llm_base_url", label="Base URL (only for 'Other')", help="e.g. https://api.deepseek.com/v1 or https://openrouter.ai/api/v1", default="", group="3. AI assistant"),
    # ---- Payments
    dict(key="payment_mode", label="How do customers pay?", help="Simplest: the customer pays your Till/Paybill/number and you confirm. 'Automatic' confirms by itself using your M-Pesa SMS (needs an Android phone, see the guide). The pop-up option needs a Safaricom Daraja account.", default="manual_owner", group="4. Payments",
         options=[("manual_owner", "Customer pays, I confirm each payment"), ("manual_sms", "Customer pays, confirmed automatically from my M-Pesa SMS"), ("stk", "M-Pesa pop-up on customer's phone (needs Daraja)")]),
    dict(key="pay_method", label="Customers pay to my...", help="Pick what you receive money on.", default="till", group="4. Payments",
         options=[("till", "Till number (Buy Goods)"), ("paybill", "Paybill"), ("send_money", "Phone number (Send Money)")]),
    dict(key="pay_number", label="Till / Paybill / phone number", help="The number customers pay to. Phone numbers: 07XXXXXXXX.", default="", group="4. Payments"),
    dict(key="pay_account", label="Paybill account number (Paybill only)", help="Leave empty to use the order number (e.g. ORDER12).", default="", group="4. Payments"),
    dict(key="pay_name", label="Name shown on M-Pesa (optional)", help="The business/person name the customer sees when paying, so they know it's right. e.g. JANE'S SHOP", default="", group="4. Payments"),
    # ---- Security / hosting
    dict(key="public_url", label="Your website address (public URL)", help="Where this program is hosted, e.g. https://myshop.onrender.com (no slash at the end).", default="", group="5. Hosting & security"),
    dict(key="admin_password", label="Admin password", help="Protects this admin area. Choose something strong. Username is: admin", secret=True, default="", group="5. Hosting & security"),
    dict(key="verify_token", label="WhatsApp verify token", help="Auto-generated. Paste it into Meta when setting the webhook.", default="", group="5. Hosting & security"),
    dict(key="sms_token", label="SMS forwarding secret", help="Auto-generated. Part of the address your phone forwards M-Pesa SMS to.", default="", group="5. Hosting & security"),
    dict(key="callback_token", label="M-Pesa callback secret", help="Auto-generated. Part of the M-Pesa callback address.", default="", group="5. Hosting & security"),
    # ---- Advanced Daraja
    dict(key="mpesa_env", label="M-Pesa mode", help="Start with 'sandbox' (fake money, for testing). Switch to 'production' when ready.", default="sandbox", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option", options=["sandbox", "production"]),
    dict(key="mpesa_type", label="Your M-Pesa account type", help="Till number (Buy Goods) or Paybill.", default="till", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option", options=["till", "paybill"]),
    dict(key="mpesa_shortcode", label="Shortcode", help="Paybill number, or the Store/Head Office number if you use a Till. Sandbox: 174379.", default="", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option"),
    dict(key="mpesa_till", label="Till number (only if type = till)", help="The Till number customers pay to.", default="", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option"),
    dict(key="mpesa_consumer_key", label="Daraja Consumer Key", help="From developer.safaricom.co.ke > My Apps.", secret=True, default="", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option"),
    dict(key="mpesa_consumer_secret", label="Daraja Consumer Secret", help="Same page as above.", secret=True, default="", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option"),
    dict(key="mpesa_passkey", label="Daraja Passkey", help="Given by Safaricom when you go live. Sandbox: leave empty (test key is built in).", secret=True, default="", group="6. Advanced: M-Pesa pop-up (Daraja) - only if you choose that option"),
]
_BY_KEY = {f["key"]: f for f in FIELDS}

def option_value(o):
    return o[0] if isinstance(o, (tuple, list)) else o


SANDBOX_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except Exception:
        return {}


def get(key: str, default: str = "") -> str:
    value = _load().get(key)
    if value not in (None, ""):
        return str(value)
    env = os.getenv(key.upper())
    if env:
        return env
    if key == "public_url" and os.getenv("RENDER_EXTERNAL_URL"):  # Render tells us our own address
        return os.getenv("RENDER_EXTERNAL_URL")
    field = _BY_KEY.get(key)
    if field and field["default"] != "":
        return field["default"]
    return default


def save(updates: dict) -> None:
    data = _load()
    for key, value in updates.items():
        if key in _BY_KEY:
            data[key] = str(value).strip()
    _FILE.write_text(json.dumps(data, indent=2))


def ensure_secrets() -> None:
    """Create the random verify/callback tokens the first time the app starts."""
    data = _load()
    changed = False
    for key in ("verify_token", "callback_token", "sms_token"):
        if not data.get(key) and not os.getenv(key.upper()):
            data[key] = secrets.token_urlsafe(16)
            changed = True
    if changed:
        _FILE.write_text(json.dumps(data, indent=2))


def money(amount: float) -> str:
    return f"{get('currency', 'KES')} {amount:,.0f}"


def missing_items() -> list[str]:
    """Plain-language checklist of what is still not set up."""
    out = []
    if not get("admin_password"):
        out.append("Set an admin password")
    if not get("owner_phone"):
        out.append("Add your WhatsApp number (to receive order alerts)")
    if not get("whatsapp_token") or not get("whatsapp_phone_number_id"):
        out.append("Add your WhatsApp token and Phone number ID")
    from . import llm
    if not llm.api_key():
        out.append("Add your AI API key")
    if get("payment_mode") == "stk":
        if not (get("mpesa_consumer_key") and get("mpesa_consumer_secret") and get("mpesa_shortcode")):
            out.append("Add your M-Pesa (Daraja) details")
        if get("mpesa_env") == "production" and not get("mpesa_passkey"):
            out.append("Add your M-Pesa passkey (needed for production)")
    elif not get("pay_number"):
        out.append("Add the Till / Paybill / phone number customers should pay to")
    if not get("public_url"):
        out.append("Add your public website address")
    return out
