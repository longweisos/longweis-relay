import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from longweis_relay_client.client_bridge import (
    ClientOpenAIChatResponse,
    ClientTokenUsage,
    LocalBridgeCompletion,
)
from longweis_relay_client.client_local_api import LOCAL_API_MODEL, LocalOpenAIApplication
from longweis_relay_client.client_model_selection import PremiumModelSelection
from longweis_relay_client.client_modes import RelayBillingMode
from longweis_relay_client.client_profile import LocalBridgeProfile, LocalBridgeProfileStore


class FakeCredentials:
    def __init__(self):
        self.values = {
            "default/relay": "relay-service-secret",
            "default/premium": "premium-secret",
            "default/local-api": "local-fused-secret",
        }

    def read(self, reference):
        return self.values[reference]

    def contains(self, reference):
        return reference in self.values


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.tool_calls = []
        self.recorded_usage = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LocalBridgeCompletion(
            request_id=kwargs["request_id"],
            mode=kwargs["mode"],
            provider="openrouter",
            model="openai/gpt-test",
            content="Local API answer.",
            premium_usage=ClientTokenUsage("openai/gpt-test", 21, 4),
            relay_charge={"amount_usd": "0.000001000"},
            relay_action="return_relay_final",
            relay_reason="relay_draft_ready",
        )

    def complete_openai_chat(self, request, *, request_id=None):
        self.tool_calls.append(dict(request, request_id=request_id))
        return ClientOpenAIChatResponse(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_status_1",
                        "type": "function",
                        "function": {"name": "read_status", "arguments": "{}"},
                    }
                ],
            },
            usage=ClientTokenUsage("openai/gpt-test", 31, 7),
            response_id="tool-response",
            finish_reason="tool_calls",
            premium_usage=ClientTokenUsage("openai/gpt-test", 101, 10),
        )

    def record_usage(self, request_id, usage, action):
        self.recorded_usage.append((request_id, usage, action))


class ClientLocalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.profile_store = LocalBridgeProfileStore(
            os.path.join(self.temp_dir.name, "profile.json")
        )
        self.profile_store.save(
            LocalBridgeProfile.create(
                profile_id="default",
                relay_base_url="https://api.longweisos.com/v1",
                selection=PremiumModelSelection.from_payload(
                    {"provider": "openrouter", "model": "openai/gpt-test"}
                ),
                default_mode=RelayBillingMode.EXECUTION_OPTIMIZATION,
                now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        )
        self.credentials = FakeCredentials()
        self.bridge = FakeBridge()
        self.application = LocalOpenAIApplication(
            self.profile_store,
            self.credentials,
            bridge_factory=lambda profile, reader: self.bridge,
        )

    @staticmethod
    def auth(secret="local-fused-secret"):
        return {"Authorization": "Bearer " + secret}

    @staticmethod
    def chat(stream=False):
        return json.dumps(
            {
                "model": LOCAL_API_MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": stream,
            }
        ).encode("utf-8")

    def test_health_is_public_but_models_and_chat_require_local_key(self):
        self.assertEqual(self.application.handle("GET", "/healthz", {}).status, 200)
        self.assertEqual(self.application.handle("GET", "/v1/models", {}).status, 401)
        self.assertEqual(
            self.application.handle("POST", "/v1/chat/completions", {}, self.chat()).status,
            401,
        )

    def test_models_and_buffered_chat_use_the_public_relay_alias(self):
        models = self.application.handle("GET", "/v1/models", self.auth())
        self.assertEqual(models.status, 200)
        self.assertEqual(json.loads(models.body)["data"][0]["id"], LOCAL_API_MODEL)
        response = self.application.handle(
            "POST", "/v1/chat/completions", self.auth(), self.chat()
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.body)["choices"][0]["message"]["content"],
            "Local API answer.",
        )
        self.assertEqual(len(self.bridge.calls), 1)

    def test_responses_endpoint_is_not_part_of_the_ordinary_client(self):
        response = self.application.handle(
            "POST", "/v1/responses", self.auth(), b"{}"
        )
        self.assertEqual(response.status, 404)

    def test_stream_and_openclaw_tool_shape_remain_supported(self):
        streamed = self.application.handle(
            "POST", "/v1/chat/completions", self.auth(), self.chat(stream=True)
        )
        self.assertEqual(streamed.status, 200)
        self.assertIn(b"data: [DONE]", streamed.body)
        body = json.dumps(
            {
                "model": LOCAL_API_MODEL,
                "messages": [{"role": "user", "content": "check"}],
                "stream": False,
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_status", "parameters": {}},
                    }
                ],
            }
        ).encode("utf-8")
        response = self.application.handle(
            "POST", "/v1/chat/completions", self.auth(), body
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.body)["choices"][0]["finish_reason"], "tool_calls"
        )
        self.assertEqual(len(self.bridge.tool_calls), 1)
        self.assertEqual(len(self.bridge.recorded_usage), 2)
        _request_id, usage, action = self.bridge.recorded_usage[-1]
        self.assertEqual(action, "call_premium_direct")
        self.assertEqual(usage.total_tokens, 111)

    def test_unknown_controls_fail_closed(self):
        payload = json.loads(self.chat())
        payload["response_format"] = {"type": "json_object"}
        response = self.application.handle(
            "POST", "/v1/chat/completions", self.auth(), json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(response.status, 400)

    def test_burst_requests_wait_for_a_slot_instead_of_failing_immediately(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingBridge(FakeBridge):
            def complete(inner_self, **kwargs):
                started.set()
                self.assertTrue(release.wait(2))
                return super(BlockingBridge, inner_self).complete(**kwargs)

        bridge = BlockingBridge()
        application = LocalOpenAIApplication(
            self.profile_store,
            self.credentials,
            bridge_factory=lambda profile, reader: bridge,
            maximum_in_flight=1,
        )
        results = []

        def send():
            results.append(
                application.handle("POST", "/v1/chat/completions", self.auth(), self.chat())
            )

        first = threading.Thread(target=send)
        second = threading.Thread(target=send)
        first.start()
        self.assertTrue(started.wait(1))
        second.start()
        self.assertTrue(second.is_alive())
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual([item.status for item in results], [200, 200])


if __name__ == "__main__":
    unittest.main()
