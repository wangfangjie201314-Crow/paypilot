"""PayPal REST API client (sandbox-first).

Uses raw HTTPS calls (no SDK) against:
  - POST /v1/oauth2/token            (client-credentials access token, cached)
  - POST /v2/checkout/orders        (create order -> approval link)
  - POST /v2/checkout/orders/{id}/capture
  - GET  /v2/checkout/orders/{id}    (status polling)
"""
import base64
import time

import requests

SHIPPING_FEE_CENTS = 595
FREE_SHIPPING_THRESHOLD_CENTS = 5000

class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self, client_id, client_secret,
                 base_url="https://api-m.sandbox.paypal.com", timeout=20):
        if not client_id or not client_secret:
            raise PayPalError(
                "Missing PayPal sandbox credentials. Set PAYPAL_CLIENT_ID and "
                "PAYPAL_CLIENT_SECRET (get them free at https://developer.paypal.com/).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = None
        self._token_exp = 0.0

    # ---- auth ----
    def _basic_auth(self):
        raw = f"{self.client_id}:{self.client_secret}"
        return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def get_access_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            headers={"Authorization": self._basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=self.timeout)
        if resp.status_code != 200:
            raise PayPalError(f"PayPal token request failed "
                              f"({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 32400))
        return self._token

    def _headers(self, idempotency_key=None):
        h = {"Authorization": f"Bearer {self.get_access_token()}",
             "Content-Type": "application/json"}
        if idempotency_key:
            h["PayPal-Request-Id"] = idempotency_key
        return h

    @staticmethod
    def _money(cents, currency):
        return {"currency_code": currency, "value": f"{cents / 100:.2f}"}

    # ---- orders ----
    def create_order(self, items, currency="USD", return_url="", cancel_url="",
                     brand_name="Mira Ceramics", reference_id=None,
                     idempotency_key=None):
        """items: list of {name, sku, unit_cents, quantity}.

        Returns (paypal_order_id, approval_url, total_cents).
        Shipping: free over $50, else $5.95 (demo store policy).
        """
        if not items:
            raise PayPalError("create_order requires at least one item")
        item_total = sum(i["unit_cents"] * i["quantity"] for i in items)
        shipping = 0 if item_total >= FREE_SHIPPING_THRESHOLD_CENTS else SHIPPING_FEE_CENTS
        total = item_total + shipping

        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id or f"paypilot-{int(time.time())}",
                "description": f"Mira Ceramics order ({sum(i['quantity'] for i in items)} items)",
                "amount": {
                    "currency_code": currency,
                    "value": f"{total / 100:.2f}",
                    "breakdown": {
                        "item_total": self._money(item_total, currency),
                        "shipping": self._money(shipping, currency),
                    },
                },
                "items": [{
                    "name": i["name"][:127],
                    "sku": i.get("sku", "")[:127],
                    "unit_amount": self._money(i["unit_cents"], currency),
                    "quantity": str(i["quantity"]),
                } for i in items],
            }],
            "application_context": {
                "brand_name": brand_name[:127],
                "landing_page": "LOGIN",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        resp = requests.post(f"{self.base_url}/v2/checkout/orders",
                             headers=self._headers(idempotency_key),
                             json=body, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"PayPal create order failed "
                              f"({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        approval_url = next((l["href"] for l in data.get("links", [])
                             if l.get("rel") == "approve"), None)
        if not approval_url:
            raise PayPalError("PayPal response missing approval link")
        return data["id"], approval_url, total

    def capture_order(self, order_id):
        resp = requests.post(
            f"{self.base_url}/v2/checkout/orders/{order_id}/capture",
            headers=self._headers(), json={}, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise PayPalError(f"PayPal capture failed "
                              f"({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    @staticmethod
    def parse_capture_result(capture_json, expected_cents=None,
                             expected_currency="USD"):
        """Validate a capture response before anyone declares "paid".

        PayPal's own rule: the payment counts iff
        purchase_units[0].payments.captures[0].status == "COMPLETED".
        An HTTP 200 on /capture is NOT enough (PENDING/failed captures
        also return 200).

        Returns {"status": "completed"|"pending"|"failed",
                 "captured_cents": int|None, "currency": str|None,
                 "reason": str}.
        """
        try:
            cap = capture_json["purchase_units"][0]["payments"]["captures"][0]
        except (KeyError, IndexError, TypeError):
            return {"status": "failed", "captured_cents": None,
                    "currency": None, "reason": "missing capture record"}
        st = (cap.get("status") or "").upper()
        amt = cap.get("amount") or {}
        try:
            cents = int(round(float(amt.get("value", "0")) * 100))
        except (ValueError, TypeError):
            cents = None
        currency = (amt.get("currency_code") or "").upper()
        if st == "COMPLETED":
            if expected_cents is not None and cents != expected_cents:
                return {"status": "failed", "captured_cents": cents,
                        "currency": currency,
                        "reason": f"amount mismatch: got {cents}, "
                                  f"expected {expected_cents}"}
            if currency != (expected_currency or "USD").upper():
                return {"status": "failed", "captured_cents": cents,
                        "currency": currency,
                        "reason": f"currency mismatch: {currency}"}
            return {"status": "completed", "captured_cents": cents,
                    "currency": currency, "reason": ""}
        if st == "PENDING":
            return {"status": "pending", "captured_cents": cents,
                    "currency": currency,
                    "reason": cap.get("reason") or "capture pending"}
        return {"status": "failed", "captured_cents": cents,
                "currency": currency, "reason": f"capture status {st}"}

    def get_order(self, order_id):
        resp = requests.get(f"{self.base_url}/v2/checkout/orders/{order_id}",
                            headers=self._headers(), timeout=self.timeout)
        if resp.status_code != 200:
            raise PayPalError(f"PayPal get order failed "
                              f"({resp.status_code}): {resp.text[:300]}")
        return resp.json()
