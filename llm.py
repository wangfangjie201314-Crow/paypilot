"""LLM abstraction.

- LLMClient: OpenAI-compatible /chat/completions (works with OpenAI,
  DeepSeek, Moonshot/Kimi, OpenRouter, local Ollama, etc.)
- DemoLLM: deterministic rule-based stand-in with the SAME interface, so the
  whole agent loop (tool calls included) runs with zero API keys. Used when
  LLM_API_KEY is empty or DEMO_MODE=true. Clearly a demo fallback.
"""
import re

import requests


def _norm_tool_calls(raw):
    out = []
    for tc in raw or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            import json
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        out.append({"id": tc.get("id", "call_0"),
                    "name": fn.get("name", ""),
                    "arguments": args or {}})
    return out


class LLMClient:
    def __init__(self, base_url, api_key, model, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, messages, tools=None, context=None):
        body = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=body, timeout=self.timeout)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        return {"content": msg.get("content"),
                "tool_calls": _norm_tool_calls(msg.get("tool_calls"))}

    def simple(self, system, user):
        """One-shot generation (used for merchant brief / follow-ups)."""
        r = self.chat([{"role": "system", "content": system},
                       {"role": "user", "content": user}])
        return r["content"] or ""


class DemoLLM:
    """Rule-based demo brain. Drives the same tool loop as a real LLM."""

    def __init__(self, products):
        self.products = products

    # -- helpers --
    def _match_products(self, text):
        found = []
        for p in self.products:
            keys = [p["id"].replace("-", " ")] + p.get("keywords", [])
            if any(k.lower() in text for k in keys):
                found.append(p)
        return found

    def _qty(self, text, default=1):
        m = re.search(r"\b([1-9])\b", text)
        return int(m.group(1)) if m else default

    def _tool(self, name, arguments):
        return {"content": None,
                "tool_calls": [{"id": "demo_0", "name": name,
                                "arguments": arguments}]}

    # -- main --
    def chat(self, messages, tools=None, context=None):
        context = context or {}
        last = messages[-1]

        # After a tool result -> compose the final reply from it.
        if last.get("role") == "tool":
            return self._render_tool_result(last)

        text = (last.get("content") or "").lower()

        # 1) order status question
        if (context.get("last_order_id")
                and any(k in text for k in
                       ["status", "did it go", "went through", "confirm",
                        "paid", "payment"])):
            return self._tool("check_order", {"order_id": "last"})

        # 2) buy intent
        matched = self._match_products(text)
        if matched and any(k in text for k in
                           ["take", "buy", "order", "i'll", "i will", "yes",
                            "get ", "want"]):
            qty = self._qty(text)
            items = [{"product_id": p["id"], "quantity": qty}
                     for p in matched[:2]]
            return self._tool("create_paypal_order", {"items": items})

        # 3) product question -> search
        if any(k in text for k in
               ["mug", "cup", "vase", "plate", "bowl", "coffee", "matcha",
                "blue", "pink", "ceramic", "product", "looking for", "show",
                "have", "sell", "price", "cost"]):
            q = text
            for w in ["do you", "have", "any", "looking for", "show me",
                      "what", "the", "a", "an", "is", "there", "?"]:
                q = q.replace(w, " ")
            q = re.sub(r"\s+", " ", q).strip() or text
            return self._tool("search_products", {"query": q})

        # 4) greeting / fallback
        return {"content": ("Hi there! \U0001F44B Welcome to Mira Ceramics \u2014 "
                            "handmade ceramics from Jingdezhen. Are you looking "
                            "for a mug, a vase, or maybe a coffee set?"),
                "tool_calls": []}

    def _render_tool_result(self, tool_msg):
        import json
        name = tool_msg.get("name", "")
        try:
            data = json.loads(tool_msg.get("content") or "{}")
        except Exception:
            data = {}
        if data.get("error"):
            return {"content": ("Hmm, I ran into an issue: "
                                + str(data["error"])), "tool_calls": []}
        if name == "search_products":
            items = data.get("products", [])
            if not items:
                return {"content": ("I couldn't find that in our catalog \u2014 "
                                    "we make mugs, cups, vases, plates, bowls "
                                    "and coffee sets. What catches your eye?"),
                        "tool_calls": []}
            lines = [f"{i+1}. **{p['name']}** \u2014 ${p['price_usd']:.2f}: "
                     f"{p['short']}" for i, p in enumerate(items[:4])]
            return {"content": ("Here's what I found:\n" + "\n".join(lines)
                                + "\nWant me to reserve one for you?"),
                    "tool_calls": []}
        if name == "create_paypal_order":
            items_txt = ", ".join(
                f"{i['quantity']} \u00d7 {i['name']}" for i in data["items"])
            return {"content": (
                f"Done! I've created a secure PayPal checkout for {items_txt} "
                f"\u2014 total **${data['total_usd']:.2f}** (shipping included).\n\n"
                f"\U0001F449 **Pay securely here:** {data['approval_url']}\n\n"
                "Click the link, approve with your PayPal account, and you'll "
                "be redirected back here for confirmation. No account needed "
                "\u2014 guest checkout works too!"),
                "tool_calls": []}
        if name == "check_order":
            st = data.get("status", "UNKNOWN")
            if st == "CAPTURED":
                return {"content": ("\u2705 Payment confirmed! Thank you so much \u2014 "
                                    "your ceramics will ship within 2 business days. "
                                    "I'll send tracking as soon as it's on its way!"),
                        "tool_calls": []}
            if st in ("CREATED", "APPROVED", "PENDING"):
                return {"content": ("Your order is created and waiting for payment. "
                                    "The PayPal approval link is still open \u2014 "
                                    "just complete checkout whenever you're ready!"),
                        "tool_calls": []}
            return {"content": f"Current order status: {st}.", "tool_calls": []}
        return {"content": "Got it!", "tool_calls": []}

    def simple(self, system, user):
        return ("(Demo mode) AI-generated text would appear here with a real "
                "LLM key configured.")
