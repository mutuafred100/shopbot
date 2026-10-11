# WhatsApp Shop Assistant

A WhatsApp AI sales assistant for a small shop: answers product questions from your Excel/Google Sheet, builds the cart, takes payment either by **the customer paying your Till/Paybill/number** (confirmed by you or automatically from the M-Pesa SMS) or by an **M-Pesa STK push** (Daraja, optional), notifies the owner, keeps the buyer updated until delivery, and asks for an optional rating.

**Non-technical owner?** Read `SETUP_GUIDE.md`. All settings are entered on the web page `/admin/setup`.

## Layout
- `app/main.py` – web server: WhatsApp webhook, M-Pesa callback, admin pages
- `app/agent.py` – the AI assistant + tools (search, cart, delivery details, payment)
- `app/llm.py` – AI providers: Claude, OpenAI, Google Gemini, Z.ai (GLM), or any OpenAI-compatible service
- `app/orders.py` – order/payment/status rules and the customer & owner messages
- `app/mpesa.py`, `app/whatsapp.py` – Safaricom Daraja (optional STK push) and Meta Cloud API
- `passenger_wsgi.py`, `requirements-cpanel.txt` – for cPanel "Setup Python App" hosting
- `app/catalog.py`, `app/sample.py` – product sheet reader, sample sheet
- `tests_logic.py`, `tests_llm.py` – offline self-tests (business logic, AI provider layer)

## Run locally
```
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000/admin  (use ngrok for WhatsApp/M-Pesa callbacks)
```
Safety design: the AI can only call the tools above; payment is confirmed solely by the M-Pesa callback (amount checked), never by chat text; stock is reserved at order time and released on failed payment.
