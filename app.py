"""PayPilot — AI sales agent for cross-border micro-merchants.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill in PayPal sandbox credentials
    python app.py
"""
import json
import os
import secrets
import uuid
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from agent import Agent
from llm import DemoLLM, LLMClient
from payments import verify_payment
from paypal_client import PayPalClient, PayPalError
from store import Store

# Load .env (if present) before anything reads os.environ, so the
# README quickstart (cp .env.example .env) actually works.
# Test processes set PAYPILOT_NO_DOTENV=1 before importing this module so a
# developer .env with dummy credentials can never change test semantics
# (ARCHITECTURE.md §6); production behaviour is unchanged.
if os.environ.get("PAYPILOT_NO_DOTENV") != "1":
    load_dotenv()

def _load_products():
    p = os.path.join(os.path.dirname(__file__), "products.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


PRODUCTS = _load_products()


def build_llm():
    if os.environ.get("DEMO_MODE", "").lower() == "true" \
            or not os.environ.get("LLM_API_KEY"):
        return DemoLLM(PRODUCTS), "demo"
    return LLMClient(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini")), "live"


def build_paypal():
    cid = os.environ.get("PAYPAL_CLIENT_ID", "")
    sec = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not cid or not sec:
        return None
    return PayPalClient(cid, sec,
                        os.environ.get("PAYPAL_BASE_URL",
                                       "https://api-m.sandbox.paypal.com"))


def create_app():
    app = Flask(__name__)
    store = Store(os.environ.get("DB_PATH", "paypilot.db"))
    llm, llm_mode = build_llm()
    paypal = build_paypal()
    agent = Agent(store, paypal, llm, PRODUCTS,
                  base_url=os.environ.get("BASE_URL", "http://localhost:5000"))
    # Merchant API token: set MERCHANT_API_TOKEN to fix it, otherwise a
    # random one is generated per run and printed (demo-grade protection:
    # keeps casual API scraping out; not a substitute for real auth).
    merchant_token = os.environ.get("MERCHANT_API_TOKEN") or \
        secrets.token_urlsafe(24)
    if not os.environ.get("MERCHANT_API_TOKEN"):
        print("[PayPilot] generated MERCHANT_API_TOKEN="
              f"{merchant_token} (set MERCHANT_API_TOKEN env to fix it)")
    app.config["CTX"] = {"store": store, "llm": llm, "llm_mode": llm_mode,
                         "paypal": paypal, "agent": agent,
                         "merchant_token": merchant_token}

    def ctx():
        return app.config["CTX"]

    def require_merchant(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {ctx()['merchant_token']}":
                return jsonify({"error": "merchant unauthorized"}), 401
            return fn(*a, **kw)
        return wrapper

    # ---------- pages ----------
    # The buyer page is PUBLIC. It must never receive the merchant token
    # (ARCHITECTURE.md §4.1): merchant access lives on /merchant, where the
    # operator pastes the token from the server console into an input box.
    @app.get("/")
    def index():
        c = ctx()
        return render_template("index.html",
                               paypal_ok=c["paypal"] is not None,
                               llm_mode=c["llm_mode"])

    @app.get("/merchant")
    def merchant_page():
        # No secrets are embedded: the operator pastes MERCHANT_API_TOKEN
        # (printed at startup) into the page; the JS keeps it in a memory
        # variable only — never in HTML, localStorage or cookies.
        return render_template("merchant.html")

    @app.get("/return")
    def paypal_return():
        """Buyer redirected here after approving on PayPal -> capture."""
        c = ctx()
        token = request.args.get("token", "")
        if not token or not c["paypal"]:
            return render_template("message.html", title="Payment",
                                   body="Missing order token."), 400
        rec0 = c["store"].get_order(token)
        try:
            data = c["paypal"].capture_order(token)
        except PayPalError as e:
            return render_template("message.html", title="Payment issue",
                                   body=str(e)), 502
        # The ONE shared payment-proof verification (ARCHITECTURE.md §2):
        # "paid" requires sub-capture COMPLETED + amount/currency match.
        # An HTTP 200 on /capture is NOT enough.
        local, paid, reason = verify_payment(
            data,
            rec0["amount_cents"] if rec0 else None,
            rec0["currency"] if rec0 else "USD")
        # Terminal-state protection: the write is transition-guarded and
        # atomic; a stale observation can never downgrade a paid order.
        c["store"].apply_payment_verdict(token, local, reason)
        items = ", ".join(f"{i['quantity']}x {i['name']}"
                          for i in (rec0["items"] if rec0 else []))
        if paid:
            rec = c["store"].get_order(token)
            if rec:
                c["store"].save_message(
                    rec["session_id"], "assistant",
                    "\u2705 Payment confirmed! Thank you \u2014 your order "
                    f"({items}) will ship within 2 business days.")
            return render_template("message.html", title="Payment successful",
                                   ok=True,
                                   body=f"Order {token[:8]}\u2026 captured. "
                                        f"{items}. You can close this tab \u2014 "
                                        "your chat has the confirmation.")
        if local == "APPROVED":
            # Buyer approved; money not yet captured. Do NOT promise
            # payment received or shipment.
            return render_template(
                "message.html", title="Payment pending",
                body=f"Order {token[:8]}\u2026: PayPal is still processing "
                     f"the payment ({reason}). Nothing has been "
                     "charged yet \u2014 we'll confirm in chat as soon as "
                     "PayPal clears it. You can close this tab.")
        return render_template(
            "message.html", title="Payment issue",
            body=f"Order {token[:8]}\u2026 could not be captured "
                 f"({reason}). No money moved \u2014 please try "
                 "again from the chat."), 502

    @app.get("/cancel")
    def paypal_cancel():
        return render_template("message.html", title="Payment cancelled",
                               body="No worries \u2014 your order is saved. "
                                    "Just say the word in chat to try again.")

    # ---------- buyer chat API ----------
    @app.post("/api/chat")
    def api_chat():
        c = ctx()
        data = request.get_json(force=True)
        session_id = data.get("session_id") or uuid.uuid4().hex[:12]
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"error": "empty message"}), 400
        # Retry identity (ARCHITECTURE.md §5): the caller may pass the SAME
        # idempotency_key when retrying the same purchase operation
        # (e.g. after a network timeout); omit it for a new purchase.
        out = c["agent"].run_turn(session_id, message,
                                  idempotency_key=data.get("idempotency_key"))
        return jsonify({"session_id": session_id, "reply": out["reply"],
                        "order_id": out.get("order_id")})

    @app.get("/api/order/<order_id>/status")
    def api_order_status(order_id):
        c = ctx()
        rec = c["store"].get_order(order_id)
        if not rec:
            return jsonify({"error": "unknown order"}), 404
        # Access model (ARCHITECTURE.md §4.2): the order id is an unguessable
        # capability. When the client identifies its session, ownership is
        # enforced; a bare id without session_id is capability possession.
        qsid = request.args.get("session_id", "")
        if qsid and qsid != rec["session_id"]:
            return jsonify({"error": "not your order"}), 403
        status = rec["status"]
        if c["paypal"] and status in ("CREATED", "APPROVED"):
            try:
                # Full order JSON through the ONE shared verification —
                # never the bare order-level status (ARCHITECTURE.md §2).
                # Terminal states are never re-polled: nothing can downgrade
                # them, so there is nothing to learn.
                remote = c["paypal"].get_order(order_id)
                local, paid, reason = verify_payment(
                    remote, rec["amount_cents"], rec["currency"])
                old, status, detail = c["store"].apply_payment_verdict(
                    order_id, local, reason)
                if detail.startswith("rejected"):
                    status = old
            except PayPalError:
                pass
        return jsonify({"order_id": order_id, "status": status,
                        "approval_url": rec["approval_url"]})

    # ---------- merchant dashboard API ----------
    @app.get("/api/merchant/orders")
    @require_merchant
    def api_merchant_orders():
        return jsonify(ctx()["store"].list_orders())

    @app.post("/api/merchant/brief")
    @require_merchant
    def api_merchant_brief():
        c = ctx()
        st = c["store"].stats()
        orders = c["store"].list_orders(limit=20)
        summary = {
            "captured_orders": st["captured_count"],
            "revenue_usd": round(st["captured_cents"] / 100, 2),
            "abandoned_checkouts": st["abandoned_count"],
            "abandoned_usd": round(st["abandoned_cents"] / 100, 2),
            "recent": [{"id": o["paypal_order_id"][:8], "status": o["status"],
                        "usd": o["amount_cents"] / 100,
                        "items": [i["name"] for i in o["items"]]}
                       for o in orders],
        }
        if c["llm_mode"] == "live":
            text = c["llm"].simple(
                "You are PayPilot, a business analyst for a small ceramics "
                "studio. Write a concise daily brief (max 120 words) with: "
                "sales summary, top products, abandoned checkouts, and ONE "
                "actionable suggestion. Friendly tone, a few emoji.",
                "Today's store data (JSON): " + json.dumps(summary))
        else:
            lines = [
                "\U0001F4CA PayPilot Daily Brief",
                f"\u2022 Revenue: ${summary['revenue_usd']:.2f} across "
                f"{summary['captured_orders']} paid orders",
                f"\u2022 Abandoned checkouts: {summary['abandoned_checkouts']} "
                f"(${summary['abandoned_usd']:.2f} recoverable)",
            ]
            top = {}
            for o in orders:
                if o["status"] == "CAPTURED":
                    for i in o["items"]:
                        top[i["name"]] = top.get(i["name"], 0) + i["quantity"]
            if top:
                best = max(top, key=top.get)
                lines.append(f"\u2022 Top seller: {best} ({top[best]} units)")
            else:
                lines.append("\u2022 Top seller: Ocean Blue Glaze Mug "
                             "(demo catalog favorite)")
            lines.append("\u2728 Suggestion: send the drafted follow-up to "
                         "abandoned checkouts \u2014 even a 20% recovery rate "
                         "adds meaningful revenue for a small studio.")
            text = "\n".join(lines)
        return jsonify({"brief": text, "mode": c["llm_mode"]})

    @app.post("/api/merchant/followup")
    @require_merchant
    def api_merchant_followup():
        c = ctx()
        order_id = request.get_json(force=True).get("order_id", "")
        rec = c["store"].get_order(order_id)
        if not rec:
            return jsonify({"error": "unknown order"}), 404
        items = ", ".join(f"{i['quantity']}x {i['name']}"
                          for i in rec["items"])
        if c["llm_mode"] == "live":
            text = c["llm"].simple(
                "You are PayPilot writing a cart-recovery WhatsApp message for "
                "a ceramics studio. Warm, short (under 60 words), one gentle "
                "nudge, include the checkout link. No discounts unless asked.",
                f"Buyer left checkout unpaid. Items: {items}. "
                f"Total: ${rec['amount_cents']/100:.2f}. "
                f"Checkout link: {rec['approval_url']}")
        else:
            text = (f"Hi! \U0001F44B This is Mira from Mira Ceramics \u2014 I noticed "
                    f"you were checking out the {items} (${rec['amount_cents']/100:.2f}). "
                    "Happy to answer any questions, or hold one aside for you! "
                    f"Your secure checkout link is still open: {rec['approval_url']}")
        return jsonify({"draft": text, "mode": c["llm_mode"]})

    # ---------- PayPal webhook (optional but real) ----------
    # Security: a webhook body alone is NOT trusted. We extract the order id
    # from resource.links (rel "up" -> .../v2/checkout/orders/{id}) and ask
    # PayPal for the FULL order, then run the ONE shared payment-proof
    # verification (ARCHITECTURE.md §2) — never the bare order-level status.
    # A forged event can therefore never flip an order to CAPTURED, and a
    # late/stale observation can never downgrade a terminal state.
    @app.post("/webhook/paypal")
    def webhook_paypal():
        c = ctx()
        event = request.get_json(force=True, silent=True) or {}
        if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
            res = (event.get("resource") or {})
            oid = None
            for link in res.get("links", []):
                if link.get("rel") != "up":
                    continue
                href = link.get("href", "")
                if "/v2/checkout/orders/" in href:
                    oid = href.rstrip("/").split("/")[-1]
                    break
            paypal = c["paypal"]
            if oid and paypal is not None:
                try:
                    remote = paypal.get_order(oid)
                except PayPalError:
                    app.logger.warning(
                        "webhook: PayPal verification failed for order %s; "
                        "state unchanged", oid)
                else:
                    rec = c["store"].get_order(oid)
                    if rec is None:
                        app.logger.warning(
                            "webhook: event for unknown local order %s; "
                            "ignored", oid)
                    else:
                        local, paid, reason = verify_payment(
                            remote, rec["amount_cents"], rec["currency"])
                        old, new, detail = c["store"].apply_payment_verdict(
                            oid, local, reason)
                        if detail.startswith("rejected"):
                            app.logger.warning(
                                "webhook: transition %s->%s rejected for "
                                "order %s (%s)", old, local, oid, reason)
            elif oid:
                app.logger.info(
                    "webhook: no PayPal client configured; ignoring event "
                    "for order %s", oid)
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=False)
