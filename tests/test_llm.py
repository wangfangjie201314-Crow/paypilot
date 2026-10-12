import unittest
from unittest.mock import patch, Mock
from llm import LLMClient
from paypal_client import PayPalClient


class ProviderCompatibility(unittest.TestCase):
    def request_body(self, url, tools=None):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {
            "content": "ok", "tool_calls": []}}]}
        with patch("llm.requests.post", return_value=response) as post:
            LLMClient(url, "test-not-a-secret", "test-model").chat(
                [{"role": "user", "content": "Hello"}], tools=tools)
            response.raise_for_status.assert_called_once()
            return post.call_args.kwargs["json"]

    def test_deepseek_non_thinking_tools(self):
        tools = [{"type": "function", "function": {"name": "search_products"}}]
        body = self.request_body("https://api.deepseek.com/v1/", tools)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")

    def test_deepseek_simple_call(self):
        body = self.request_body("https://api.deepseek.com")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", body)

    def test_other_providers_unchanged(self):
        for url in ("https://api.openai.com/v1", "http://localhost:11434/v1",
                    "https://api.deepseek.com.example.org/v1"):
            with self.subTest(url=url):
                body = self.request_body(url)
                self.assertNotIn("thinking", body)
                self.assertNotIn("max_tokens", body)

    def test_shipping_threshold_matches_checkout(self):
        for subtotal, expected_shipping in ((4999, 595), (5000, 0), (5001, 0)):
            with self.subTest(subtotal=subtotal):
                response = Mock(status_code=201)
                response.json.return_value = {"id": "test-order", "links": [
                    {"rel": "approve", "href": "https://example.org/test"}]}
                with patch("paypal_client.requests.post", return_value=response) as post, \
                        patch.object(PayPalClient, "get_access_token", return_value="test-token"):
                    _, _, total = PayPalClient("test-id", "test-secret").create_order([
                        {"name": "test", "unit_cents": subtotal, "quantity": 1}])
                    amount = post.call_args.kwargs["json"]["purchase_units"][0]["amount"]
                    self.assertEqual(total, subtotal + expected_shipping)
                    self.assertEqual(amount["breakdown"]["shipping"]["value"],
                                     f"{expected_shipping / 100:.2f}")


if __name__ == "__main__":
    unittest.main()
