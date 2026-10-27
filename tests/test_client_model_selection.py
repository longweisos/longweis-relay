import json
import unittest
from pathlib import Path

from longweis_relay_client.client_model_selection import (
    DiscoveredModel,
    PremiumModelSelection,
    assess_cost_fit,
    filter_discovered_models,
    model_catalog_request,
    model_setup_contract,
    parse_model_catalog,
)
from longweis_relay_client.client_errors import ValidationError


class ClientModelSelectionTests(unittest.TestCase):
    def test_setup_lists_openrouter_and_always_requires_one_model(self):
        contract = model_setup_contract()
        providers = {item["id"]: item for item in contract["providers"]}
        self.assertTrue(contract["model_required"])
        self.assertTrue(contract["manual_model_entry"])
        self.assertTrue(providers["openrouter"]["aggregator"])
        self.assertTrue(providers["openrouter"]["manual_model_allowed"])
        self.assertEqual(
            contract["api_key_storage"], "local_os_credential_store_only"
        )

    def test_selection_requires_explicit_model_and_never_accepts_an_api_key(self):
        with self.assertRaises(ValidationError):
            PremiumModelSelection.from_payload({"provider": "openrouter"})
        with self.assertRaises(ValidationError):
            PremiumModelSelection.from_payload(
                {
                    "provider": "openrouter",
                    "model": "openai/frontier-model",
                    "api_key": "must-stay-local",
                }
            )
        selection = PremiumModelSelection.from_payload(
            {"provider": "openrouter", "model": "openai/frontier-model"}
        )
        self.assertEqual(
            selection.relay_payload(),
            {"provider": "openrouter", "premium_model": "openai/frontier-model"},
        )

    def test_known_low_cost_family_is_red_but_never_blocked(self):
        for model in (
            "google/gemini-3.7-flash",
            "anthropic/claude-haiku-4-5",
            "openai/gpt-5-nano",
            "provider/model:free",
        ):
            selection = PremiumModelSelection.from_payload(
                {"provider": "openrouter", "model": model}
            )
            notice = assess_cost_fit(selection)
            self.assertEqual(notice.level, "red", model)
            self.assertFalse(notice.blocks_selection, model)
            self.assertIn("continue anyway", notice.message)

    def test_cost_warning_does_not_embed_private_provider_candidates(self):
        source = Path("src/longweis_relay_client/client_model_selection.py").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("minimax", source)
        self.assertNotIn("deepseek", source)

    def test_unrecognized_model_is_allowed_with_neutral_cost_fit(self):
        selection = PremiumModelSelection.from_payload(
            {"provider": "openrouter", "model": "vendor/new-frontier-model"}
        )
        notice = assess_cost_fit(selection)
        self.assertEqual(notice.level, "neutral")
        self.assertFalse(notice.blocks_selection)

    def test_openrouter_discovery_preserves_specific_model_ids(self):
        request = model_catalog_request("openrouter", "customer-secret")
        self.assertEqual(
            request.full_url,
            "https://openrouter.ai/api/v1/models?output_modalities=text",
        )
        self.assertNotIn("customer-secret", request.full_url)
        models = parse_model_catalog(
            "openrouter",
            json.dumps(
                {
                    "data": [
                        {"id": "deepseek/deepseek-v3", "name": "DeepSeek V3"},
                        {"id": "openai/frontier-model", "name": "Frontier"},
                    ]
                }
            ).encode("utf-8"),
        )
        self.assertEqual(
            {item.model_id for item in models},
            {"deepseek/deepseek-v3", "openai/frontier-model"},
        )

    def test_model_filter_matches_id_label_and_forgiving_character_order(self):
        models = (
            DiscoveredModel("openai/gpt-5.6", "GPT-5.6"),
            DiscoveredModel("vendor/frontier", "General Purpose Transformer"),
            DiscoveredModel("vendor/other", "Other"),
        )
        self.assertEqual(
            [item.model_id for item in filter_discovered_models(models, "gpt")],
            ["openai/gpt-5.6", "vendor/frontier"],
        )
        self.assertEqual(
            [item.model_id for item in filter_discovered_models(models, "g56")],
            ["openai/gpt-5.6"],
        )

    def test_google_catalog_filters_non_generation_models(self):
        request = model_catalog_request("google", "customer-secret")
        self.assertNotIn("customer-secret", request.full_url)
        models = parse_model_catalog(
            "google",
            {
                "models": [
                    {
                        "name": "models/gemini-generation",
                        "displayName": "Gemini Generation",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding",
                        "displayName": "Embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )
        self.assertEqual([item.model_id for item in models], ["gemini-generation"])

    def test_custom_provider_requires_https_base_and_allows_manual_model(self):
        with self.assertRaises(ValidationError):
            PremiumModelSelection.from_payload(
                {
                    "provider": "custom-openai-compatible",
                    "model": "vendor/model",
                    "custom_base_url": "http://example.com/v1",
                }
            )
        selection = PremiumModelSelection.from_payload(
            {
                "provider": "custom-openai-compatible",
                "model": "vendor/model",
                "custom_base_url": "https://models.example.com/v1/",
            }
        )
        request = model_catalog_request(
            selection.provider_id, "customer-secret", selection.custom_base_url
        )
        self.assertEqual(request.full_url, "https://models.example.com/v1/models")
        self.assertNotIn("customer-secret", request.full_url)


if __name__ == "__main__":
    unittest.main()
