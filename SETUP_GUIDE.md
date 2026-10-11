# Shop Assistant on WhatsApp — Setup Guide (no coding needed)

You will do five things: **(1)** put the program online, **(2)** add your products, **(3)** connect WhatsApp, **(4)** choose the AI (Claude, OpenAI, Gemini, Z.ai or another), **(5)** choose how customers pay, then test. Almost everything is typed into one **Settings** web page that the program gives you — no files to edit.

Expect 1–2 hours for steps 1–4. Paying to your own Till/Paybill/number needs no approval. The optional M-Pesa pop-up (Daraja) needs Safaricom approval that can take days, so test it in the **sandbox** first.

---

## Step 1 — Put the program online

The program must run on the internet 24/7 so WhatsApp and M-Pesa can reach it. Pick one:

**Easiest: Render.com or Railway.app** (about USD 5–10 / month)
1. Create an account and choose **New → Web Service → Deploy from a Git repository** (upload this folder to a free GitHub account first) — it detects the `Dockerfile` by itself.
2. Add a **persistent disk/volume** mounted at `/data` (this keeps your orders and settings safe when it restarts). Without it, you lose data on every restart.
3. Add one environment variable: `ADMIN_PASSWORD` = a strong password of your choice.
4. Deploy. You get an address like `https://myshop.onrender.com`.

