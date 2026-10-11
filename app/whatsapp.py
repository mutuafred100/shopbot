"""Sending WhatsApp messages (Meta Cloud API). Phones starting with 'test-' are the
built-in test chat: their messages are kept in memory instead of being sent."""
from __future__ import annotations

import logging
import re
import threading

import httpx

from . import settings

log = logging.getLogger("shop.whatsapp")
API = "https://graph.facebook.com/v21.0"

TEST_INBOX: dict[str, list[dict]] = {}
_lock = threading.Lock()


def is_test(phone: str) -> bool:
    return phone.startswith("test-")


def normalize_phone(raw: str) -> str | None:
    """0712345678 / +254712345678 / 254712345678 / 712345678 -> 254712345678"""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if len(digits) == 9 and digits[0] in "71":
        return "254" + digits
    return None


def _test_add(phone: str, kind: str, text: str) -> bool:
    with _lock:
        TEST_INBOX.setdefault(phone, []).append({"from": "bot", "kind": kind, "text": text})
    return True


def _post(payload: dict) -> tuple[bool, str]:
    token, phone_id = settings.get("whatsapp_token"), settings.get("whatsapp_phone_number_id")
    if not token or not phone_id:
        log.warning("WhatsApp is not configured; message not sent: %s", payload.get("to"))
        return False, "not-configured"
    try:
        r = httpx.post(
            f"{API}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "to": payload.pop("to"), **payload},
            timeout=20,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
            return False, r.text
        return True, ""
    except Exception as e:
        log.exception("WhatsApp send error")
        return False, str(e)


def send_text(to: str, body: str) -> tuple[bool, str]:
    if is_test(to):
        return _test_add(to, "text", body), ""
    return _post({"to": to, "type": "text", "text": {"body": body[:4000], "preview_url": False}})


def send_image(to: str, url: str, caption: str = "") -> bool:
    if is_test(to):
        return _test_add(to, "image", f"[photo] {caption}\n{url}")
    ok, _ = _post({"to": to, "type": "image", "image": {"link": url, "caption": caption[:1000]}})
    return ok


def send_rating_list(to: str, order_id: int) -> bool:
    body = f"How was your experience with order #{order_id}? Tap below to rate us (optional)."
    if is_test(to):
        return _test_add(to, "rating", f"{body}\n[rate options: 5 / 4 / 3 / 2 / 1 / skip]  (type e.g. 'rate {order_id} 5')")
    rows = [{"id": f"rate:{order_id}:{n}", "title": "⭐" * n} for n in (5, 4, 3, 2, 1)]
    rows.append({"id": f"rate:{order_id}:skip", "title": "Skip"})
    ok, _ = _post({
        "to": to, "type": "interactive",
        "interactive": {"type": "list", "body": {"text": body},
                        "action": {"button": "Rate us", "sections": [{"title": "Your rating", "rows": rows}]}},
    })
    return ok


def _send_template(to: str, text: str) -> bool:
    name = settings.get("template_name")
    if not name:
        return False
    clean = re.sub(r"\s+", " ", text.replace("\n", " | ")).strip()[:900]
    ok, _ = _post({
        "to": to, "type": "template",
        "template": {"name": name, "language": {"code": settings.get("template_lang", "en")},
                     "components": [{"type": "body", "parameters": [{"type": "text", "text": clean}]}]},
    })
    return ok


def notify(to: str, text: str) -> bool:
    """For updates the customer did not just ask for (payment received, out for delivery...).
    WhatsApp only allows free text within 24h of the person's last message; otherwise we
    fall back to the approved template, if one is set up."""
    if is_test(to):
        return _test_add(to, "text", text)
    ok, err = send_text(to, text)
    if ok:
        return True
    if "131047" in err or "re-engage" in err.lower():
        return _send_template(to, text)
    return False
