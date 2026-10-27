import json
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from longweis_relay_client.client_modes import RelayBillingMode
from longweis_relay_client.client_model_selection import PremiumModelSelection
from longweis_relay_client.client_profile import (
    CLIENT_PROFILE_VERSION,
    ClientConnectionMode,
    LocalBridgeProfile,
    LocalBridgeProfileStore,
)
from longweis_relay_client.client_errors import IntegrityConflict, ValidationError


class LocalBridgeProfileTests(unittest.TestCase):
    def profile(self):
        return LocalBridgeProfile.create(
            profile_id="tester-one",
            relay_base_url="https://api.longweisos.com",
            selection=PremiumModelSelection.from_payload(
                {
                    "provider": "openrouter",
                    "model": "openai/gpt-5.6-terra",
                }
            ),
            default_mode=RelayBillingMode.MEMORY_RETRIEVAL,
            memory_budget_tokens=1200,
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def test_atomic_private_profile_round_trip_contains_references_not_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "private", "bridge-profile.json")
            store = LocalBridgeProfileStore(path)
            profile = self.profile()
            store.save(profile)

            encoded = Path(path).read_text(encoding="utf-8")
            payload = json.loads(encoded)
            self.assertEqual(payload["version"], CLIENT_PROFILE_VERSION)
            self.assertEqual(payload["relay_credential_reference"], "tester-one/relay")
            self.assertEqual(
                payload["premium_credential_reference"], "tester-one/premium"
            )
            self.assertNotIn("relay-secret", encoded)
            self.assertNotIn("premium-secret", encoded)
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(Path(path).parent).st_mode & 0o777, 0o700)
            self.assertEqual(store.load(), profile)

    def test_integrity_hash_detects_profile_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge-profile.json"
            store = LocalBridgeProfileStore(str(path))
            store.save(self.profile())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["premium_model"] = "attacker/model"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(IntegrityConflict):
                store.load()

    def test_unknown_fields_and_symlink_targets_are_rejected(self):
        payload = self.profile().public_payload()
        payload["api_key"] = "must-never-be-accepted"
        with self.assertRaises(ValidationError):
            LocalBridgeProfile.from_payload(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = Path(temp_dir) / "profile.json"
            link.symlink_to(target)
            with self.assertRaises(ValidationError):
                LocalBridgeProfileStore(str(link))

    def test_shared_permissions_fail_closed_without_changing_them(self):
        if os.name != "posix":
            self.skipTest("POSIX permission bits are not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            shared = Path(temp_dir) / "shared"
            shared.mkdir(mode=0o755)
            os.chmod(shared, 0o755)
            store = LocalBridgeProfileStore(str(shared / "profile.json"))
            with self.assertRaisesRegex(ValidationError, "directory"):
                store.save(self.profile())
            self.assertEqual(os.stat(shared).st_mode & 0o777, 0o755)

            private = Path(temp_dir) / "private"
            private.mkdir(mode=0o700)
            store = LocalBridgeProfileStore(str(private / "profile.json"))
            store.save(self.profile())
            os.chmod(store.path, 0o644)
            with self.assertRaisesRegex(ValidationError, "permissions"):
                store.load()
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o644)

    def test_invalid_memory_budget_and_plaintext_relay_are_rejected(self):
        profile = self.profile()
        with self.assertRaises(ValidationError):
            LocalBridgeProfile.create(
                profile_id="tester-two",
                relay_base_url="http://api.longweisos.com",
                selection=profile.selection,
                default_mode=profile.default_mode,
                memory_budget_tokens=800,
            )
        with self.assertRaises(ValidationError):
            LocalBridgeProfile.create(
                profile_id="tester-two",
                relay_base_url="https://api.longweisos.com",
                selection=profile.selection,
                default_mode=profile.default_mode,
                memory_budget_tokens=0,
            )

    def test_legacy_profile_is_read_as_general_api_mode(self):
        payload = self.profile().public_payload()
        payload.pop("connection_mode")
        payload["version"] = "0.46.0"
        unsigned = {key: value for key, value in payload.items() if key != "config_hash"}
        payload["config_hash"] = hashlib.sha256(
            json.dumps(
                unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        migrated = LocalBridgeProfile.from_payload(payload)
        self.assertEqual(migrated.connection_mode, ClientConnectionMode.GENERAL_API)


if __name__ == "__main__":
    unittest.main()
