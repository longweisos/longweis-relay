import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from longweis_relay_client import __version__
from longweis_relay_client import cli
from longweis_relay_client.client_bridge import (
    LocalPremiumClient,
    LocalRelayBridge,
    LocalRelayClient,
    LocalRelayConfig,
    PUBLIC_CLIENT_PROTOCOL,
)
from longweis_relay_client.client_endpoint import normalize_relay_base_url
from longweis_relay_client.client_errors import ValidationError
from longweis_relay_client.client_memory_import import _NoRelayRedirects
from longweis_relay_client.client_model_selection import PremiumModelSelection
from longweis_relay_client.client_modes import RelayBillingMode
from longweis_relay_client.egress import egress_inventory


class PublicBoundaryTests(unittest.TestCase):
    def test_cli_reports_the_package_version(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as result, redirect_stdout(output):
            cli.main(["--version"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), __version__)

    def test_relay_origin_is_fixed_except_literal_loopback_http(self):
        self.assertEqual(
            normalize_relay_base_url("https://api.longweisos.com"),
            "https://api.longweisos.com/v1",
        )
        self.assertEqual(
            normalize_relay_base_url("http://127.0.0.1:8765/v1"),
            "http://127.0.0.1:8765/v1",
        )
        for value in (
            "http://api.longweisos.com/v1",
            "https://relay.longweisos.com/v1",
            "https://api.longweisos.com.example/v1",
            "https://api.longweisos.com/v2",
            "https://api.longweisos.com/v1?token=secret",
            "https://user:secret@api.longweisos.com/v1",
            "http://localhost:8765/v1",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_relay_base_url(value)

    def test_egress_inventory_is_complete_and_contains_no_secrets(self):
        payload = egress_inventory()
        self.assertEqual(
            {item["purpose"] for item in payload},
            {
                "Relay service",
                "User-selected premium provider",
                "Local OpenAI-compatible API",
            },
        )
        serialized = json.dumps(payload).lower()
        provider_entry = next(
            item
            for item in payload
            if item["purpose"] == "User-selected premium provider"
        )
        self.assertEqual(len(provider_entry["available_provider_labels"]), 3)
        for secret in ("psk_test_", "lsk_", "bearer "):
            self.assertNotIn(secret, serialized)

    def test_memory_upload_refuses_redirects_before_forwarding_content(self):
        handler = _NoRelayRedirects()
        self.assertIsNone(
            handler.redirect_request(
                object(),
                object(),
                307,
                "Temporary Redirect",
                {},
                "https://attacker.example/upload",
            )
        )

    def test_cli_audit_commands_do_not_read_or_print_credentials(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["egress"]), 0)
        payload = output.getvalue().lower()
        self.assertNotIn("psk_test_", payload)
        self.assertNotIn("lsk_", payload)
        self.assertNotIn("bearer ", payload)

        native = {
            "status": "native_preflight_ready",
            "credentials_read": False,
            "credentials_written": False,
            "network_calls": 0,
        }
        output = io.StringIO()
        with patch.object(cli, "native_client_preflight", return_value=native):
            with redirect_stdout(output):
                self.assertEqual(cli.main(["doctor", "--offline"]), 0)
        doctor = json.loads(output.getvalue())
        self.assertTrue(doctor["offline"])
        self.assertFalse(doctor["credentials_read"])
        self.assertFalse(doctor["credentials_printed"])

    def test_online_doctor_requires_explicit_protocol_advertisement(self):
        native = {
            "status": "native_preflight_ready",
            "credentials_read": False,
            "credentials_written": False,
            "network_calls": 0,
        }
        methods = {
            "email": True,
            "google": True,
            "invite_only": False,
            "topups": True,
            "promotion": {
                "signup_credit_usd": "10.00",
                "stage": "public_launch",
            },
        }

        class AuthClient:
            def __init__(self, unused_url):
                del unused_url

            def methods(self):
                return dict(methods)

        def run_doctor():
            output = io.StringIO()
            with patch.object(cli, "native_client_preflight", return_value=native), patch.object(
                cli, "_public_json", return_value={"status": "ok"}
            ), patch.object(cli, "RelayAuthClient", AuthClient), redirect_stdout(output):
                status = cli.main(["doctor"])
            return status, json.loads(output.getvalue())

        status, payload = run_doctor()
        self.assertEqual(status, 1)
        self.assertFalse(payload["public_service"]["public_client_protocol_v1"])

        methods["client_protocols"] = [PUBLIC_CLIENT_PROTOCOL]
        status, payload = run_doctor()
        self.assertEqual(status, 0)
        self.assertTrue(payload["public_service"]["public_client_protocol_v1"])

    def test_bridge_sends_original_messages_without_embedded_policy(self):
        relay_requests = []
        premium_requests = []
        credentials = {
            "relay/main": "relay-secret",
            "premium/main": "premium-secret",
        }

        def read_credential(reference):
            return credentials[reference]

        def relay_transport(url, headers, payload, timeout):
            del url, headers, timeout
            request = json.loads(payload)
            relay_requests.append(request)
            return 200, json.dumps(
                {
                    "object": "longweis_relay.bridge_handoff",
                    "client_protocol": PUBLIC_CLIENT_PROTOCOL,
                    "request_id": request["request_id"],
                    "mode": request["mode"],
                    "premium": {
                        "provider": request["premium_provider"],
                        "model": request["premium_model"],
                    },
                    "action": "call_premium_direct",
                    "use_original_messages": True,
                    "reason": "service_selected_direct",
                    "relay_charge": None,
                }
            ).encode("utf-8")

        def premium_transport(url, headers, payload, timeout):
            del url, headers, timeout
            premium_requests.append(json.loads(payload))
            return 200, json.dumps(
                {
                    "id": "response-one",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                }
            ).encode("utf-8")

        selection = PremiumModelSelection.from_payload(
            {"provider": "openrouter", "model": "openai/test-model"}
        )
        bridge = LocalRelayBridge(
            LocalRelayClient(
                LocalRelayConfig(),
                "relay/main",
                read_credential,
                transport=relay_transport,
            ),
            LocalPremiumClient(
                selection,
                "premium/main",
                read_credential,
                transport=premium_transport,
            ),
        )
        original = [{"role": "user", "content": "Summarize this."}]
        result = bridge.complete(
            request_id="public-boundary-one",
            mode=RelayBillingMode.EXECUTION_OPTIMIZATION,
            messages=original,
        )
        self.assertEqual(result.content, "Answer")
        self.assertEqual(
            relay_requests[0]["client_protocol"], PUBLIC_CLIENT_PROTOCOL
        )
        self.assertEqual(relay_requests[0]["messages"], original)
        self.assertEqual(premium_requests[0]["messages"], original)

    def test_legacy_server_rejection_falls_back_before_relay_execution(self):
        relay_requests = []
        premium_requests = []
        credentials = {
            "relay/main": "relay-secret",
            "premium/main": "premium-secret",
        }

        def read_credential(reference):
            return credentials[reference]

        def legacy_relay(url, headers, payload, timeout):
            del url, headers, timeout
            relay_requests.append(json.loads(payload))
            return 400, b'{"error":{"code":"invalid_request"}}'

        def premium(url, headers, payload, timeout):
            del url, headers, timeout
            premium_requests.append(json.loads(payload))
            return 200, json.dumps(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Fallback"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            ).encode("utf-8")

        selection = PremiumModelSelection.from_payload(
            {"provider": "openrouter", "model": "openai/test-model"}
        )
        bridge = LocalRelayBridge(
            LocalRelayClient(
                LocalRelayConfig(),
                "relay/main",
                read_credential,
                transport=legacy_relay,
            ),
            LocalPremiumClient(
                selection,
                "premium/main",
                read_credential,
                transport=premium,
            ),
        )
        original = [{"role": "user", "content": "Keep working."}]
        result = bridge.complete(
            request_id="legacy-rejection-one",
            mode=RelayBillingMode.EXECUTION_OPTIMIZATION,
            messages=original,
        )
        self.assertEqual(result.content, "Fallback")
        self.assertEqual(result.relay_reason, "relay_unavailable")
        self.assertIsNone(result.relay_charge)
        self.assertEqual(
            relay_requests[0]["client_protocol"], PUBLIC_CLIENT_PROTOCOL
        )
        self.assertEqual(premium_requests[0]["messages"], original)


if __name__ == "__main__":
    unittest.main()
