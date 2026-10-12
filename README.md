# PayPilot — Your AI Sales Agent (PayPal AI Hackathon entry)

**Never miss a sale again.** PayPilot is an AI sales agent for cross-border
micro-merchants: it chats with buyers in natural language, answers product
questions from the catalog, and — when the buyer says "I'll take it" — creates
a **real PayPal checkout order** (Orders API v2) right inside the conversation.
Merchants get a live dashboard with an **AI daily brief** and **AI-drafted
cart-recovery messages** for abandoned checkouts.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/):
meaningful use of **PayPal** (sandbox) + **AI** (agentic tool-calling loop).

## 60-second demo

Buyer (chat tab):

> 🧑 "Hi! Do you have any blue mugs?"
> 🤖 "Here's what I found: 1. **Ocean Blue Glaze Mug** — $24.00 …"
> 🧑 "I'll take 2 of the Ocean Blue mugs"
> 🤖 "Done! I've created a secure PayPal checkout … 👉 **Pay securely here:** (sandbox approval link)"
> 🧑 *(approves on PayPal, redirected back — order captured)*
> 🤖 "✅ Payment confirmed! … your ceramics will ship within 2 business days."

Merchant (dashboard): open **`/merchant`**, paste the `MERCHANT_API_TOKEN`
from the server console → the order appears live → **Generate AI Daily Brief**
→ revenue summary, top products, abandoned checkouts, one actionable suggestion
→ **Draft follow-up** writes a WhatsApp-ready recovery nudge for the abandoned cart.

## Quickstart (5 minutes)

**1. Get free PayPal sandbox credentials** — https://developer.paypal.com/
→ log in → *Apps & Credentials* → *Create App* → copy **Client ID** and **Secret**.

**2. Configure**

```bash
cp .env.example .env
# edit .env: paste PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET
```

**3. Run**

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```

No LLM key? Chat still works: with `LLM_API_KEY` empty (or `DEMO_MODE=true`)
the built-in deterministic demo brain drives the *same* agent tool-loop, so
judges can evaluate the chat flow with no LLM key. Note: completing a **real
payment** still requires free PayPal sandbox credentials — see step 1 above.
Set `LLM_API_KEY` (+ optional `LLM_BASE_URL`/`LLM_MODEL`, any
OpenAI-compatible API) for live LLM mode.

For the verified DeepSeek integration, set `LLM_BASE_URL=https://api.deepseek.com`
and `LLM_MODEL=deepseek-flash`, with your own `LLM_API_KEY` in `.env`.
The client selects non-thinking mode for this official host, matching the
agent's ordinary tool-message history, and limits each model output to 2048
tokens. API usage is billable; do not expose your key or run an unattended
public demo without your own usage controls.

## Merchant dashboard auth

The merchant area lives at **`GET /merchant`** — a separate page from the
public buyer chat (`GET /`). The buyer page never receives any merchant
credential.

`/api/merchant/*` requires a bearer token:
`Authorization: Bearer <MERCHANT_API_TOKEN>`. Set `MERCHANT_API_TOKEN` in
your `.env` to fix it; otherwise a random token is generated on every start
and printed to the console. On the `/merchant` page, paste the token into
the input box — the page keeps it in a JavaScript memory variable only
(never in HTML, localStorage or cookies); closing the tab forgets it.
This is demo-grade protection (per-run token, operator-pasted) — not a
substitute for real authentication in production.

### Access model (who can see what)

- **Buyer order status** (`GET /api/order/<id>/status`): the order id is an
  unguessable capability (PayPal order ids are high-entropy). Anyone holding
  the id can check the status — by design, like a signed link, so buyers can
  check out on any device without logging in. When the client identifies its
  session (`?session_id=…`, which the chat UI always sends), ownership is
  enforced: a mismatched session gets 403. The merchant bearer-token check is
  **not** what protects this endpoint — the two mechanisms are independent.
- **Merchant data** (`/api/merchant/*`): bearer token only, never embedded
  in any public page.
- **Payment truth**: only a `COMPLETED` sub-capture whose amount/currency
  match the local order counts as paid. See [ARCHITECTURE.md](ARCHITECTURE.md)
  for the full payment-evidence and state-machine design.

