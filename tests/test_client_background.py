import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from longweis_relay_client.client_background import (
    BACKGROUND_SERVICE_VERSION,
    BackgroundInstanceLock,
    BackgroundPaths,
    BackgroundRelayService,
    BackgroundServiceClient,
)
from longweis_relay_client.client_errors import IntegrityConflict, ValidationError


class FakeController:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.running = True
        self.starts += 1
        return self.status()

    def stop(self):
        self.running = False
        self.stops += 1
        return self.status()

    def status(self):
        return SimpleNamespace(
            running=self.running,
            base_url="http://127.0.0.1:18777/v1",
        )


class RetryController(FakeController):
    def __init__(self, failed_starts):
        super().__init__()
        self.failed_starts = failed_starts

    def start(self):
        self.starts += 1
        if self.failed_starts:
            self.failed_starts -= 1
            raise ValidationError("local API port is unavailable")
        self.running = True
        return self.status()


@unittest.skipUnless(os.name == "posix", "background daemon is POSIX-only")
class ClientBackgroundTests(unittest.TestCase):
    def make_paths(self):
        root = tempfile.mkdtemp(prefix="relay-bg-", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        return BackgroundPaths.from_runtime_dir(root)

    def test_owner_only_paths_and_single_instance_lock(self):
        paths = self.make_paths()
        first = BackgroundInstanceLock(paths)
        second = BackgroundInstanceLock(paths)
        first.acquire()
        self.addCleanup(first.release)
        self.assertEqual(os.stat(paths.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(paths.lock).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(IntegrityConflict, "already running"):
            second.acquire()

    def test_runtime_rejects_shared_directory_and_symbolic_link(self):
        paths = self.make_paths()
        os.chmod(paths.root, 0o755)
        with self.assertRaisesRegex(ValidationError, "owner-only"):
            paths.ensure()
        target = tempfile.mkdtemp(prefix="relay-real-", dir="/tmp")
        self.addCleanup(shutil.rmtree, target, True)
        link = target + "-link"
        os.symlink(target, link)
        self.addCleanup(os.unlink, link)
        with self.assertRaisesRegex(ValidationError, "symbolic link"):
            BackgroundPaths.from_runtime_dir(link)

    def test_service_survives_gui_and_recovers_after_sleep_gap(self):
        paths = self.make_paths()
        controller = FakeController()
        clock = [100.0]
        service = BackgroundRelayService(
            controller,
            paths,
            monotonic=lambda: clock[0],
            wake_gap_seconds=2.0,
        )
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=3))
        client = BackgroundServiceClient(paths)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not client.status().service_running:
            time.sleep(0.02)
        status = client.status()
        self.assertTrue(status.service_running)
        self.assertTrue(status.running)
        self.assertEqual(status.service_version, BACKGROUND_SERVICE_VERSION)
        self.assertEqual(controller.starts, 1)

        # The GUI-side client can disappear; the service and API remain alive.
        del client
        self.assertTrue(controller.running)

        clock[0] += 30.0
        deadline = time.monotonic() + 3
        observer = BackgroundServiceClient(paths)
        while time.monotonic() < deadline:
            status = observer.status()
            if status.recovery_count == 1:
                break
            time.sleep(0.05)
        self.assertEqual(status.last_recovery_reason, "sleep_wake")
        self.assertEqual(controller.starts, 2)
        self.assertGreaterEqual(controller.stops, 1)

        stopped = observer.stop()
        self.assertFalse(stopped.service_running)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_service_recovers_if_api_thread_stops(self):
        paths = self.make_paths()
        controller = FakeController()
        service = BackgroundRelayService(controller, paths)
        service._last_tick = service.monotonic()
        controller.running = False
        service.watchdog_tick()
        self.assertTrue(controller.running)
        self.assertEqual(service.recovery_count, 1)
        self.assertEqual(service.last_recovery_reason, "api_stopped")

    def test_initial_start_retries_a_just_closed_loopback_port(self):
        controller = RetryController(failed_starts=2)
        service = BackgroundRelayService(controller, self.make_paths())
        service._start_api_with_retry()
        self.assertTrue(controller.running)
        self.assertEqual(controller.starts, 3)

    def test_client_without_service_command_cannot_spawn(self):
        client = BackgroundServiceClient(self.make_paths())
        self.assertFalse(client.status().service_running)
        with self.assertRaisesRegex(ValidationError, "command is unavailable"):
            client.start()
        self.assertEqual(client.paths.read_desired_state(), "running")

    def test_explicit_stop_records_intent_before_service_is_gone(self):
        paths = self.make_paths()
        paths.set_desired_state("running")
        self.assertEqual(paths.read_desired_state(), "running")
        BackgroundServiceClient(paths).stop()
        self.assertEqual(paths.read_desired_state(), "stopped")

    def test_desired_state_rejects_invalid_values_and_shared_files(self):
        paths = self.make_paths()
        with self.assertRaisesRegex(ValidationError, "desired state is invalid"):
            paths.set_desired_state("maybe")
        paths.set_desired_state("running")
        os.chmod(paths.desired_state, 0o644)
        with self.assertRaisesRegex(ValidationError, "desired state is unsafe"):
            paths.read_desired_state()


if __name__ == "__main__":
    unittest.main()
