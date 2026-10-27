import importlib.resources
import unittest

from longweis_relay_client.client_memory_mode import (
    MemoryModeSelection,
    MemoryPreparationProgress,
    memory_mode_setup_contract,
    memory_preparation_stages,
)
from longweis_relay_client.client_errors import ValidationError


class ClientMemoryModeTests(unittest.TestCase):
    def test_toggle_is_optional_and_has_two_explicit_up_to_claims(self):
        contract = memory_mode_setup_contract()
        self.assertFalse(contract["default_enabled"])
        self.assertTrue(contract["customer_can_change"])
        self.assertEqual(contract["disabled"]["maximum_savings_percent"], 50)
        self.assertEqual(contract["disabled"]["metric"], "premium_model_compute")
        self.assertEqual(contract["enabled"]["maximum_savings_percent"], 90)
        self.assertEqual(contract["enabled"]["metric"], "total_model_compute")
        self.assertIn("up to", contract["disabled"]["claim"].lower())
        self.assertIn("up to", contract["enabled"]["claim"].lower())

    def test_two_modes_defer_rates_to_the_server_account(self):
        contract = memory_mode_setup_contract()
        self.assertEqual(
            contract["disabled"]["relay_rate_source"],
            "server_account",
        )
        self.assertEqual(
            contract["enabled"]["relay_rate_source"],
            "server_account",
        )

    def test_memory_mode_presents_durable_memory_value_with_limits(self):
        value = memory_mode_setup_contract()["enabled"]["memory_value"]
        self.assertEqual(value["headline"], "Near-unlimited long-term memory")
        self.assertIn("keep growing", value["description"])
        self.assertTrue(value["subject_to_account_storage_limits"])

    def test_memory_preparation_has_no_separate_charge_but_retains_limits(self):
        preparation = memory_mode_setup_contract()["preparation"]
        self.assertFalse(preparation["separate_setup_charge"])
        self.assertTrue(preparation["subject_to_account_import_limits"])
        self.assertIn("longer", preparation["size_notice"])

    def test_toggle_requires_one_boolean_and_remains_customer_controlled(self):
        self.assertEqual(
            MemoryModeSelection.from_payload(
                {"memory_retrieval_enabled": True}
            ).public_payload(),
            {"memory_retrieval_enabled": True},
        )
        for payload in (
            {},
            {"memory_retrieval_enabled": "yes"},
            {"memory_retrieval_enabled": False, "server_policy": "hidden"},
        ):
            with self.assertRaises(ValidationError):
                MemoryModeSelection.from_payload(payload)

    def test_progress_is_coarse_determinate_or_indeterminate(self):
        scanning = MemoryPreparationProgress.from_payload(
            {"stage": "scanning_sources"}
        ).public_payload()
        self.assertTrue(scanning["indeterminate"])
        self.assertIsNone(scanning["percentage"])

        importing = MemoryPreparationProgress.from_payload(
            {
                "stage": "transferring_selected_content",
                "completed_units": 1,
                "total_units": 2,
            }
        ).public_payload()
        self.assertFalse(importing["indeterminate"])
        self.assertEqual(importing["percentage"], 30)

        organizing = MemoryPreparationProgress.from_payload(
            {
                "stage": "organizing_private_memory",
                "completed_units": 1,
                "total_units": 2,
            }
        ).public_payload()
        self.assertEqual(organizing["percentage"], 74)

        ready = MemoryPreparationProgress.from_payload(
            {"stage": "ready", "completed_units": 2, "total_units": 2}
        ).public_payload()
        self.assertEqual(ready["percentage"], 100)
        self.assertTrue(ready["ready"])

    def test_progress_rejects_internal_or_impossible_state(self):
        for payload in (
            {"stage": "burning_bricks"},
            {"stage": "organizing_private_memory", "completed_units": -1},
            {
                "stage": "organizing_private_memory",
                "completed_units": 2,
                "total_units": 1,
            },
            {"stage": "ready", "completed_units": 1, "total_units": 2},
        ):
            with self.assertRaises(ValidationError):
                MemoryPreparationProgress.from_payload(payload)
        self.assertEqual(
            memory_preparation_stages(),
            (
                "scanning_sources",
                "transferring_selected_content",
                "organizing_private_memory",
                "ready",
            ),
        )

    def test_packaged_ui_has_toggle_progress_and_no_private_terms(self):
        script = (
            importlib.resources.files("longweis_relay_client")
            .joinpath("relay_memory_mode.mjs")
            .read_text(encoding="utf-8")
        )
        self.assertIn('toggle.type = "checkbox"', script)
        self.assertIn('document.createElement("progress")', script)
        self.assertIn("up to about 50%", script)
        self.assertIn("up to about 90%", script)
        self.assertIn("Current Relay pricing is provided", script)
        self.assertNotIn("$0.20 per million input tokens", script)
        self.assertIn("Near-unlimited long-term memory", script)
        self.assertIn("account storage and import limits", script)
        self.assertIn("no separate setup charge", script)
        for private_term in ("server policy", "private route", "routing threshold"):
            self.assertNotIn(private_term, script)


if __name__ == "__main__":
    unittest.main()