**4. Try the payment** — in chat, buy something, click the sandbox approval
link, log in with a [PayPal sandbox buyer account](https://developer.paypal.com/docs/api-basics/sandbox/accounts/)
(create one free in the developer dashboard), approve → you're redirected back,
the order is captured, and the chat confirms automatically. Sandbox = no real money.

## Architecture

```
buyer chat (web UI)
      │  POST /api/chat
      ▼
Agent (agent.py) — tool-calling loop, max 6 steps
      │  tools: search_products · get_product · create_paypal_order · check_order
      ▼
┌──────────┐   ┌──────────────┐   ┌────────────┐
│ LLM      │   │ PayPalClient │   │ Store      │
│ - live:  │   │ Orders API v2│   │ SQLite:    │
│   OpenAI-│   │ token cache, │   │ messages,  │
│   compat │   │ create/cap-  │   │ orders     │
│ - demo:  │   │ ture/get     │   │            │
│   DemoLLM│   │ (sandbox)    │   │            │
└──────────┘   └──────────────┘   └────────────┘
      │
      ▼
merchant dashboard: orders table · AI daily brief · cart-recovery drafts
webhook: POST /webhook/paypal handles PAYMENT.CAPTURE.COMPLETED
```

Payment flow: `create order (CREATED)` → buyer approves at PayPal →
`GET /return?token=…` captures. What counts as "paid" is defined once in
[ARCHITECTURE.md](ARCHITECTURE.md) and enforced identically on all four
write paths (`/return`, order polling, agent `check_order`, webhook):
only a `COMPLETED` **sub-capture** whose amount **and** currency match the
local order. `PENDING` shows "payment pending" (never a success, never a
shipping promise); amount/currency mismatch or a missing capture is an
error. The local state machine (`CREATED → APPROVED → CAPTURED`, terminal
`CAPTURED`/`CANCELLED`) atomically rejects late/stale observations, so a
paid order's revenue can never be downgraded by a delayed `APPROVED`.
Chat polls `/api/order/<id>/status` and confirms. Webhook supported as a
second path (event body never trusted; order re-fetched from PayPal first).

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chat` | `{session_id, message, idempotency_key?}` → `{reply, order_id}`. Pass the same `idempotency_key` when retrying the same purchase (e.g. after a timeout); omit for a new purchase |
| `GET` | `/api/order/<id>/status` | order status + approval URL; optional `?session_id=` enforces ownership |
| `GET` | `/merchant` | merchant dashboard page (paste `MERCHANT_API_TOKEN`; kept in tab memory only) |
| `GET` | `/api/merchant/orders` | all orders, newest first (needs `Authorization: Bearer $MERCHANT_API_TOKEN`) |
| `POST` | `/api/merchant/brief` | AI daily brief (needs bearer token) |
| `POST` | `/api/merchant/followup` | `{order_id}` → drafted recovery message (needs bearer token) |
| `POST` | `/webhook/paypal` | PayPal webhook receiver (re-fetches the full order from PayPal and runs the shared payment-proof verification before touching local state) |
| `GET` | `/return`, `/cancel` | PayPal redirect targets (`/return` runs the shared verification: only a `COMPLETED` sub-capture with matching amount/currency counts as paid) |

Retry semantics: the idempotency key travels
`HTTP /api/chat` → `Agent.run_turn` → `tool_create_paypal_order` →
PayPal `PayPal-Request-Id` header. A repeated key returns the existing
order instead of creating a duplicate; a missing key mints a fresh one
per purchase intent. The HTTP operation key is authoritative. The server derives a per-call key from normalized items; model-supplied keys are ignored and are not part of the tool schema.

Tests: `python -m unittest discover -s tests -p 'test_*.py'` (60 tests, no keys needed;
includes the payment path×variant combination matrix, terminal-state
protection, HTTP-level retry, session-boundary, a fresh-subprocess dotenv
isolation test, and a Node DOM suite for the chat XSS fix — skipped if
node is absent).

## Tools used (hackathon "Tools" disclosure)

- **PayPal Developer Platform (sandbox)**: Orders API v2 (`/v2/checkout/orders`
  create/capture/get), OAuth2 client-credentials and order verification. The implementation targets the real sandbox APIs; the latest independent acceptance tests used offline recorded mocks.
- **AI**: agent built with Muse AI assistance; runtime LLM is
  pluggable — any OpenAI-compatible model via `LLM_*` env vars
  (DeepSeek Flash catalog lookup and shipping-price replies were exercised live); built-in `DemoLLM` fallback
  for keyless evaluation.
- **Demo video**: AI-generated intro/outro segments (Muse video pipeline),
  AI voiceover, screen recording of the live app.

## License

MIT — see [LICENSE](LICENSE).

## Verification status (v5, 2026-10-12)

56 Python tests and 5 Node DOM tests pass. Independent offline verification covered 84 caller checks, including 15 threaded SQLite schedules and stable PayPal request IDs when model tool arguments change. This confirms the tested offline code paths; live LLM execution, real sandbox approval/capture, and production deployment are not yet claimed as verified.

## Publication increment: live model integration (2026-10-12)

The original v5 remains the offline acceptance baseline. This increment adds
DeepSeek non-thinking compatibility and shares the existing shipping constants
between checkout construction and the model prompt. All 60 Python tests pass,
including the $49.99/$50.00/$50.01 shipping boundary; independent read-only
review found no actionable regression.

Live DeepSeek tool calls were exercised through the app and returned provider
HTTP 200 with token usage. An independent catalog query matched the public
product data. A separate shipping query returned $48.00 + $5.95 = $53.95 for
two Ocean Blue mugs, without creating an order. A buyer-authorized purchase
then created a real PayPal **sandbox** order for that amount. Buyer approval and
capture remain pending verification. This is not a production payment or a
measured merchant-sales result. Mira Ceramics and its static catalog are demo data.
