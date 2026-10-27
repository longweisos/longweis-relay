import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from longweis_relay_client.client_errors import ProviderResponseError, ValidationError
from longweis_relay_client.client_update import (
    MacOSUpdateClient,
    UpdatePreferences,
    UpdatePreferencesStore,
    checked_preferences,
    remind_tomorrow,
    should_check_automatically,
    should_notify_about,
)


class RecordingTransport:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return self.status, json.dumps(self.payload).encode("utf-8")


def manifest(version="0.65.29", url="https://relay.longweisos.com/releases/macos/Longweis-Relay-macOS-v0.65.29-arm64.dmg"):
    return {
        "object": "longweis_relay.macos_update",
        "schema_version": 1,
        "channel": "stable",
        "version": version,
        "build": "6528",
        "architecture": "arm64",
        "release_notes": "Security and reliability improvements.",
        "artifact": {
            "url": url,
            "sha256": "a" * 64,
            "size_bytes": 123456,
        },
    }


class ClientUpdateTests(unittest.TestCase):
    def test_p_edition_checks_only_its_own_p_prefixed_release_line(self):
        update = MacOSUpdateClient(
            "P0.60",
            transport=RecordingTransport(
                200,
                manifest(
                    "P0.61",
                    "https://relay.longweisos.com/releases/macos/Longweis-Relay-macOS-vP0.61-arm64.dmg",
                ),
            ),
        ).check()
        self.assertIsNotNone(update)
        self.assertEqual(update.filename, "Longweis-Relay-macOS-vP0.61-arm64.dmg")
        self.assertIsNone(
            MacOSUpdateClient("P0.60", transport=RecordingTransport(200, manifest())).check()
        )

    def test_current_p_edition_discovers_the_next_p_release(self):
        update = MacOSUpdateClient(
            "P0.68",
            transport=RecordingTransport(
                200,
                manifest(
                    "P0.69",
                    "https://relay.longweisos.com/releases/macos/Longweis-Relay-macOS-vP0.69-arm64.dmg",
                ),
            ),
        ).check()
        self.assertIsNotNone(update)
        self.assertEqual(update.version, "P0.69")

    def test_check_accepts_only_newer_official_arm64_release(self):
        transport = RecordingTransport(200, manifest())
        update = MacOSUpdateClient("0.65.28", transport=transport).check()
        self.assertIsNotNone(update)
        self.assertEqual(update.version, "0.65.29")
        self.assertEqual(update.filename, "Longweis-Relay-macOS-v0.65.29-arm64.dmg")
        request, unused_timeout = transport.calls[0]
        self.assertEqual(request.full_url, "https://relay.longweisos.com/releases/macos/manifest.json")
        self.assertNotIn("Authorization", request.headers)

    def test_check_rejects_downgrade_and_untrusted_artifact_origin(self):
        self.assertIsNone(
            MacOSUpdateClient(
                "0.65.28", transport=RecordingTransport(200, manifest("0.65.28"))
            ).check()
        )
        with self.assertRaises(ProviderResponseError):
            MacOSUpdateClient(
                "0.65.28",
                transport=RecordingTransport(
                    200,
                    manifest(url="https://attacker.example/Longweis-Relay.dmg"),
                ),
            ).check()
        with self.assertRaises(ProviderResponseError):
            MacOSUpdateClient("0.65.28", manifest_url="http://relay.longweisos.com/releases/macos/manifest.json")

    def test_preferences_are_owner_only_and_honor_remind_and_skip_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = UpdatePreferencesStore(str(path))
            now = datetime(2026, 9, 10, tzinfo=timezone.utc)
            initial = UpdatePreferences()
            stored = checked_preferences(initial, now)
            store.save(stored)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(should_check_automatically(store.load(), now + timedelta(hours=1)))
            delayed = remind_tomorrow(store.load(), now)
            store.save(delayed)
            update = MacOSUpdateClient(
                "0.65.28", transport=RecordingTransport(200, manifest())
            ).check()
            self.assertFalse(should_notify_about(update, store.load(), now + timedelta(hours=2)))
            self.assertTrue(should_notify_about(update, store.load(), now + timedelta(days=2)))
            skipped = UpdatePreferences(skipped_version=update.version)
            self.assertFalse(should_notify_about(update, skipped, now))


if __name__ == "__main__":
    unittest.main()
