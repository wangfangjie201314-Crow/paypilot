"""Shared payment-proof verification (ARCHITECTURE.md §1–§2).

Every path that writes PayPal-driven state into a local order — /return,
order-status polling, the agent's check_order tool, the webhook — MUST go
through verify_payment(). No path is allowed its own definition of "paid".

Trusted payment evidence (all three required):
  1. purchase_units[0].payments.captures[0].status == "COMPLETED"
  2. captured amount (cents) == local order amount_cents
  3. captured currency == local order currency

A PayPal order-level "COMPLETED" alone is NOT evidence: the money proof
lives in the sub-capture.
"""
from paypal_client import PayPalClient


def verify_payment(remote_order_json, expected_cents, expected_currency):
    """Verify a full PayPal order JSON against the local order's expectation.

    Args:
        remote_order_json: full order object from PayPal (get_order or the
            /capture response — both carry purchase_units/payments/captures).
        expected_cents: local order amount in cents (or None to skip).
        expected_currency: local order currency, e.g. "USD".

    Returns:
        (local_status, paid, reason) where local_status is one of
        CREATED | APPROVED | CAPTURED | CANCELLED (the local vocabulary),
        paid is True ONLY when the trusted-evidence triple holds, and
        reason is a human-readable explanation for the negative cases.
    """
    remote = remote_order_json or {}
    order_status = (remote.get("status") or "").upper()
    verdict = PayPalClient.parse_capture_result(
        remote, expected_cents=expected_cents,
        expected_currency=expected_currency or "USD")

    if verdict["status"] == "completed":
        return "CAPTURED", True, ""

    # Not paid. Map the lifecycle to local vocabulary so the UI can say
    # "approved, waiting on PayPal" instead of "unknown".
    if verdict["status"] == "pending":
        # Buyer approved; PayPal is still processing the capture. This is
        # APPROVED locally even when the order-level status already reads
        # COMPLETED — the money proof (completed sub-capture) is missing.
        return ("APPROVED", False,
                verdict["reason"] or "capture pending at PayPal")
    if order_status == "VOIDED":
        return "CANCELLED", False, "order voided at PayPal"
    if order_status == "APPROVED":
        return ("APPROVED", False,
                verdict["reason"] or "buyer approved; capture not completed")
    return "CREATED", False, verdict["reason"] or "payment not completed"
