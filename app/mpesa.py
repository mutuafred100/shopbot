"""M-Pesa STK push (Safaricom Daraja)."""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime

import httpx

from . import settings

log = logging.getLogger("shop.mpesa")
_token = {"value": None, "exp": 0.0}


def _base() -> str:
    return "https://api.safaricom.co.ke" if settings.get("mpesa_env") == "production" else "https://sandbox.safaricom.co.ke"


def _access_token() -> str:
    if _token["value"] and time.time() < _token["exp"]:
        return _token["value"]
    r = httpx.get(
        f"{_base()}/oauth/v1/generate?grant_type=client_credentials",
        auth=(settings.get("mpesa_consumer_key"), settings.get("mpesa_consumer_secret")),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    _token.update(value=data["access_token"], exp=time.time() + int(data.get("expires_in", 3599)) - 60)
    return _token["value"]


def _passkey() -> str:
    key = settings.get("mpesa_passkey")
    if not key and settings.get("mpesa_env") != "production":
        return settings.SANDBOX_PASSKEY
    return key


def _shortcode() -> str:
    return settings.get("mpesa_shortcode") or ("174379" if settings.get("mpesa_env") != "production" else "")


def _password(ts: str) -> str:
    return base64.b64encode(f"{_shortcode()}{_passkey()}{ts}".encode()).decode()


def callback_url() -> str:
    return f"{settings.get('public_url').rstrip('/')}/mpesa/callback/{settings.get('callback_token')}"


def stk_push(phone: str, amount: int, order_id: int) -> tuple[str | None, str]:
    """Returns (checkout_request_id, error_message). Exactly one of them is empty."""
    try:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        is_till = settings.get("mpesa_type") == "till" and settings.get("mpesa_env") == "production"
        party_b = (settings.get("mpesa_till") or _shortcode()) if is_till else _shortcode()
        payload = {
            "BusinessShortCode": _shortcode(),
            "Password": _password(ts),
            "Timestamp": ts,
            "TransactionType": "CustomerBuyGoodsOnline" if is_till else "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": phone,
            "PartyB": party_b,
            "PhoneNumber": phone,
            "CallBackURL": callback_url(),
            "AccountReference": f"Order{order_id}",
            "TransactionDesc": f"{settings.get('shop_name')[:10]} order {order_id}",
        }
        r = httpx.post(f"{_base()}/mpesa/stkpush/v1/processrequest",
                       headers={"Authorization": f"Bearer {_access_token()}"}, json=payload, timeout=30)
        data = r.json()
        if r.status_code == 200 and data.get("ResponseCode") == "0":
            return data["CheckoutRequestID"], ""
        log.error("STK push rejected: %s", data)
        return None, data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa rejected the request"
    except Exception as e:
        log.exception("STK push error")
        return None, str(e)


def stk_query(checkout_id: str) -> tuple[int | None, str]:
    """Ask Safaricom what happened to a payment. Returns (result_code, description).
    0 = paid. None = still unknown / could not check."""
    try:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        r = httpx.post(
            f"{_base()}/mpesa/stkpushquery/v1/query",
            headers={"Authorization": f"Bearer {_access_token()}"},
            json={"BusinessShortCode": _shortcode(), "Password": _password(ts), "Timestamp": ts, "CheckoutRequestID": checkout_id},
            timeout=30,
        )
        data = r.json()
        if "ResultCode" in data:
            return int(data["ResultCode"]), data.get("ResultDesc", "")
        return None, data.get("errorMessage", "still processing")
    except Exception as e:
        return None, str(e)
