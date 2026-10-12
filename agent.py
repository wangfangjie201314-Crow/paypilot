"""PayPilot agent: tool-calling sales-agent loop.

The agent talks to buyers, searches the catalog, and creates REAL PayPal
checkout orders via the Orders API v2. Works with any OpenAI-compatible LLM
or the built-in DemoLLM (same tool interface).
"""
import hashlib
import json
import sqlite3
import uuid

from payments import verify_payment

SYSTEM_PROMPT = """You are PayPilot, the AI sales agent for Mira Ceramics, a small \
handmade ceramics studio in Jingdezhen, China selling to customers worldwide.
You chat with buyers in English: friendly, concise, warm \u2014 like a great shop \
assistant, never pushy.

You have tools to search the product catalog and to create real PayPal checkout \
orders. Rules:
1. When asked about products, ALWAYS call search_products first \u2014 never invent \
products, prices, or stock.
2. When the buyer clearly wants to buy ("I'll take", "buy", "order", "yes" to an \
offer), confirm items, quantities and total, then call create_paypal_order.
3. After creating an order, share the PayPal approval link and explain: click to pay \
securely with PayPal, then you'll be redirected back here for confirmation.
4. If asked about an order's status, call check_order.
5. Keep replies under 80 words unless details are needed. Prices in USD.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_products",
        "description": "Search the Mira Ceramics product catalog by keyword.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_product",
        "description": "Get full details of one product by its id.",
        "parameters": {"type": "object",
                       "properties": {"product_id": {"type": "string"}},
                       "required": ["product_id"]}}},
    {"type": "function", "function": {
        "name": "create_paypal_order",
        "description": ("Create a REAL PayPal checkout order (Orders API v2) for "
                        "the given items. Returns order id, approval URL and total. "
                        "Retry handling is server-side: the caller passes "
                        "idempotency_key to POST /api/chat when retrying the same "
                        "operation; the model must NOT invent or pass any key."),
        "parameters": {"type": "object",
                       "properties": {
                           "items": {"type": "array", "items": {
                               "type": "object",
                               "properties": {
                                   "product_id": {"type": "string"},
                                   "quantity": {"type": "integer", "minimum": 1}},
                               "required": ["product_id", "quantity"]}}},
                       "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "check_order",
        "description": "Check the payment status of a PayPal order.",
        "parameters": {"type": "object",
                       "properties": {"order_id": {"type": "string",
                                                  "description": 'PayPal order id, or "last"'}},
                       "required": ["order_id"]}}},
]


class Agent:
    def __init__(self, store, paypal, llm, products, base_url):
        self.store = store
        self.paypal = paypal          # PayPalClient or None
        self.llm = llm
        self.products = {p["id"]: p for p in products}
        self.base_url = base_url.rstrip("/")

    # ---- tool implementations ----
    def tool_search_products(self, query):
        q = query.lower()
        scored = []
        for p in self.products.values():
            hay = f"{p['id']} {p['name']} {p['short']} {p['desc']} " \
                  f"{' '.join(p.get('keywords', []))}".lower()
            score = sum(1 for w in q.split() if w and w in hay)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        return {"products": [
            {"id": p["id"], "name": p["name"],
             "price_usd": p["price_usd"], "short": p["short"],
             "stock": p["stock"]}
            for _, p in scored[:4]]}

    def tool_get_product(self, product_id):
        p = self.products.get(product_id)
        if not p:
            return {"error": f"Unknown product id: {product_id}"}
        return {"product": p}

    def tool_create_paypal_order(self, session_id, items, idempotency_key=None):
        """Create a PayPal order for a purchase intent.

        The idempotency key is SERVER-SUPPLIED (ARCHITECTURE.md §5): it is
        derived in _execute from the HTTP caller's operation key and the
        items, never from the model. Every new intent without a caller key
        gets a FRESH uuid, so two independent purchases of the same items
        in one session never collide on PayPal's idempotency window and
        never hit the local UNIQUE(paypal_order_id) constraint. A retry of
        the SAME operation reuses the derived key: PayPal then returns the
        existing order id, and the local save conflict is absorbed by
        returning the already-stored order instead of crashing.
        """
        if not self.paypal:
            return {"error": ("PayPal is not configured on this server "
                              "(missing sandbox credentials).")}
        line_items, desc = [], []
        for it in items:
            p = self.products.get(it.get("product_id", ""))
            if not p:
                return {"error": f"Unknown product: {it.get('product_id')}"}
            qty = max(1, int(it.get("quantity", 1)))
            line_items.append({"name": p["name"], "sku": p["id"],
                               "unit_cents": int(round(p["price_usd"] * 100)),
                               "quantity": qty})
            desc.append({"product_id": p["id"], "name": p["name"],
                         "quantity": qty,
                         "unit_usd": p["price_usd"]})
        key = idempotency_key or f"{session_id}-{uuid.uuid4().hex[:12]}"
        try:
            order_id, approval_url, total_cents = self.paypal.create_order(
                line_items, currency="USD",
                return_url=f"{self.base_url}/return",
                cancel_url=f"{self.base_url}/cancel",
                brand_name="Mira Ceramics",
                idempotency_key=key)
        except Exception as e:  # noqa: BLE001 - surface PayPal errors to chat
            return {"error": str(e)}
        try:
            self.store.save_order(session_id, order_id, desc, total_cents,
                                  "USD", approval_url)
        except sqlite3.IntegrityError:
            # Idempotent retry: PayPal returned the order id of an already
            # saved operation (same idempotency key). Return the stored
            # order instead of failing.
            rec = self.store.get_order(order_id)
            if rec is None:
                raise
            desc, total_cents = rec["items"], rec["amount_cents"]
            approval_url = rec["approval_url"]
        return {"order_id": order_id, "approval_url": approval_url,
                "total_usd": total_cents / 100, "items": desc,
                "status": "CREATED"}

    def tool_check_order(self, session_id, order_id):
        if order_id == "last":
            order_id = self.store.get_last_order_id(session_id)
            if not order_id:
                return {"error": "No order found in this conversation yet."}
        rec = self.store.get_order(order_id)
        if self.paypal and rec:
            try:
                # Full order JSON (carries purchase_units/payments/captures)
                # goes through the ONE shared payment-proof verification —
                # never a bare order-level status string (ARCHITECTURE §2).
                remote = self.paypal.get_order(order_id)
                local, paid, reason = verify_payment(
                    remote, rec["amount_cents"], rec["currency"])
                old, status, detail = self.store.apply_payment_verdict(
                    order_id, local, reason)
                if detail.startswith("rejected"):
                    status = old  # terminal-state protection held
            except Exception:
                status = rec["status"]
        else:
            status = rec["status"] if rec else "UNKNOWN"
        return {"order_id": order_id, "status": status}

    # ---- conversation turn ----
    def run_turn(self, session_id, user_message, max_steps=6,
                 idempotency_key=None):
        """Run one conversation turn.

        idempotency_key (optional): SERVER-SUPPLIED retry identity for this
        user operation (from POST /api/chat). It is fixed for the whole
        turn and is the SOLE authority for PayPal idempotency
        (ARCHITECTURE.md §5): the per-call key is derived from it plus the
        items, and the model can neither see nor override it. Pass the SAME
        key again when retrying the same operation (e.g. after a network
        timeout); omit it for a brand-new purchase.
        """
        self.store.save_message(session_id, "user", user_message)
        history = self.store.get_history(session_id, limit=20)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        context = {"last_order_id": self.store.get_last_order_id(session_id)}

        for _ in range(max_steps):
            resp = self.llm.chat(messages, tools=TOOLS, context=context)
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                reply = resp.get("content") or "Sorry, I missed that \u2014 could you say it again?"
                self.store.save_message(session_id, "assistant", reply)
                return {"reply": reply, "order_id": context["last_order_id"]}

            messages.append({"role": "assistant",
                             "content": resp.get("content"),
                             "tool_calls": [
                                 {"id": tc["id"], "type": "function",
                                  "function": {"name": tc["name"],
                                               "arguments": json.dumps(tc["arguments"])}}
                                 for tc in tool_calls]})
            for tc in tool_calls:
                result = self._execute(session_id, tc["name"], tc["arguments"],
                                       idempotency_key=idempotency_key)
                if tc["name"] == "create_paypal_order" and "order_id" in result:
                    context["last_order_id"] = result["order_id"]
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc["name"],
                                 "content": json.dumps(result)})

        reply = "Let me get back to you on that in a moment!"
        self.store.save_message(session_id, "assistant", reply)
        return {"reply": reply, "order_id": context["last_order_id"]}

    @staticmethod
    def _server_call_key(server_key, items):
        """Derive the per-call idempotency key from server inputs ONLY.

        Same server key + same items  -> same key (a true retry reuses the
        PayPal-Request-Id, so PayPal returns the existing order instead of
        minting a duplicate).
        Same server key + different items -> distinct key (a second,
        genuine purchase inside one turn — the stable sub-operation
        mapping required by ARCHITECTURE.md §5).
        The model cannot influence this mapping: idempotency_key was
        removed from the tool schema, and any hallucinated key in the
        model's arguments is discarded by _execute.

        Returns None when the caller passed no key; then
        tool_create_paypal_order generates a fresh per-intent uuid.
        """
        if not server_key:
            return None
        norm = sorted(
            (str(i.get("product_id", "")), int(i.get("quantity", 1)))
            for i in (items or []) if isinstance(i, dict))
        sig = json.dumps(norm, separators=(",", ":"))
        digest = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12]
        return f"{server_key}#{digest}"

    def _execute(self, session_id, name, args, idempotency_key=None):
        try:
            if name == "search_products":
                return self.tool_search_products(args.get("query", ""))
            if name == "get_product":
                return self.tool_get_product(args.get("product_id", ""))
            if name == "create_paypal_order":
                # Retry authority (ARCHITECTURE.md §5): the server-side
                # operation key is the SOLE authority, fixed for the whole
                # turn. The model never sees idempotency_key (removed from
                # the tool schema); any key it hallucinates into args is
                # ignored here.
                items = args.get("items", []) if isinstance(args, dict) else []
                return self.tool_create_paypal_order(
                    session_id, items,
                    idempotency_key=self._server_call_key(idempotency_key,
                                                         items))
            if name == "check_order":
                return self.tool_check_order(session_id, args.get("order_id", "last"))
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

