import json
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from longweis_relay_client.client_profile import LocalBridgeProfileStore
from longweis_relay_client.client_profile import ClientConnectionMode
from longweis_relay_client.client_setup import (
    CLIENT_SETUP_VERSION,
    LocalBridgeSettingsService,
    LocalModelCatalogClient,
    supported_client_providers,
)
from longweis_relay_client import client_setup
from longweis_relay_client.client_errors import ProviderResponseError, ValidationError


class FakeCredentialStore:
    available = True

    def __init__(self):
        self.values = {}
        self.stores = []
        self.reads = []

    def store(self, reference, secret):
        self.stores.append((reference, secret))
        self.values[reference] = secret

    def read(self, reference):
        self.reads.append(reference)
        if reference not in self.values:
            raise ValidationError("credential unavailable")
        return self.values[reference]

    def contains(self, reference):
        return reference in self.values

    def delete(self, reference):
        self.values.pop(reference, None)


def catalog_payload(*model_ids):
    return json.dumps(
        {"data": [{"id": model_id, "name": model_id} for model_id in model_ids]}
    ).encode("utf-8")


class ClientSetupTests(unittest.TestCase):
    def service(self, temp_dir, credentials=None, transport=None):
        return LocalBridgeSettingsService(
            LocalBridgeProfileStore(os.path.join(temp_dir, "bridge-profile.json")),
            credential_store=credentials or FakeCredentialStore(),
            catalog_client=LocalModelCatalogClient(transport=transport),
        )

    def test_supported_provider_list_matches_the_implemented_local_protocols(self):
        self.assertEqual(
            [item["id"] for item in supported_client_providers()],
            ["openai", "openrouter", "custom-openai-compatible"],
        )

    def test_first_save_puts_both_keys_in_keychain_and_none_in_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = FakeCredentialStore()
            service = self.service(temp_dir, credentials)
            receipt = service.save(
                profile_id="tester-one",
                relay_base_url="https://api.longweisos.com/v1",
                provider_id="openrouter",
                model_id="openai/gpt-5.6-terra",
                custom_base_url=None,
                memory_retrieval_enabled=False,
                memory_budget_tokens=800,
                relay_key="relay-super-secret",
                premium_key="premium-super-secret",
                now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )

            self.assertEqual(receipt["client_setup_version"], CLIENT_SETUP_VERSION)
            self.assertEqual(
                credentials.values["tester-one/relay"], "relay-super-secret"
            )
            self.assertEqual(
                credentials.values["tester-one/premium"], "premium-super-secret"
            )
            self.assertTrue(
                credentials.values["tester-one/local-api"].startswith("lsk_")
            )
            self.assertEqual(
                service.read_local_api_key(),
                credentials.values["tester-one/local-api"],
            )
            profile_text = Path(service.profile_store.path).read_text(encoding="utf-8")
            public_text = json.dumps(receipt)
            for secret in credentials.values.values():
                self.assertNotIn(secret, profile_text)
                self.assertNotIn(secret, public_text)
                self.assertNotIn(secret, repr(service))
            snapshot = service.snapshot()
            self.assertTrue(snapshot.ready)
            self.assertEqual(snapshot.public_payload()["network_calls"], 0)

    def test_existing_keys_can_be_kept_while_nonsecret_settings_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = FakeCredentialStore()
            service = self.service(temp_dir, credentials)
            first_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
            service.save(
                profile_id="tester-two",
                relay_base_url="https://api.longweisos.com/v1",
                provider_id="openrouter",
                model_id="openai/model-one",
                custom_base_url=None,
                memory_retrieval_enabled=False,
                memory_budget_tokens=800,
                relay_key="relay-secret",
                premium_key="premium-secret",
                now=first_time,
            )
            credentials.stores.clear()

            receipt = service.save(
                profile_id="tester-two",
                relay_base_url="https://api.longweisos.com/v1",
                provider_id="openrouter",
                model_id="openai/model-two",
                custom_base_url=None,
                memory_retrieval_enabled=True,
                memory_budget_tokens=1200,
                relay_key="",
                premium_key="",
                now=first_time + timedelta(minutes=1),
            )

            self.assertEqual(credentials.stores, [])
            self.assertFalse(receipt["relay_key_replaced"])
            self.assertFalse(receipt["premium_key_replaced"])
            self.assertTrue(service.snapshot().memory_retrieval_enabled)
            self.assertEqual(service.profile_store.load().created_at, first_time)

    def test_reset_removes_both_keys_and_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = FakeCredentialStore()
            service = self.service(temp_dir, credentials)
            service.save(
                profile_id="tester-reset",
                relay_base_url="https://api.longweisos.com/v1",
                provider_id="openrouter",
                model_id="openai/model",
                custom_base_url=None,
                memory_retrieval_enabled=False,
                memory_budget_tokens=800,
                relay_key="relay-secret",
                premium_key="premium-secret",
            )
            receipt = service.reset()
            self.assertEqual(receipt["credentials_removed"], 3)
            self.assertTrue(receipt["profile_removed"])
            self.assertEqual(credentials.values, {})
            self.assertFalse(Path(service.profile_store.path).exists())

    def test_first_save_requires_both_credentials_before_writing_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = FakeCredentialStore()
            service = self.service(temp_dir, credentials)
            with self.assertRaisesRegex(ValidationError, "premium provider credential"):
                service.save(
                    profile_id="tester-three",
                    relay_base_url="https://api.longweisos.com/v1",
                    provider_id="openrouter",
                    model_id="openai/model",
                    custom_base_url=None,
                    memory_retrieval_enabled=False,
                    memory_budget_tokens=800,
                    relay_key="relay-only",
                )
            self.assertEqual(credentials.stores, [])
            self.assertFalse(Path(service.profile_store.path).exists())

    def test_discovery_goes_directly_to_provider_and_returns_no_secret(self):
        captured = []

        def transport(request, timeout):
            captured.append((request, timeout))
            return 200, catalog_payload(
                "openai/gpt-5.6-terra", "deepseek/deepseek-v3"
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir, transport=transport)
            models = service.discover_models(
                provider_id="openrouter",
                custom_base_url=None,
                premium_key="premium-super-secret",
            )

        request, timeout = captured[0]
        self.assertEqual(timeout, 30.0)
        self.assertEqual(
            request.full_url,
            "https://openrouter.ai/api/v1/models?output_modalities=text",
        )
        self.assertNotIn("premium-super-secret", request.full_url)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer premium-super-secret"
        )
        result = json.dumps([item.public_payload() for item in models])
        self.assertNotIn("premium-super-secret", result)
        self.assertEqual(
            {item.model_id for item in models},
            {"openai/gpt-5.6-terra", "deepseek/deepseek-v3"},
        )

    def test_discovery_can_read_existing_key_but_only_for_matching_provider(self):
        calls = []

        def transport(request, timeout):
            calls.append((request, timeout))
            return 200, catalog_payload("openai/model")

        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = FakeCredentialStore()
            service = self.service(temp_dir, credentials, transport)
            service.save(
                profile_id="tester-four",
                relay_base_url="https://api.longweisos.com/v1",
                provider_id="openrouter",
                model_id="openai/model",
                custom_base_url=None,
                memory_retrieval_enabled=False,
                memory_budget_tokens=800,
                relay_key="relay-secret",
                premium_key="premium-secret",
            )
            credentials.reads.clear()
            service.discover_models(
                provider_id="openrouter",
                custom_base_url=None,
                premium_key="",
            )
            self.assertEqual(credentials.reads, ["tester-four/premium"])
            with self.assertRaisesRegex(ValidationError, "enter the premium"):
                service.discover_models(
                    provider_id="openai",
                    custom_base_url=None,
                    premium_key="",
                )

    def test_provider_failure_is_sanitized(self):
        def transport(request, timeout):
            del request, timeout
            raise RuntimeError("premium-super-secret")

        client = LocalModelCatalogClient(transport=transport)
        with self.assertRaises(ProviderResponseError) as raised:
            client.discover("openrouter", "premium-super-secret")
        self.assertNotIn("premium-super-secret", str(raised.exception))

    def test_default_discovery_refuses_redirects_before_forwarding_a_key(self):
        handler = client_setup._NoProviderRedirects()
        self.assertIsNone(
            handler.redirect_request(
                object(), object(), 302, "Found", {}, "https://other.example/models"
            )
        )

    def test_relocated_python_uses_a_system_ca_bundle_when_default_store_is_empty(self):
        class EmptyContext:
            def cert_store_stats(self):
                return {"x509_ca": 0}

        loaded_context = object()
        with tempfile.TemporaryDirectory() as temp_dir:
            ca_file = os.path.join(temp_dir, "cert.pem")
            Path(ca_file).write_text("test-ca", encoding="utf-8")
            with patch.object(client_setup, "_SYSTEM_CA_FILES", (ca_file,)), patch.object(
                client_setup.ssl,
                "create_default_context",
                side_effect=(EmptyContext(), loaded_context),
            ) as create_context:
                self.assertIs(client_setup._provider_tls_context(), loaded_context)
        self.assertEqual(create_context.call_args_list[1].kwargs, {"cafile": ca_file})


if __name__ == "__main__":
    unittest.main()
