import json
import unittest

from longweis_relay_client.client_account import CLIENT_PRODUCT_VERSION, RelayAccountClient
from longweis_relay_client.client_errors import ProviderResponseError, ValidationError


class RecordingTransport:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return self.status, json.dumps(self.payload).encode("utf-8")


class RelayAccountClientTests(unittest.TestCase):
    def test_client_has_a_public_product_version(self):
        self.assertEqual(CLIENT_PRODUCT_VERSION, "P0.70")

    def client(self, transport):
        return RelayAccountClient(
            "https://api.longweisos.com/v1",
            "default/relay",
            lambda reference: "relay-secret" if reference == "default/relay" else "",
            transport=transport,
        )

    def test_balance_uses_relay_credential_and_parses_exact_money(self):
        transport = RecordingTransport(
            200,
            {
                "object": "longweis_relay.balance",
                "available_usd": "49.123000000",
                "settled_usd": "0.877000000",
                "recovery_due_usd": "0.000000000",
                "can_run_relay_work": True,
            },
        )
        balance = self.client(transport).balance()
        self.assertEqual(balance["available_usd"], "49.123000000")
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.longweisos.com/v1/relay/balance")
        self.assertEqual(request.get_header("Authorization"), "Bearer relay-secret")

    def test_pricing_is_read_from_the_server_and_is_not_client_calculated(self):
        transport = RecordingTransport(
            200,
            {
                "object": "longweis_relay.pricing",
                "currency": "USD",
                "unit": "per_million_relay_tokens",
                "modes": {
                    "execution_optimization": {
                        "input_usd": "0.20",
                        "output_usd": "0.80",
                    },
                    "memory_retrieval": {
                        "input_usd": "0.30",
                        "output_usd": "1.20",
                    },
                },
            },
        )
        pricing = self.client(transport).pricing()
        self.assertEqual(pricing["execution_optimization"]["input_usd"], "0.20")
        self.assertEqual(pricing["memory_retrieval"]["output_usd"], "1.20")
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.longweisos.com/v1/relay/pricing")
        self.assertEqual(request.get_header("Authorization"), "Bearer relay-secret")

    def test_compute_usage_is_server_measured_and_never_subscription_billing(self):
        transport = RecordingTransport(
            200,
            {
                "object": "longweis_relay.account_dashboard",
                "usage": {
                    "summary": {
                        "settled_request_count": 3,
                        "hybrid_token_usage": {
                            "total_tokens": 15000,
                            "premium_model_tokens": 12345,
                            "relay_tokens": 2655,
                            "relay_token_share_percent": "17.70",
                            "measured_turn_count": 3,
                            "premium_usage_unavailable_turn_count": 0,
                        },
                    }
                },
            },
        )
        usage = self.client(transport).compute_usage()
        self.assertEqual(usage["total_tokens"], 15000)
        self.assertEqual(usage["premium_model_tokens"], 12345)
        self.assertEqual(usage["relay_tokens"], 2655)
        self.assertEqual(usage["relay_token_share_percent"], "17.70")
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.longweisos.com/v1/relay/account")
        self.assertEqual(request.get_header("Authorization"), "Bearer relay-secret")

    def test_compute_usage_requires_actual_hybrid_token_receipts(self):
        transport = RecordingTransport(
            200,
            {
                "object": "longweis_relay.account_dashboard",
                "usage": {
                    "summary": {
                        "settled_request_count": 1,
                    }
                },
            },
        )
        with self.assertRaises(ProviderResponseError):
            self.client(transport).compute_usage()

    def test_topup_returns_only_a_square_owned_checkout_link(self):
        transport = RecordingTransport(
            200,
            {
                "object": "longweis_relay.square_topup",
                "amount_usd": "25.00",
                "currency": "USD",
                "status": "pending",
                "checkout_url": "https://sandbox.square.link/u/relay-test",
            },
        )
        result = self.client(transport).create_topup("25.00")
        self.assertEqual(
            result["checkout_url"], "https://sandbox.square.link/u/relay-test"
        )
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.longweisos.com/v1/relay/topups")
        payload = json.loads(request.data)
        self.assertEqual(payload["amount_usd"], "25.00")
        self.assertTrue(payload["request_id"].startswith("client-"))
        self.assertNotIn("relay-secret", request.data.decode("utf-8"))

    def test_arbitrary_checkout_origin_and_amount_are_rejected(self):
        for url in (
            "http://square.link/u/test",
            "https://attacker.example/checkout",
            "https://square.link@attacker.example/checkout",
            "https://square.link.attacker.example/checkout",
        ):
            with self.subTest(url=url):
                transport = RecordingTransport(
                    200,
                    {
                        "object": "longweis_relay.square_topup",
                        "amount_usd": "10.00",
                        "currency": "USD",
                        "status": "pending",
                        "checkout_url": url,
                    },
                )
                with self.assertRaises(ProviderResponseError):
                    self.client(transport).create_topup("10.00")
        with self.assertRaises(ValidationError):
            self.client(RecordingTransport(200, {})).create_topup("11.00")

    def test_feedback_uses_account_credential_and_returns_receipt(self):
        transport = RecordingTransport(
            201,
            {
                "object": "longweis_relay.feedback_receipt",
                "feedback": {"feedback_id": "fb_123", "outcome": "success"},
            },
        )
        result = self.client(transport).submit_feedback(
            {
                "source": "macos",
                "optimization_mode": "compute_optimization",
                "outcome": "success",
                "quality_rating": 5,
                "deviation": "none",
                "speed": "faster",
                "savings_result": "met",
            }
        )
        self.assertEqual(result["feedback_id"], "fb_123")
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://api.longweisos.com/v1/relay/feedback")
        self.assertEqual(request.get_header("Authorization"), "Bearer relay-secret")

    def test_repr_and_errors_do_not_expose_relay_key(self):
        client = self.client(RecordingTransport(503, {"private": "detail"}))
        self.assertNotIn("relay-secret", repr(client))
        with self.assertRaises(ProviderResponseError) as raised:
            client.balance()
        self.assertNotIn("detail", str(raised.exception))
        self.assertNotIn("relay-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
