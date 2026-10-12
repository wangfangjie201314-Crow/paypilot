"""SQLite-backed store: chat messages + orders."""
import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    paypal_order_id TEXT UNIQUE NOT NULL,
    buyer_name TEXT DEFAULT '',
    items_json TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,           -- CREATED | APPROVED | CAPTURED | CANCELLED
    approval_url TEXT NOT NULL,
    created_ts REAL NOT NULL,
    captured_ts REAL
);
"""

# ---- payment status state machine (ARCHITECTURE.md §3) ----
#
# Local order lifecycle. CAPTURED and CANCELLED are TERMINAL: money that
# has arrived (or an order that is dead) can never be moved out again, no
# matter how late or stale the incoming PayPal observation is.
#
# Every PayPal-driven write MUST go through apply_payment_verdict(), which
# enforces this table with real serialization: BEGIN IMMEDIATE up front
# (Python 3.12 sqlite3 defaults to autocommit — `with connection:` alone
# does NOT wrap SELECT->UPDATE in one transaction) plus a conditional
# compare-and-swap UPDATE. A stale request can never downgrade a paid
# order. A multi-worker deployment would need a distributed CAS.
TRANSITIONS = {
    "CREATED":   {"APPROVED", "CAPTURED", "CANCELLED"},
    "APPROVED":  {"CAPTURED", "CANCELLED"},
    "CAPTURED":  set(),   # terminal
    "CANCELLED": set(),   # terminal
}
# Local statuses that count as "money arrived" for revenue/briefs.
PAID_STATUSES = frozenset({"CAPTURED"})


def is_paid(status):
    """The single revenue-recognition predicate. Money counts iff True."""
    return (status or "").upper() in PAID_STATUSES


def legal_transition(old_status, new_status):
    """True iff the state machine allows old -> new (same state = no-op)."""
    old = (old_status or "").upper()
    new = (new_status or "").upper()
    if old == new:
        return True
    return new in TRANSITIONS.get(old, set())


class Store:
    def __init__(self, path="paypilot.db"):
        self.path = path
        self._init()

    @contextmanager
    def _conn(self):
        # sqlite3 connections used as context managers commit/rollback but do
        # NOT close: the open file handle blocks os.remove on Windows
        # (WinError 32). This wrapper always closes the connection.
        c = sqlite3.connect(self.path)
        try:
            c.row_factory = sqlite3.Row
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ---- messages ----
    def save_message(self, session_id, role, content):
        with self._conn() as c:
            c.execute("INSERT INTO messages (session_id, role, content, ts) "
                      "VALUES (?, ?, ?, ?)",
                      (session_id, role, content, time.time()))

    def get_history(self, session_id, limit=20):
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]}
                for r in reversed(rows)]

    # ---- orders ----
    def save_order(self, session_id, paypal_order_id, items, amount_cents,
                   currency, approval_url, status="CREATED"):
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders (session_id, paypal_order_id, items_json, "
                "amount_cents, currency, status, approval_url, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, paypal_order_id, json.dumps(items), amount_cents,
                 currency, status, approval_url, time.time()))

    def update_order_status(self, paypal_order_id, status):
        """Legacy direct setter. Now routed through apply_payment_verdict()
        so the state machine and terminal-state protection always apply —
        no PayPal-driven write may bypass the shared entry
        (ARCHITECTURE.md §3)."""
        self.apply_payment_verdict(paypal_order_id, status,
                                   reason="legacy update_order_status")

    def _cas_status(self, c, paypal_order_id, expected_old_raw,
                    captured_ts, new):
        """Atomic compare-and-swap of the order status.

        The UPDATE only fires when the row still holds the status we
        SELECTed. Returns True when the row was written, False when
        another writer changed it first — the caller must then re-read
        and re-evaluate, never blind-write.
        """
        if new == "CAPTURED":
            # Keep the FIRST capture timestamp; re-verifications must
            # not rewrite history.
            ts = captured_ts if captured_ts else time.time()
            cur = c.execute(
                "UPDATE orders SET status=?, captured_ts=? "
                "WHERE paypal_order_id=? AND status=?",
                (new, ts, paypal_order_id, expected_old_raw))
        else:
            cur = c.execute(
                "UPDATE orders SET status=? "
                "WHERE paypal_order_id=? AND status=?",
                (new, paypal_order_id, expected_old_raw))
        return cur.rowcount > 0

    def apply_payment_verdict(self, paypal_order_id, new_status,
                              reason=""):
        """Single shared entry for every PayPal-driven status change.

        Callers pass the LOCAL status produced by
        payments.verify_payment() — never a raw PayPal status string.
        The transition table is enforced with real serialization
        (ARCHITECTURE.md §3):

        1. ``BEGIN IMMEDIATE`` before the first read takes the RESERVED
           lock, so all writers through this entry are serialized —
           a later writer blocks until the earlier one commits.
        2. The write itself is a conditional compare-and-swap
           (``_cas_status``): ``WHERE status = <just-read value>``.
           If a writer that bypassed this entry changed the row in
           between, ``rowcount == 0`` and we re-read and re-evaluate
           against the transition table instead of clobbering.

        A stale/late request (e.g. APPROVED arriving after CAPTURED)
        is rejected and the record is left untouched; terminal states
        are never downgraded.

        Returns (old_status, new_status_or_old, detail) where detail is
        "noop" | "applied" | "rejected: ...".
        """
        new = (new_status or "").upper()
        with self._conn() as c:
            if not c.in_transaction:
                # Serialize read-modify-write. NOTE: on Python 3.12 the
                # sqlite3 default is autocommit, so `with connection:`
                # alone would NOT do this — in_transaction is False
                # after a bare SELECT (Codex v4 proved the v4 doc wrong).
                c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT status, captured_ts FROM orders "
                          "WHERE paypal_order_id=?",
                          (paypal_order_id,)).fetchone()
            if not r:
                return None, None, "rejected: unknown local order"
            old = (r["status"] or "").upper()
            if old == new:
                return old, old, "noop"
            if not legal_transition(old, new):
                return (old, old,
                        f"rejected: illegal transition {old}->{new} "
                        f"({reason})" if reason else
                        f"rejected: illegal transition {old}->{new}")
            if self._cas_status(c, paypal_order_id, r["status"],
                                r["captured_ts"], new):
                return old, new, "applied"
            # Lost a race with a writer that bypassed BEGIN IMMEDIATE
            # (legacy path / direct SQL). Re-read and re-evaluate —
            # never clobber blindly.
            r2 = c.execute("SELECT status, captured_ts FROM orders "
                           "WHERE paypal_order_id=?",
                           (paypal_order_id,)).fetchone()
            if not r2:
                return None, None, "rejected: unknown local order"
            old2 = (r2["status"] or "").upper()
            if old2 == new:
                return old2, old2, "noop"
            if not legal_transition(old2, new):
                return (old2, old2,
                        f"rejected: illegal transition {old2}->{new} "
                        "(stale request)")
            if self._cas_status(c, paypal_order_id, r2["status"],
                                r2["captured_ts"], new):
                return old2, new, "applied"
            return old2, old2, "rejected: concurrent modification"

    def get_order(self, paypal_order_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM orders WHERE paypal_order_id=?",
                          (paypal_order_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["items"] = json.loads(d["items_json"])
        return d

    def get_last_order_id(self, session_id):
        with self._conn() as c:
            r = c.execute("SELECT paypal_order_id FROM orders WHERE session_id=? "
                          "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return r["paypal_order_id"] if r else None

    def list_orders(self, limit=50):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items_json"])
            out.append(d)
        return out

    def stats(self):
        # Revenue is recognised ONLY through is_paid(): every PayPal-driven
        # write goes through apply_payment_verdict(), so "paid" can never
        # silently flip to a non-counted spelling — and terminal CAPTURED
        # can never be downgraded by a late observation.
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n, "
                "COALESCE(SUM(amount_cents),0) AS s "
                "FROM orders GROUP BY status").fetchall()
        paid_n = paid_c = ab_n = ab_c = 0
        for r in rows:
            if is_paid(r["status"]):
                paid_n += r["n"]
                paid_c += r["s"]
            elif (r["status"] or "").upper() != "CANCELLED":
                ab_n += r["n"]
                ab_c += r["s"]
        return {"captured_count": paid_n, "captured_cents": paid_c,
                "abandoned_count": ab_n, "abandoned_cents": ab_c}
