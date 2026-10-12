"""Tests that run WITHOUT any API keys: DemoLLM agent loop + mocked PayPal.

Environment isolation (ARCHITECTURE.md §6): this module sets
PAYPILOT_NO_DOTENV=1 BEFORE importing the app, so a developer .env with
dummy PayPal credentials can never change test semantics — in this process
or in any fresh subprocess running these tests. Production behaviour
(load_dotenv on real runs) is untouched.
"""
import json
import os

# Must precede every `import app` (including lazy ones in setUp methods).
os.environ["PAYPILOT_NO_DOTENV"] = "1"

import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent  # noqa: E402
from llm import DemoLLM  # noqa: E402
from paypal_client import PayPalClient  # noqa: E402
from store import Store  # noqa: E402

PRODUCTS = json.load(open(os.path.join(os.path.dirname(__file__), "..",
                                       "products.json"), encoding="utf-8"))


# ---- test environment isolation (P2-4) ----
# Tests must never inherit ambient config: a developer .env (or CI env) with
# dummy PayPal credentials must not change test semantics or trigger real
# network calls. Every Flask-app test scrubs these keys in setUp and
# restores them in tearDown.
_SCRUB_KEYS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_BASE_URL",
               "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
               "DEMO_MODE", "DB_PATH", "BASE_URL", "PORT",
               "MERCHANT_API_TOKEN")


def scrub_env():
    """Remove all PayPilot config from os.environ; return a backup."""
    backup = {k: os.environ.get(k) for k in _SCRUB_KEYS}
    for k in _SCRUB_KEYS:
        os.environ.pop(k, None)
    return backup


def restore_env(backup):
    for k, v in backup.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class FakePayPal:
    """Stands in for PayPalClient: records calls, returns canned values.

    Simulates PayPal's idempotency: the same PayPal-Request-Id returns the
    same order id (like the real API); a fresh key mints a fresh order.
    capture_response can be overridden to inject capture outcomes.
    """

    def __init__(self):
        self.created = []
        self.by_key = {}
        self.capture_response = None

    def create_order(self, items, currency="USD", return_url="", cancel_url="",
                     brand_name="", reference_id=None, idempotency_key=None):
        total = sum(i["unit_cents"] * i["quantity"] for i in items)
        ship = 0 if total >= 5000 else 595
        if idempotency_key and idempotency_key in self.by_key:
            oid = self.by_key[idempotency_key]
        else:
            oid = f"TEST-{len(self.created)+1:03d}"
            if idempotency_key:
                self.by_key[idempotency_key] = oid
        self.created.append((items, total + ship, idempotency_key))
        return oid, f"https://www.sandbox.paypal.com/checkoutnow?token={oid}", total + ship

    def get_order(self, order_id):
        return {"id": order_id, "status": "CREATED"}

    def capture_order(self, order_id):
        if self.capture_response is not None:
            return self.capture_response
        return {"id": order_id, "status": "COMPLETED",
                "purchase_units": [{"payments": {"captures": [
                    {"status": "COMPLETED",
                     "amount": {"currency_code": "USD",
                               "value": "53.95"}}]}}]}


def remote_order(variant):
    """Full PayPal order JSON for each adversarial remote variant.

    Variants (ARCHITECTURE.md §6):
      ok            — order COMPLETED, sub-capture COMPLETED 53.95 USD (paid)
      pending       — order COMPLETED, sub-capture PENDING 53.95 USD
      one_dollar    — order COMPLETED, sub-capture COMPLETED but only $1.00
      eur           — order COMPLETED, sub-capture COMPLETED 53.95 EUR
      no_capture    — order COMPLETED, no capture object at all
      approved_late — stale/late observation: order APPROVED, capture PENDING
      created       — order CREATED, nothing captured
    """
    def cap(status, value, cur):
        return {"status": status,
                "amount": {"currency_code": cur, "value": value}}

    table = {
        "ok":            ("COMPLETED", [cap("COMPLETED", "53.95", "USD")]),
        "pending":       ("COMPLETED", [cap("PENDING", "53.95", "USD")]),
        "one_dollar":    ("COMPLETED", [cap("COMPLETED", "1.00", "USD")]),
        "eur":           ("COMPLETED", [cap("COMPLETED", "53.95", "EUR")]),
        "no_capture":    ("COMPLETED", []),
        "approved_late": ("APPROVED",  [cap("PENDING", "53.95", "USD")]),
        "created":       ("CREATED",   []),
    }
    status, caps = table[variant]
    if caps:
        pu = [{"payments": {"captures": caps}}]
    else:
        pu = [{"payments": {}}]
    return {"id": "REMOTE-X", "status": status, "purchase_units": pu}