**Shared hosting with cPanel (works, with limits)**
1. In cPanel open **Setup Python App** (you need Python 3.9 or newer; if you don't see it, ask your host to enable it).
2. Create the app: choose the Python version, set *Application root* to a folder such as `shopbot` (outside `public_html`), your domain/subdomain as the *URL*, and **Application startup file = `passenger_wsgi.py`**, *Application entry point = `application`*.
3. Upload this project's files into that folder (File Manager or FTP), then in the Python App page run **Run Pip Install** with `requirements-cpanel.txt`.
4. Add environment variable `ADMIN_PASSWORD`, click **Restart**, and turn on **AutoSSL** for the domain (WhatsApp needs `https://`).
5. Open `https://YOUR-DOMAIN/admin`.
Limits: the app "sleeps" when idle and wakes on the next message (the first reply can be a few seconds slower), and the 60-minute "release unpaid items" cleanup only runs while it is awake. Orders and settings are kept in the `data` folder inside the app folder, so never put the app inside `public_html`. If WhatsApp replies feel slow or unreliable, move to Render/Railway/VPS — nothing else changes.

**No Terminal in your cPanel?** Upload `diagnose.py` to the app folder, then on the Python App page find **"Execute python script"**, type `diagnose.py` and press **Run Script**. It installs what is missing (trying the older-server list if needed), tries to start the app, and prints the exact reason if it can't. Read `startup_error.log` and `stderr.log` with the File Manager. If your cPanel has neither Terminal nor "Execute python script", ask your host to run the pip install for you, or use Render.com (steps below).

**cPanel installer says "Web application is inaccessible by its address"?** That message appears when the installer's final check of your web address fails. The install itself may have worked. With a Terminal:
1. Copy the "Enter to the virtual environment" command shown at the top of the Python App page, paste it in Terminal, then `cd` into your app folder.
2. `pip install -r requirements-cpanel.txt` (shows the real error if there is one). If the errors mention *rust*, *cargo* or *maturin*, use `requirements-cpanel-legacy.txt` instead. If it says *Killed*, the host limited memory: try again or ask your host.
3. `python -c "import passenger_wsgi"` and then look for a file named `startup_error.log` in the app folder; it names the exact problem.
4. Open your address in a browser. Not-found/DNS errors mean the subdomain isn't created or pointed to the server yet; a redirect loop means turn off "Force HTTPS" until AutoSSL is active.
5. Press **Restart** on the Python App page.

**Render.com FREE plan (good for testing)**
1. Create a free account at **github.com** and a new repository (Private is fine). Unzip this project and drag **all of its files and the `app` folder** into the repository page (*Add file → Upload files*), then *Commit*.
2. At **render.com** sign up (use "Sign in with GitHub") → **New → Blueprint** → pick your repository. Render reads `render.yaml`, selects the **Free** plan and asks for the secret values (admin password, tokens, AI key, your numbers). Fields you don't have yet (like the WhatsApp ones) can hold any placeholder text for now.
3. Press **Apply**. The first build takes a few minutes. When it says *Live*, open `https://YOUR-SERVICE.onrender.com/health`, then `/admin`. (Render tells the program its own address, so you don't have to enter the Public URL.)
4. **Free-plan limits:** it sleeps after 15 minutes idle (about a minute to wake) and has no disk, so orders and anything saved on the Settings page disappear on every restart or redeploy. Change settings under **Render → your service → Environment** instead, and use the **Google Sheet link** for products.
5. Before you press *Verify* in Meta, open `/health` once so the service is awake.

If you have a developer or a VPS, `docker build -t shop . && docker run -p 8000:8000 -v shopdata:/data shop` works too (put HTTPS in front of it).

Open `https://YOUR-ADDRESS/admin`. Username: `admin`, password: the one you chose.

## Step 2 — Add your products

Go to **Products** in the admin page.
- **Option A (Excel):** click *Download sample Excel sheet*, replace the samples with your items, upload it back.
- **Option B (Google Sheet):** use the same columns, set sharing to *Anyone with the link can view*, paste the link. Updates appear within about a minute.

Tip: write good **Descriptions** (sizes, colours, materials) — the assistant answers customer questions from them.

## Step 3 — Connect WhatsApp

You need a Facebook account and a phone number **not already used on WhatsApp** (or one you are willing to move to the API).

1. Go to **developers.facebook.com → My Apps → Create App → type "Business"**. Add the **WhatsApp** product.
2. Open **WhatsApp → API Setup**. Copy the **Phone number ID**. Add your real business number there and verify it (the test number works for practice).
3. Create a **permanent token**: Meta Business Settings → Users → **System Users** → add one (Admin) → *Generate token* → choose your app, tick `whatsapp_business_messaging` and `whatsapp_business_management`. Copy it.
4. Meta app → **Settings → Basic** → copy the **App Secret**.
5. In our **Settings** page paste: *Access token, Phone number ID, App Secret*, and set *Public URL* to your address from step 1. Save.
6. Back in Meta: **WhatsApp → Configuration → Webhook → Edit.** Paste the **webhook address** and **verify token** shown at the top of our Settings page. Click verify, then **subscribe to "messages"**.
7. **Message template (do this now, approval takes time).** In **WhatsApp Manager → Message templates → Create**: category **Utility**, name `order_update`, language English, body: `Update from MY SHOP: {{1}}` (use your shop name). Submit. When approved, type `order_update` into *Message template name* in Settings. This lets the assistant send order updates (e.g. "out for delivery") even when the customer last wrote more than 24 hours ago.

**Also:** put **your own** WhatsApp number in Settings as *owner number*. Send *hi* to the shop's number from your phone; the bot replies with order commands. (WhatsApp only lets the bot message you freely for 24 hours after you last messaged it, so the template above also covers your alerts.)

## Step 4 — Choose the AI that talks to customers (Settings → 3. AI assistant)

Pick **one** provider, create an account, make an **API key**, paste it in Settings, save, then press **Test AI connection** (it tells you in plain words if the key or model name is wrong). You can switch provider any time.

| Provider | Where to get a key | Notes |
|---|---|---|
| **Anthropic (Claude)** | console.anthropic.com → API Keys | Add a few dollars of credit first. |
| **OpenAI (ChatGPT)** | platform.openai.com → API keys | Needs billing credit. |
| **Google Gemini** | aistudio.google.com → Get API key | Has a free tier for testing. On free tiers, Google may use your chats to improve its products, so use a paid key for real customers' data. |
| **Z.ai (GLM)** | z.ai API console (docs.z.ai) | Use the global address (built in). |
| **Other** | DeepSeek, Groq, OpenRouter, a local server… anything "OpenAI-compatible" | Also fill in *Base URL* and *Model name*. |

Good to know:
- **Model name** – leave it empty to use the suggested one. Providers rename and retire models often; if the test says "model not found", copy a current model name from the provider's website into *Model name*.
- **Quality varies.** The assistant relies on the AI calling "tools" correctly (search, cart, payment). Bigger models are more reliable; very small or free models may make mistakes. Prices, totals, stock and payment checks are always calculated by the program itself, never by the AI, but test a full purchase in the **Test chat** with the model you choose.
- **Privacy.** Customer messages (and names/addresses they type) are sent to the provider you pick. Choose one whose terms you are comfortable with and mention it in your shop's privacy notice.
- Costs are usually a fraction of a shilling to a few shillings per chat.

## Step 5 — Choose how customers pay (Settings → 4. Payments)

You can pick **either** option at any time; both are built in.

**Option 1 — Customer pays your Till / Paybill / phone number (no Safaricom developer account; works with a Till or Paybill from *any* provider)**
1. *How do customers pay?* → choose one of:
   - **"Customer pays, I confirm each payment"** – zero setup. When the customer sends their M-Pesa code, you get a WhatsApp alert and reply `OK 12` (received) or `NO 12` (not received) — or press the button on the Orders page. Always check your own M-Pesa messages for that code and amount first.
   - **"Confirmed automatically from my M-Pesa SMS"** – the program checks the code and amount itself. For this, the phone that receives your M-Pesa SMS (Android) must forward them: install any "SMS forwarder" app that can send each incoming SMS to a web address (a "webhook"/HTTP POST), set it to forward only messages from **MPESA**, and paste the address shown at the top of Settings ("Address your phone forwards M-Pesa SMS to"). If the SMS doesn't arrive within 2 minutes you get an alert and can confirm by hand, so nothing gets stuck.
2. *Customers pay to my…* → Till, Paybill or Phone number; type the number; for Paybill you may set a fixed account number (otherwise each order uses ORDER12 style); optionally the name shown on M-Pesa.
3. How it works for the customer: after they confirm the order, the bot sends the exact steps (Till/Paybill/amount) → they pay → they reply with the confirmation code (or paste the SMS) → they get the "payment received" message.
Safety built in: a code works only once, the amount must match the total, and only incoming-payment SMS from MPESA count — pasted SMS text is never trusted on its own.

**Option 2 — M-Pesa pop-up on the customer's phone (STK push, needs Safaricom Daraja)**
This only works with a Till/Paybill registered with Safaricom, and needs their approval for real money. Test first:
1. Create an account at **developer.safaricom.co.ke → My Apps → Add a new app** (tick *Lipa Na M-Pesa Sandbox*). Copy the **Consumer Key/Secret**.
2. Settings → *How do customers pay?* = **M-Pesa pop-up**. In "6. Advanced": *M-Pesa mode* = **sandbox**, *Shortcode* = `174379`, paste Key and Secret, leave the passkey empty (the public test passkey is built in). Set *Public URL* (must be `https://` and reachable — a cPanel domain with SSL works).
3. Test chat → buy something → the program sends a real sandbox STK request. In the sandbox the pop-up does not reach real phones: use the **Daraja portal → APIs → Lipa Na M-Pesa → Simulate** page (it shows the test phone) to enter the PIN; then the callback arrives and the order shows as paid.
4. **Going live:** apply under **Go Live** on the portal with your Paybill/Till. Safaricom gives you the **passkey**. Settings: *mode* = production, account type (Till or Paybill), *Shortcode* (for a Till this is the Store/Head Office number), *Till number*, the production Key/Secret, and the passkey.

## Step 6 — Test before real customers (Test chat also has a "fake M-Pesa SMS" box for the automatic mode)

1. Open **Test chat** in the admin page. Chat like a customer: ask about an item, buy it. Payment is simulated; check the owner alert reaches your WhatsApp.
2. Message the shop's WhatsApp number from another phone and buy something with a small amount (Till/Paybill mode works with real money straight away).
3. Go live, then place a real KES 10 order yourself and press **Delivered** to see the whole journey, including the rating request.

---

## Daily use
- New paid orders arrive on your WhatsApp (in "I confirm" mode you first get a "customer says they paid" alert: reply `OK 12` or `NO 12`). Update them either on the **Orders** admin page (big buttons) or by replying to the bot: `ORDERS`, `PACK 12`, `SHIP 12`, `DONE 12`, `CANCEL 12`.
- Each update is sent to the customer automatically. After **Delivered**, the customer is invited (optional) to rate 1–5 stars.
- Change products/prices/stock in your sheet any time.
- **Refunds** are done by you in M-Pesa. The program only tells the customer you'll contact them.
- If a customer is unhappy or asks for something odd, the assistant forwards the chat to you: reply to that customer directly in WhatsApp.

## If something doesn't work
| Problem | Check |
|---|---|
| Bot never replies | Webhook verified and subscribed to *messages*? Public URL correct? Press **Test AI connection** in Settings (key, credit, model name). Is the WhatsApp token still valid? |
| Customer paid but order stays "waiting" | Till/Paybill mode: did the customer send the code? In automatic mode, is the SMS forwarder running and the address correct? You can always press *Payment received* on the Orders page. STK mode: Public URL must be `https://` and reachable. |
| "Could not send the M-Pesa prompt" | Wrong Daraja keys, shortcode or passkey; or still in sandbox with a real phone number. |
| Unpaid orders keep items reserved | Unpaid orders are released after 60 minutes (the customer can say *retry*). |
| Updates not reaching customers after a day | The template is not approved/typed in Settings (see step 3.7). |
| Products missing | Open **Products** — it lists rows that were skipped and why (usually a missing price). |

## Good to know (honest limits)
- One assistant = one shop. Delivery is a flat fee for now.
- Text only: customers send text; voice notes and photos get a polite "text please".
- Keep your admin password and tokens private. The WhatsApp access token and M-Pesa keys are stored in the `/data` folder on your server.
- You are collecting phone numbers and addresses: only use them for orders and respect Kenya's Data Protection Act (register with the ODPC if required for your business).