class FakePayPalFull:
    """Variant-driven fake: get_order/capture_order return remote_order(variant).

    The variant can be switched mid-test to simulate a remote state change
    (e.g. PENDING -> cleared).
    """

    def __init__(self, variant="ok"):
        self.variant = variant
        self.calls = []

    def get_order(self, order_id):
        self.calls.append(("get", order_id))
        d = remote_order(self.variant)
        d["id"] = order_id
        return d

    def capture_order(self, order_id):
        self.calls.append(("capture", order_id))
        d = remote_order(self.variant)
        d["id"] = order_id
        return d


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.store = Store(self.db)
        self.agent = Agent(self.store, FakePayPal(), DemoLLM(PRODUCTS),
                           PRODUCTS, base_url="http://localhost:5000")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_greeting(self):
        out = self.agent.run_turn("s1", "hello")
        self.assertIn("Mira Ceramics", out["reply"])

    def test_product_search(self):
        out = self.agent.run_turn("s2", "Do you have any blue mugs?")
        self.assertIn("Ocean Blue", out["reply"])

    def test_buy_flow_creates_paypal_order(self):
        out = self.agent.run_turn("s3", "I'll take 2 of the Ocean Blue mugs")
        self.assertIn("sandbox.paypal.com/checkoutnow", out["reply"])
        self.assertIn("53.95", out["reply"])  # 2*24 + 5.95 shipping
        rec = self.store.get_order(out["order_id"])
        self.assertEqual(rec["status"], "CREATED")
        self.assertEqual(rec["amount_cents"], 5395)

    def test_order_status(self):
        out = self.agent.run_turn("s4", "I'll take 1 terracotta vase")
        out2 = self.agent.run_turn("s4", "What is the status of my order?")
        self.assertIn("waiting for payment", out2["reply"])

    def test_unknown_product_not_invented(self):
        out = self.agent.run_turn("s5", "Do you sell golden teapots?")
        self.assertNotIn("golden", out["reply"].lower().replace(
            "couldn't find", ""))


class PayPalClientTest(unittest.TestCase):
    def test_create_order_body(self):
        c = PayPalClient("id", "secret")
        captured = {}

        class R:
            status_code = 201

            def json(self):
                return {"id": "ORDER123",
                        "links": [{"rel": "approve",
                                   "href": "https://approve.test/x"}]}

        with patch("paypal_client.requests.post") as post:
            post.return_value = R()
            with patch.object(PayPalClient, "get_access_token",
                              return_value="tok"):
                oid, url, total = c.create_order(
                    [{"name": "Mug", "sku": "m", "unit_cents": 2400,
                      "quantity": 2}],
                    return_url="http://x/return", cancel_url="http://x/cancel")
                captured.update(post.call_args.kwargs["json"])
        self.assertEqual(oid, "ORDER123")
        self.assertEqual(total, 5395)  # 4800 + 595 shipping
        pu = captured["purchase_units"][0]
        self.assertEqual(pu["amount"]["value"], "53.95")
        self.assertEqual(pu["amount"]["breakdown"]["shipping"]["value"], "5.95")
        self.assertEqual(len(pu["items"]), 1)

    def test_missing_credentials(self):
        from paypal_client import PayPalError
        with self.assertRaises(PayPalError):
            PayPalClient("", "")


class FlaskTest(unittest.TestCase):
    def setUp(self):
        self._env = scrub_env()
        os.environ["DEMO_MODE"] = "true"
        os.environ["MERCHANT_API_TOKEN"] = "test-merchant-token"
        self.db = tempfile.mktemp(suffix=".db")
        os.environ["DB_PATH"] = self.db
        import app as appmod
        self.app = appmod.create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        restore_env(self._env)
        if os.path.exists(self.db):
            os.remove(self.db)

    def _mheaders(self):
        return {"Authorization": "Bearer test-merchant-token"}

    def test_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"PayPilot", r.data)

    def test_chat_greeting(self):
        r = self.client.post("/api/chat",
                             json={"session_id": "t1", "message": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("session_id", r.get_json())

    def test_brief(self):
        r = self.client.post("/api/merchant/brief", headers=self._mheaders())
        self.assertEqual(r.status_code, 200)
        self.assertIn("brief", r.get_json())


class WebhookTest(unittest.TestCase):
    """A forged webhook must never flip order state without PayPal proof."""

    def setUp(self):
        self._env = scrub_env()
        os.environ["DEMO_MODE"] = "true"
        os.environ["MERCHANT_API_TOKEN"] = "test-merchant-token"
        self.db = tempfile.mktemp(suffix=".db")
        os.environ["DB_PATH"] = self.db
        import app as appmod
        self.app = appmod.create_app()
        self.client = self.app.test_client()
        self.store = self.app.config["CTX"]["store"]
        self.store.save_order(
            "s9", "TEST-WH-1",
            [{"name": "Mug", "sku": "m", "unit_cents": 2400, "quantity": 1}],
            2400, "USD", "https://approve.test/x")

    def tearDown(self):
        restore_env(self._env)
        if os.path.exists(self.db):
            os.remove(self.db)

    def _payload(self, order_id="TEST-WH-1"):
        return {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"links": [{
                "rel": "up",
                "href": f"https://api-m.sandbox.paypal.com/v2/checkout/"
                        f"orders/{order_id}",
            }]},
        }

    def test_forged_webhook_no_paypal_client_ignored(self):
        # No PayPal credentials configured -> event must be ignored, not crash.
        self.assertIsNone(self.app.config["CTX"]["paypal"])
        r = self.client.post("/webhook/paypal", json=self._payload())
        self.assertEqual(r.status_code, 200)
        rec = self.store.get_order("TEST-WH-1")
        self.assertEqual(rec["status"], "CREATED")

    def test_webhook_verified_completed_flips_to_captured(self):
        # Full capture evidence: sub-capture COMPLETED, amount/currency
        # match the local order (2400c USD). Only this counts as paid.
        class StubPayPal:
            def get_order(self, order_id):
                return {"id": order_id, "status": "COMPLETED",
                        "purchase_units": [{"payments": {"captures": [
                            {"status": "COMPLETED",
                             "amount": {"currency_code": "USD",
                                       "value": "24.00"}}]}}]}

        self.app.config["CTX"]["paypal"] = StubPayPal()
        r = self.client.post("/webhook/paypal", json=self._payload())
        self.assertEqual(r.status_code, 200)
        rec = self.store.get_order("TEST-WH-1")
        self.assertEqual(rec["status"], "CAPTURED")

    def test_webhook_paypal_says_not_paid_no_flip(self):
        class StubPayPal:
            def get_order(self, order_id):
                return {"id": order_id, "status": "CREATED"}

        self.app.config["CTX"]["paypal"] = StubPayPal()
        r = self.client.post("/webhook/paypal", json=self._payload())
        self.assertEqual(r.status_code, 200)
        rec = self.store.get_order("TEST-WH-1")
        self.assertEqual(rec["status"], "CREATED")


class _AppTestBase(unittest.TestCase):
    """Scrubbed-env Flask app harness shared by the v3 regression tests."""

    def setUp(self):
        self._env = scrub_env()
        os.environ["DEMO_MODE"] = "true"
        os.environ["MERCHANT_API_TOKEN"] = "test-merchant-token"
        self.db = tempfile.mktemp(suffix=".db")
        os.environ["DB_PATH"] = self.db
        import app as appmod
        self.app = appmod.create_app()
        self.client = self.app.test_client()
        self.store = self.app.config["CTX"]["store"]

    def tearDown(self):
        restore_env(self._env)
        if os.path.exists(self.db):
            os.remove(self.db)

    def _mheaders(self):
        return {"Authorization": "Bearer test-merchant-token"}

    def _save_order(self, oid="TEST-X-1", cents=5395, session="sx"):
        self.store.save_order(
            session, oid,
            [{"name": "Ocean Blue Glaze Mug", "sku": "mug-ocean",
              "unit_cents": 2400, "quantity": 2}],
            cents, "USD", "https://approve.test/x")
        return oid


class PaymentStatusTest(_AppTestBase):
    """P1-1 (v4): only full capture evidence counts as revenue, on every path."""

    def test_poll_with_capture_evidence_counts_revenue(self):
        oid = self._save_order()
        self.app.config["CTX"]["paypal"] = FakePayPalFull("ok")
        r = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(r.status_code, 200)
        # API returns the LOCAL vocabulary now, never raw PayPal spelling.
        self.assertEqual(r.get_json()["status"], "CAPTURED")
        self.assertEqual(self.store.get_order(oid)["status"], "CAPTURED")
        st = self.store.stats()
        self.assertEqual(st["captured_cents"], 5395)
        self.assertEqual(st["captured_count"], 1)

    def test_poll_top_level_completed_without_capture_not_paid(self):
        # The v2/v3 lesson, locked in: an order-level COMPLETED with no
        # capture evidence is NOT payment. Revenue stays 0.
        oid = self._save_order()

        class BareStatus:
            def get_order(self, oid):
                return {"id": oid, "status": "COMPLETED"}

        self.app.config["CTX"]["paypal"] = BareStatus()
        r = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.get_order(oid)["status"], "CREATED")
        self.assertEqual(self.store.stats()["captured_cents"], 0)

    def test_agent_recheck_after_webhook_keeps_revenue(self):
        # End-to-end per the report: webhook pays the order, then the agent
        # re-checks via chat tool. Revenue must survive the second write.
        oid = self._save_order("TEST-X-2")
        fake = FakePayPalFull("ok")
        self.app.config["CTX"]["paypal"] = fake
        self.client.post("/webhook/paypal", json={
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"links": [{
                "rel": "up",
                "href": "https://api-m.sandbox.paypal.com/v2/checkout/"
                        f"orders/{oid}"}]}})
        self.assertEqual(self.store.stats()["captured_cents"], 5395)
        agent = self.app.config["CTX"]["agent"]
        agent.paypal = fake
        out = agent.tool_check_order("sx", oid)
        self.assertEqual(out["status"], "CAPTURED")
        self.assertEqual(self.store.get_order(oid)["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_unknown_remote_status_leaves_record_untouched(self):
        oid = self._save_order("TEST-X-3")

        class Stub:
            def get_order(self, oid):
                return {"id": oid, "status": "SOME_FUTURE_STATUS"}

        self.app.config["CTX"]["paypal"] = Stub()
        r = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.get_order(oid)["status"], "CREATED")


class CaptureVerifyTest(_AppTestBase):
    """P1-2: /return must validate the capture response content."""

    def _cap(self, status, value="53.95", currency="USD"):
        return {"id": "X", "status": "COMPLETED",
                "purchase_units": [{"payments": {"captures": [
                    {"status": status,
                     "amount": {"currency_code": currency,
                               "value": value}}]}}]}

    def _with_capture(self, capture_json, oid="TEST-CAP-1"):
        self._save_order(oid)

        class Stub:
            def capture_order(self, oid):
                return capture_json

        self.app.config["CTX"]["paypal"] = Stub()
        return oid

    def test_capture_completed_confirms_and_promises_shipment(self):
        oid = self._with_capture(self._cap("COMPLETED"))
        r = self.client.get(f"/return?token={oid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Payment successful", r.data)
        self.assertEqual(self.store.get_order(oid)["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)
        # the shipping promise lives in the saved chat message
        hist = self.store.get_history("sx")
        self.assertTrue(any("ship within 2 business days" in m["content"]
                            for m in hist if m["role"] == "assistant"))

    def test_capture_pending_no_shipping_promise(self):
        oid = self._with_capture(self._cap("PENDING"))
        r = self.client.get(f"/return?token={oid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Payment pending", r.data)
        self.assertNotIn(b"ship within 2 business days", r.data)
        rec = self.store.get_order(oid)
        self.assertNotEqual(rec["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 0)

    def test_capture_failed_marks_nothing(self):
        oid = self._with_capture({"id": "X", "status": "COMPLETED"})
        r = self.client.get(f"/return?token={oid}")
        self.assertEqual(r.status_code, 502)
        self.assertIn(b"Payment issue", r.data)
        rec = self.store.get_order(oid)
        self.assertNotEqual(rec["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 0)

    def test_capture_amount_mismatch_rejected(self):
        oid = self._with_capture(self._cap("COMPLETED", value="1.00"))
        r = self.client.get(f"/return?token={oid}")
        self.assertEqual(r.status_code, 502)
        self.assertNotEqual(self.store.get_order(oid)["status"], "CAPTURED")


class IdempotencyTest(unittest.TestCase):
    """P2-3: fresh key per intent; retries reuse the key idempotently."""

    def setUp(self):
        self._env = scrub_env()
        self.db = tempfile.mktemp(suffix=".db")
        self.store = Store(self.db)
        self.fake = FakePayPal()
        self.agent = Agent(self.store, self.fake, DemoLLM(PRODUCTS),
                           PRODUCTS, base_url="http://localhost:5000")

    def tearDown(self):
        restore_env(self._env)
        if os.path.exists(self.db):
            os.remove(self.db)

    def _items(self):
        return [{"product_id": "mug-ocean", "quantity": 1}]

    def test_retry_same_key_returns_existing_order(self):
        r1 = self.agent.tool_create_paypal_order(
            "s1", self._items(), idempotency_key="retry-key-1")
        r2 = self.agent.tool_create_paypal_order(
            "s1", self._items(), idempotency_key="retry-key-1")
        self.assertNotIn("error", r1)
        self.assertNotIn("error", r2)
        self.assertEqual(r1["order_id"], r2["order_id"])
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_second_independent_purchase_creates_new_order(self):
        r1 = self.agent.tool_create_paypal_order("s1", self._items())
        r2 = self.agent.tool_create_paypal_order("s1", self._items())
        self.assertNotIn("error", r1)
        self.assertNotIn("error", r2)
        self.assertNotEqual(r1["order_id"], r2["order_id"])
        self.assertEqual(len(self.store.list_orders()), 2)

    def test_keys_differ_per_intent(self):
        r1 = self.agent.tool_create_paypal_order("s1", self._items())
        r2 = self.agent.tool_create_paypal_order("s1", self._items())
        keys = [c[2] for c in self.fake.created]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(k for k in keys))


class MerchantAuthTest(_AppTestBase):
    """P2-5: merchant API requires the bearer token."""

    def test_orders_without_token_401(self):
        r = self.client.get("/api/merchant/orders")
        self.assertEqual(r.status_code, 401)

    def test_orders_with_token_200(self):
        r = self.client.get("/api/merchant/orders",
                            headers=self._mheaders())
        self.assertEqual(r.status_code, 200)

    def test_brief_and_followup_require_token(self):
        self.assertEqual(
            self.client.post("/api/merchant/brief").status_code, 401)
        self.assertEqual(
            self.client.post("/api/merchant/followup",
                             json={"order_id": "x"}).status_code, 401)

    def test_wrong_token_401(self):
        r = self.client.get("/api/merchant/orders",
                            headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_token_auto_generated_when_unset(self):
        backup = scrub_env()
        try:
            os.environ["DEMO_MODE"] = "true"
            db = tempfile.mktemp(suffix=".db")
            os.environ["DB_PATH"] = db
            import app as appmod
            a = appmod.create_app()
            tok = a.config["CTX"]["merchant_token"]
            self.assertTrue(tok and len(tok) >= 16)
            c = a.test_client()
            self.assertEqual(c.get("/api/merchant/orders").status_code, 401)
            self.assertEqual(
                c.get("/api/merchant/orders",
                      headers={"Authorization": f"Bearer {tok}"}).status_code,
                200)
        finally:
            if os.path.exists(db):
                os.remove(db)
            restore_env(backup)


class EnvIsolationTest(unittest.TestCase):
    """P2-4: ambient dummy PayPal env must not change test semantics."""

    def test_scrubbed_env_has_no_paypal_client(self):
        # Simulate the report's repro: a developer .env with dummy creds.
        os.environ["PAYPAL_CLIENT_ID"] = "dummy-id-from-dotenv"
        os.environ["PAYPAL_CLIENT_SECRET"] = "dummy-secret-from-dotenv"
        backup = scrub_env()
        db = tempfile.mktemp(suffix=".db")
        try:
            os.environ["DEMO_MODE"] = "true"
            os.environ["DB_PATH"] = db
            import app as appmod
            app = appmod.create_app()
            # The harness premise ("no PayPal client") holds again.
            self.assertIsNone(app.config["CTX"]["paypal"])
            client = app.test_client()
            app.config["CTX"]["store"].save_order(
                "s9", "TEST-ENV-1",
                [{"name": "Mug", "sku": "m", "unit_cents": 2400,
                  "quantity": 1}],
                2400, "USD", "https://approve.test/x")
            r = client.post("/webhook/paypal", json={
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"links": [{
                    "rel": "up",
                    "href": "https://api-m.sandbox.paypal.com/v2/checkout/"
                            "orders/TEST-ENV-1"}]}})
            self.assertEqual(r.status_code, 200)
            rec = app.config["CTX"]["store"].get_order("TEST-ENV-1")
            self.assertEqual(rec["status"], "CREATED")
        finally:
            if os.path.exists(db):
                os.remove(db)
            restore_env(backup)


class XssTest(unittest.TestCase):
    """P2-6: chat rendering must never turn input into markup/attributes."""

    def test_no_innerhtml_in_app_js(self):
        import re
        p = os.path.join(os.path.dirname(__file__), "..", "static", "app.js")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        # strip comments: the word may appear in prose, assignments may not
        src = re.sub(r"//.*", "", src)
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        self.assertNotIn("innerHTML", src)

    def test_node_dom_xss_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        js = os.path.join(os.path.dirname(__file__), "test_xss.js")
        r = subprocess.run([node, js], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class PaymentCombinationTest(_AppTestBase):
    """ARCHITECTURE.md §6: path x variant matrix. Single-route tests don't
    count — every write path must reject every bad remote variant."""

    BAD_VARIANTS = ["pending", "one_dollar", "eur", "no_capture"]
    PATHS = ["return", "poll", "agent", "webhook"]

    def _drive(self, path, fake, oid):
        self.app.config["CTX"]["paypal"] = fake
        if path == "return":
            return self.client.get(f"/return?token={oid}")
        if path == "poll":
            return self.client.get(f"/api/order/{oid}/status")
        if path == "agent":
            agent = self.app.config["CTX"]["agent"]
            agent.paypal = fake
            return agent.tool_check_order("sx", oid)
        if path == "webhook":
            return self.client.post("/webhook/paypal", json={
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"links": [{
                    "rel": "up",
                    "href": "https://api-m.sandbox.paypal.com/v2/checkout/"
                            f"orders/{oid}"}]}})
        raise AssertionError("unknown path " + path)

    def test_matrix_no_path_marks_bad_variants_paid(self):
        for path in self.PATHS:
            for variant in self.BAD_VARIANTS:
                with self.subTest(path=path, variant=variant):
                    oid = f"T-{path}-{variant}"
                    self._save_order(oid)
                    self._drive(path, FakePayPalFull(variant), oid)
                    rec = self.store.get_order(oid)
                    self.assertNotEqual(
                        rec["status"], "CAPTURED",
                        f"{path} x {variant}: marked paid without evidence")
                    self.assertEqual(
                        self.store.stats()["captured_cents"], 0,
                        f"{path} x {variant}: counted revenue")

    def test_return_then_poll_then_agent_stay_captured(self):
        oid = self._save_order("T-SEQ-1")
        fake = FakePayPalFull("ok")
        self.app.config["CTX"]["paypal"] = fake
        r = self.client.get(f"/return?token={oid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Payment successful", r.data)
        for _ in range(2):
            rp = self.client.get(f"/api/order/{oid}/status")
            self.assertEqual(rp.get_json()["status"], "CAPTURED")
        agent = self.app.config["CTX"]["agent"]
        agent.paypal = fake
        out = agent.tool_check_order("sx", oid)
        self.assertEqual(out["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_webhook_after_paid_stays_captured(self):
        oid = self._save_order("T-SEQ-2")
        fake = FakePayPalFull("ok")
        self.app.config["CTX"]["paypal"] = fake
        self.client.get(f"/return?token={oid}")
        self._drive("webhook", fake, oid)
        self.assertEqual(self.store.get_order(oid)["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_webhook_late_approved_after_paid_rejected(self):
        # The v3 P2: a stale APPROVED observation arriving after the money
        # is captured must be atomically rejected — revenue untouched.
        oid = self._save_order("T-SEQ-3")
        fake = FakePayPalFull("ok")
        self.app.config["CTX"]["paypal"] = fake
        self.client.get(f"/return?token={oid}")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)
        fake.variant = "approved_late"
        self._drive("webhook", fake, oid)
        rec = self.store.get_order(oid)
        self.assertEqual(rec["status"], "CAPTURED")
        st = self.store.stats()
        self.assertEqual(st["captured_cents"], 5395)
        self.assertEqual(st["captured_count"], 1)

    def test_pending_then_cleared_recovers(self):
        # PENDING at /return must not kill the order: when PayPal clears
        # the capture, the next poll promotes it to CAPTURED.
        oid = self._save_order("T-SEQ-4")
        fake = FakePayPalFull("pending")
        self.app.config["CTX"]["paypal"] = fake
        r = self.client.get(f"/return?token={oid}")
        self.assertIn(b"Payment pending", r.data)
        self.assertEqual(self.store.get_order(oid)["status"], "APPROVED")
        self.assertEqual(self.store.stats()["captured_cents"], 0)
        fake.variant = "ok"
        rp = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(rp.get_json()["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_report_repro_return_pending_then_poll_and_chat(self):
        # The exact v3-report repro: /return correctly shows pending; the
        # following poll AND chat query must not flip it to paid while the
        # sub-capture is still pending.
        oid = self._save_order("T-SEQ-5")
        fake = FakePayPalFull("pending")
        self.app.config["CTX"]["paypal"] = fake
        self.client.get(f"/return?token={oid}")
        rp = self.client.get(f"/api/order/{oid}/status")
        self.assertNotEqual(rp.get_json()["status"], "CAPTURED")
        agent = self.app.config["CTX"]["agent"]
        agent.paypal = fake
        out = agent.tool_check_order("sx", oid)
        self.assertNotEqual(out["status"], "CAPTURED")
        self.assertNotIn("ship within 2 business days",
                         " ".join(m["content"]
                                   for m in self.store.get_history("sx")))
        self.assertEqual(self.store.stats()["captured_cents"], 0)


class TerminalStateTest(_AppTestBase):
    """ARCHITECTURE.md §3: CAPTURED is terminal; late observations rejected."""

    def test_late_approved_cannot_downgrade(self):
        oid = self._save_order("T-TERM-1")
        old, new, detail = self.store.apply_payment_verdict(oid, "CAPTURED")
        self.assertEqual(detail, "applied")
        ts1 = self.store.get_order(oid)["captured_ts"]
        self.assertTrue(ts1)
        # Even a direct verdict write with a stale APPROVED is rejected…
        old, new, detail = self.store.apply_payment_verdict(
            oid, "APPROVED", "late observation")
        self.assertTrue(detail.startswith("rejected"), detail)
        rec = self.store.get_order(oid)
        self.assertEqual(rec["status"], "CAPTURED")
        self.assertEqual(rec["captured_ts"], ts1)
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_poll_skips_terminal_states(self):
        oid = self._save_order("T-TERM-2")
        self.store.apply_payment_verdict(oid, "CAPTURED")
        fake = FakePayPalFull("approved_late")
        self.app.config["CTX"]["paypal"] = fake
        rp = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(rp.get_json()["status"], "CAPTURED")
        self.assertEqual(self.store.stats()["captured_cents"], 5395)

    def test_illegal_transitions_table(self):
        from store import legal_transition
        self.assertTrue(legal_transition("CREATED", "APPROVED"))
        self.assertTrue(legal_transition("CREATED", "CAPTURED"))
        self.assertTrue(legal_transition("CREATED", "CANCELLED"))
        self.assertTrue(legal_transition("APPROVED", "CAPTURED"))
        self.assertTrue(legal_transition("APPROVED", "CANCELLED"))
        self.assertTrue(legal_transition("CAPTURED", "CAPTURED"))  # no-op
        self.assertFalse(legal_transition("CAPTURED", "APPROVED"))
        self.assertFalse(legal_transition("CAPTURED", "CREATED"))
        self.assertFalse(legal_transition("APPROVED", "CREATED"))
        self.assertFalse(legal_transition("CANCELLED", "CAPTURED"))


class RetryHttpTest(_AppTestBase):
    """ARCHITECTURE.md §5: retry identity end-to-end over HTTP."""

    def _inject_fake(self):
        fake = FakePayPal()
        self.app.config["CTX"]["paypal"] = fake
        self.app.config["CTX"]["agent"].paypal = fake
        return fake

    def test_same_key_twice_via_http_yields_one_order(self):
        self._inject_fake()
        body = {"session_id": "rk1",
                "message": "I'll take 1 terracotta vase",
                "idempotency_key": "http-retry-key-1"}
        r1 = self.client.post("/api/chat", json=body)
        r2 = self.client.post("/api/chat", json=body)
        j1, j2 = r1.get_json(), r2.get_json()
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(j1["order_id"])
        self.assertEqual(j1["order_id"], j2["order_id"])
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_two_intents_without_key_yield_two_orders(self):
        self._inject_fake()
        body = {"session_id": "rk2",
                "message": "I'll take 1 terracotta vase"}
        r1 = self.client.post("/api/chat", json=body)
        r2 = self.client.post("/api/chat", json=body)
        self.assertNotEqual(r1.get_json()["order_id"],
                            r2.get_json()["order_id"])
        self.assertEqual(len(self.store.list_orders()), 2)


class SessionBoundaryTest(_AppTestBase):
    """ARCHITECTURE.md §4.2: order id is the capability; a supplied
    session_id must match the owning session."""

    def test_wrong_session_403(self):
        oid = self._save_order("T-SB-1", session="owner-s")
        r = self.client.get(f"/api/order/{oid}/status?session_id=intruder")
        self.assertEqual(r.status_code, 403)

    def test_own_session_200(self):
        oid = self._save_order("T-SB-2", session="owner-s")
        r = self.client.get(f"/api/order/{oid}/status?session_id=owner-s")
        self.assertEqual(r.status_code, 200)

    def test_bare_id_is_capability_200(self):
        oid = self._save_order("T-SB-3", session="owner-s")
        r = self.client.get(f"/api/order/{oid}/status")
        self.assertEqual(r.status_code, 200)


class MerchantPageTest(_AppTestBase):
    """ARCHITECTURE.md §4.1: public pages never carry the merchant token."""

    def test_index_has_no_token(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"MERCHANT_TOKEN", r.data)
        self.assertNotIn(
            self.app.config["CTX"]["merchant_token"].encode(), r.data)

    def test_merchant_page_has_no_token_value(self):
        r = self.client.get("/merchant")
        self.assertEqual(r.status_code, 200)
        # The page may name the ENV VAR (operator instructions) but must
        # never contain the secret value or an assignment of it.
        self.assertNotIn(
            self.app.config["CTX"]["merchant_token"].encode(), r.data)
        self.assertNotIn(b"window.MERCHANT_TOKEN", r.data)

    def test_buyer_js_has_no_token(self):
        r = self.client.get("/static/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"MERCHANT_TOKEN", r.data)


class DotenvSubprocessTest(unittest.TestCase):
    """ARCHITECTURE.md §6: the v3 report's exact repro — a .env with dummy
    PayPal creds sitting in the cwd of a FRESH test process must not change
    the no-credentials webhook test. Import order cannot matter, because
    dotenv loading is disabled before any app import."""

    def test_fresh_process_ignores_dummy_dotenv(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, ".env"), "w") as f:
                f.write("PAYPAL_CLIENT_ID=dummy-id-from-dotenv\n"
                        "PAYPAL_CLIENT_SECRET=dummy-secret-from-dotenv\n")
            env = dict(os.environ)
            here = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = here + os.pathsep + env.get("PYTHONPATH", "")
            for k in list(_SCRUB_KEYS) + ["PAYPILOT_NO_DOTENV"]:
                env.pop(k, None)
            proc = subprocess.run(
                [sys.executable, "-m", "unittest",
                 "tests.test_all.WebhookTest."
                 "test_forged_webhook_no_paypal_client_ignored"],
                cwd=tmp, env=env, capture_output=True, text=True,
                timeout=180)
            self.assertEqual(proc.returncode, 0,
                             proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---- v5: single-process thread race (Codex v4 P1-1) ----
import threading  # noqa: E402
import time  # noqa: E402


class ConcurrentVerdictTest(unittest.TestCase):
    """ARCHITECTURE.md §3 (v5): a stale verdict must never downgrade a
    terminal state, even when two threads interleave inside one process.

    Mirrors Codex's v4 repro: thread A SELECTs a stale status and pauses
    between the read and the write; thread B drives the order to CAPTURED.
    """

    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.store = Store(self.db)
        self.oid = "TEST-CONC-1"
        self.store.save_order(
            "sx", self.oid,
            [{"name": "Ocean Blue Glaze Mug", "sku": "mug-ocean",
              "unit_cents": 2400, "quantity": 2}],
            5395, "USD", "https://approve.test/x")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_stale_verdict_cannot_downgrade_captured(self):
        import store as store_mod
        orig = store_mod.legal_transition
        entered = threading.Event()
        proceed = threading.Event()
        calls = []

        def hooked(old, new):
            calls.append((old, new))
            if len(calls) == 1:
                # First decision point == thread A's stale read.
                entered.set()
                self.assertTrue(proceed.wait(timeout=10),
                                "stale thread was never released")
            return orig(old, new)

        store_mod.legal_transition = hooked
        try:
            t_a = threading.Thread(
                target=self.store.apply_payment_verdict,
                args=(self.oid, "APPROVED"),
                kwargs={"reason": "stale poll"})
            t_a.start()
            self.assertTrue(entered.wait(timeout=10),
                            "thread A never reached the decision point")
            t_b = threading.Thread(
                target=self.store.apply_payment_verdict,
                args=(self.oid, "CAPTURED"))
            t_b.start()
            time.sleep(0.5)  # let B queue on BEGIN IMMEDIATE
            proceed.set()
            t_a.join(timeout=15)
            t_b.join(timeout=15)
            self.assertFalse(t_a.is_alive(), "thread A hung")
            self.assertFalse(t_b.is_alive(), "thread B hung")
        finally:
            store_mod.legal_transition = orig

        rec = self.store.get_order(self.oid)
        self.assertEqual(rec["status"], "CAPTURED")
        self.assertTrue(rec["captured_ts"])
        self.assertEqual(self.store.stats()["captured_cents"], 5395)


class CasAtomicityTest(unittest.TestCase):
    """White-box: _cas_status is a real compare-and-swap."""

    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.store = Store(self.db)
        self.oid = "TEST-CAS-1"
        self.store.save_order(
            "sx", self.oid,
            [{"name": "Ocean Blue Glaze Mug", "sku": "mug-ocean",
              "unit_cents": 2400, "quantity": 2}],
            5395, "USD", "https://approve.test/x")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_cas_rejects_when_row_changed(self):
        old, new, detail = self.store.apply_payment_verdict(
            self.oid, "CAPTURED")
        self.assertEqual(detail, "applied")
        with self.store._conn() as c:
            ok = self.store._cas_status(c, self.oid, "CREATED", None,
                                        "APPROVED")
            self.assertFalse(ok)
        rec = self.store.get_order(self.oid)
        self.assertEqual(rec["status"], "CAPTURED")

    def test_cas_applies_when_row_matches(self):
        with self.store._conn() as c:
            ok = self.store._cas_status(c, self.oid, "CREATED", None,
                                        "APPROVED")
            self.assertTrue(ok)
        self.assertEqual(self.store.get_order(self.oid)["status"],
                         "APPROVED")


# ---- v5: server-side retry key authority (Codex v4 P1-2) ----
class HallucinatingLLM:
    """Mimics Codex's v4 repro: every turn the model invents a DIFFERENT
    idempotency_key in its tool arguments (as the old schema allowed)."""

    def __init__(self, tool_keys):
        self.tool_keys = list(tool_keys)
        self.turns = 0
        self.calls_in_turn = 0

    def chat(self, messages, tools=None, context=None):
        if self.calls_in_turn == 0:
            self.calls_in_turn += 1
            k = self.tool_keys[self.turns % len(self.tool_keys)]
            self.turns += 1
            return {"content": None, "tool_calls": [
                {"id": f"hallu-{self.turns}", "name": "create_paypal_order",
                 "arguments": {
                     "items": [{"product_id": "vase-terra", "quantity": 1}],
                     "idempotency_key": k}}]}
        self.calls_in_turn = 0
        return {"content": "Your order is ready!", "tool_calls": []}


class RetryKeyAuthorityTest(_AppTestBase):
    """ARCHITECTURE.md §5 (v5): the server-side operation key is the SOLE
    authority. Same HTTP key + different hallucinated model keys must still
    yield exactly one order with one stable PayPal-Request-Id."""

    def _inject_fake(self):
        fake = FakePayPal()
        self.app.config["CTX"]["paypal"] = fake
        self.app.config["CTX"]["agent"].paypal = fake
        return fake

    def test_same_http_key_different_model_keys_yield_one_order(self):
        fake = self._inject_fake()
        self.app.config["CTX"]["agent"].llm = HallucinatingLLM(
            ["llm-turn-1", "llm-turn-2"])
        body = {"session_id": "rk-auth",
                "message": "I'll take 1 terracotta vase",
                "idempotency_key": "http-op-key-9"}
        r1 = self.client.post("/api/chat", json=body)
        r2 = self.client.post("/api/chat", json=body)
        j1, j2 = r1.get_json(), r2.get_json()
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(j1["order_id"])
        self.assertEqual(j1["order_id"], j2["order_id"])
        self.assertEqual(len(self.store.list_orders()), 1)
        sent_keys = [k for (_, _, k) in fake.created]
        self.assertEqual(len(sent_keys), 2)
        self.assertEqual(sent_keys[0], sent_keys[1],
                         "PayPal-Request-Id must be stable across retries")
        self.assertNotIn("llm-turn", sent_keys[0],
                         "model-hallucinated key leaked to PayPal")

    def test_tool_schema_has_no_idempotency_key(self):
        from agent import TOOLS
        schema = next(t for t in TOOLS
                      if t["function"]["name"] == "create_paypal_order")
        props = schema["function"]["parameters"]["properties"]
        self.assertNotIn("idempotency_key", props)

    def test_server_call_key_stable_and_item_sensitive(self):
        from agent import Agent
        items = [{"product_id": "vase-terra", "quantity": 1}]
        k1 = Agent._server_call_key("op-1", items)
        k2 = Agent._server_call_key(
            "op-1", [{"quantity": 1, "product_id": "vase-terra"}])
        self.assertEqual(k1, k2)  # dict/key order must not matter
        k3 = Agent._server_call_key(
            "op-1", [{"product_id": "mug-ocean", "quantity": 1}])
        self.assertNotEqual(k1, k3)  # different items -> distinct key
        self.assertIsNone(Agent._server_call_key(None, items))
