"""Single-instance background lifecycle for the local Relay API."""

import errno
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

try:
    import fcntl
except ImportError:  # Windows has no POSIX advisory locking.
    fcntl = None

from .client_errors import IntegrityConflict, ProjectSError, ValidationError
from .client_local_api import LOCAL_API_HOST, LOCAL_API_MODEL, LOCAL_API_PORT
from .client_platform import default_client_runtime_dir


# Bump this whenever packaged local-API behavior changes.  The settings app uses
# this value to retire a still-running daemon from an older application build.
# It is deliberately tied to the ordinary desktop release rather than a hidden
# protocol number, so an older worker cannot silently keep serving new UI.
BACKGROUND_SERVICE_VERSION = "P0.70"
_MAX_CONTROL_BYTES = 16 * 1024
_INITIAL_API_STARTUP_RETRY_SECONDS = 10.0


def default_background_runtime_dir() -> str:
    return default_client_runtime_dir()


@dataclass(frozen=True)
class BackgroundPaths:
    root: str
    lock: str
    control_socket: str
    desired_state: str

    @classmethod
    def from_runtime_dir(cls, runtime_dir: str) -> "BackgroundPaths":
        if not isinstance(runtime_dir, str) or not runtime_dir.strip() or "\x00" in runtime_dir:
            raise ValidationError("background runtime path is invalid")
        unresolved = Path(runtime_dir).expanduser()
        if unresolved.is_symlink():
            raise ValidationError("background runtime must not be a symbolic link")
        root = unresolved.resolve()
        socket_path = root / "control.sock"
        if len(os.fsencode(str(socket_path))) >= 100:
            raise ValidationError("background runtime path is too long")
        return cls(
            str(root),
            str(root / "service.lock"),
            str(socket_path),
            str(root / "desired-state"),
        )

    def ensure(self) -> None:
        root = Path(self.root)
        existed = root.exists()
        if existed and (not root.is_dir() or root.is_symlink()):
            raise ValidationError("background runtime directory is unsafe")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if existed and root.stat().st_mode & 0o077:
            raise ValidationError("background runtime directory must be owner-only")
        os.chmod(str(root), 0o700)

    def set_desired_state(self, value: str) -> None:
        if value not in {"running", "stopped"}:
            raise ValidationError("background desired state is invalid")
        self.ensure()
        target = Path(self.desired_state)
        if target.is_symlink():
            raise ValidationError("background desired state is unsafe")
        temporary = Path(self.root) / (
            ".desired-%d-%d" % (os.getpid(), threading.get_ident())
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(str(temporary), str(target))
            os.chmod(str(target), 0o600)
        finally:
            try:
                os.unlink(str(temporary))
            except FileNotFoundError:
                pass

    def read_desired_state(self) -> Optional[str]:
        target = Path(self.desired_state)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file() or target.stat().st_mode & 0o077:
            raise ValidationError("background desired state is unsafe")
        try:
            value = target.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            raise ValidationError("background desired state is invalid") from None
        if value not in {"running", "stopped"}:
            raise ValidationError("background desired state is invalid")
        return value


class BackgroundInstanceLock:
    def __init__(self, paths: BackgroundPaths) -> None:
        self.paths = paths
        self._descriptor = None

    def acquire(self) -> None:
        if fcntl is None:
            raise ValidationError(
                "background service locking is unavailable on this platform"
            )
        self.paths.ensure()
        lock = Path(self.paths.lock)
        if lock.is_symlink():
            raise ValidationError("background lock must not be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(lock), flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValidationError("background lock file is unsafe")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise IntegrityConflict("background service is already running") from None
                raise
            payload = json.dumps(
                {"pid": os.getpid(), "version": BACKGROUND_SERVICE_VERSION},
                sort_keys=True,
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None and fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@dataclass(frozen=True)
class BackgroundStatus:
    running: bool
    service_running: bool
    pid: Optional[int]
    base_url: str
    public_model: str
    recovery_count: int
    last_recovery_reason: Optional[str]
    service_version: str

    @classmethod
    def stopped(cls) -> "BackgroundStatus":
        return cls(
            False,
            False,
            None,
            "http://%s:%d/v1" % (LOCAL_API_HOST, LOCAL_API_PORT),
            LOCAL_API_MODEL,
            0,
            None,
            BACKGROUND_SERVICE_VERSION,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "BackgroundStatus":
        if not isinstance(payload, dict):
            raise ValidationError("background status is invalid")
        required = {
            "service_version",
            "service_running",
            "api_running",
            "pid",
            "base_url",
            "model",
            "recovery_count",
            "last_recovery_reason",
        }
        if set(payload) != required:
            raise ValidationError("background status is invalid")
        if (
            not isinstance(payload["service_running"], bool)
            or not isinstance(payload["api_running"], bool)
            or not isinstance(payload["pid"], int)
            or isinstance(payload["recovery_count"], bool)
            or not isinstance(payload["recovery_count"], int)
            or payload["recovery_count"] < 0
            or payload["model"] != LOCAL_API_MODEL
            or not isinstance(payload["base_url"], str)
            or not payload["base_url"].startswith("http://127.0.0.1:")
            or payload["last_recovery_reason"] not in {None, "sleep_wake", "api_stopped"}
            or not isinstance(payload["service_version"], str)
        ):
            raise ValidationError("background status is invalid")
        return cls(
            payload["api_running"],
            payload["service_running"],
            payload["pid"],
            payload["base_url"],
            payload["model"],
            payload["recovery_count"],
            payload["last_recovery_reason"],
            payload["service_version"],
        )


def _control_request(paths: BackgroundPaths, command: str, timeout: float = 1.0):
    if command not in {"status", "start", "stop"}:
        raise ValidationError("background command is invalid")
    control = Path(paths.control_socket)
    if control.is_symlink():
        raise ValidationError("background control socket is unsafe")
    try:
        metadata = control.stat()
    except FileNotFoundError:
        raise ValidationError("background service is unavailable") from None
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ValidationError("background control socket is unsafe")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(paths.control_socket)
        client.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
        chunks = []
        total = 0
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CONTROL_BYTES:
                raise ValidationError("background response is too large")
            if b"\n" in chunk:
                break
    except (OSError, TimeoutError):
        raise ValidationError("background service is unavailable") from None
    finally:
        client.close()
    try:
        return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValidationError("background response is invalid") from None


class BackgroundServiceClient:
    """Small GUI-side controller that never reads either model credential."""

    def __init__(
        self,
        paths: BackgroundPaths,
        service_command: Optional[Sequence[str]] = None,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(paths, BackgroundPaths):
            raise ValidationError("background paths are invalid")
        if service_command is not None and (
            not isinstance(service_command, (list, tuple))
            or not service_command
            or any(not isinstance(item, str) or not item for item in service_command)
        ):
            raise ValidationError("background service command is invalid")
        if (
            isinstance(startup_timeout_seconds, bool)
            or not isinstance(startup_timeout_seconds, (int, float))
            or not (0.5 <= float(startup_timeout_seconds) <= 30.0)
        ):
            raise ValidationError("background startup timeout is invalid")
        self.paths = paths
        self.service_command = tuple(service_command or ())
        self.startup_timeout_seconds = float(startup_timeout_seconds)

    def status(self) -> BackgroundStatus:
        self.paths.ensure()
        control = Path(self.paths.control_socket)
        if control.is_symlink():
            raise ValidationError("background control socket is unsafe")
        try:
            metadata = control.stat()
        except FileNotFoundError:
            return BackgroundStatus.stopped()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValidationError("background control socket is unsafe")
        try:
            payload = _control_request(self.paths, "status")
        except ValidationError:
            return BackgroundStatus.stopped()
        return BackgroundStatus.from_payload(payload)

    def start(self) -> BackgroundStatus:
        self.paths.set_desired_state("running")
        current = self.status()
        if current.service_running:
            if current.service_version != BACKGROUND_SERVICE_VERSION:
                self.stop()
                self.paths.set_desired_state("running")
            elif current.running:
                return current
            else:
                return BackgroundStatus.from_payload(
                    _control_request(self.paths, "start")
                )
        if not self.service_command:
            raise ValidationError("background service command is unavailable")
        # The bundled macOS launcher does not reliably complete its runtime
        # handoff when both output descriptors are DEVNULL.  Give it a private,
        # truncated launch log instead.  The command contains no credentials;
        # this is solely for short startup diagnostics and remains owner-only.
        launch_log = Path(self.paths.root) / "background-launch.log"
        if launch_log.exists() and (launch_log.is_symlink() or not launch_log.is_file()):
            raise ValidationError("background launch log is unsafe")
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(launch_log), flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            subprocess.Popen(
                list(self.service_command),
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=descriptor,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            status = self.status()
            if status.service_running and status.running:
                return status
            # The packaged macOS launcher may hand off to its app process and
            # exit before the control socket is ready.  Treat the socket as
            # the source of truth and wait through the bounded startup window.
            time.sleep(0.05)
        raise ValidationError("background service could not start")

    def stop(self) -> BackgroundStatus:
        self.paths.set_desired_state("stopped")
        current = self.status()
        if not current.service_running:
            return current
        _control_request(self.paths, "stop")
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            current = self.status()
            if not current.service_running:
                return current
            time.sleep(0.05)
        raise ValidationError("background service could not stop")


class BackgroundRelayService:
    """Owns the local API after the settings window exits."""

    def __init__(
        self,
        controller: object,
        paths: BackgroundPaths,
        monotonic: Callable[[], float] = time.monotonic,
        wake_gap_seconds: float = 20.0,
    ) -> None:
        if not callable(monotonic) or wake_gap_seconds < 2.0:
            raise ValidationError("background watchdog configuration is invalid")
        self.controller = controller
        self.paths = paths
        self.monotonic = monotonic
        self.wake_gap_seconds = float(wake_gap_seconds)
        self.recovery_count = 0
        self.last_recovery_reason = None
        self._last_tick = self.monotonic()
        self._stop = threading.Event()
        self._lock = BackgroundInstanceLock(paths)
        self._listener = None

    def _status_payload(self) -> Dict[str, object]:
        api = self.controller.status()
        return {
            "service_version": BACKGROUND_SERVICE_VERSION,
            "service_running": True,
            "api_running": bool(api.running),
            "pid": os.getpid(),
            "base_url": str(api.base_url),
            "model": LOCAL_API_MODEL,
            "recovery_count": self.recovery_count,
            "last_recovery_reason": self.last_recovery_reason,
        }

    def _restart_api(self, reason: str) -> None:
        try:
            self.controller.stop()
        except ProjectSError:
            pass
        self.controller.start()
        self.recovery_count += 1
        self.last_recovery_reason = reason

    def _start_api_with_retry(self) -> None:
        """Wait out a just-closed loopback port without losing the daemon."""

        deadline = time.monotonic() + _INITIAL_API_STARTUP_RETRY_SECONDS
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.controller.start()
                return
            except ProjectSError as error:
                last_error = error
                time.sleep(0.1)
        if last_error is not None:
            raise last_error
        raise ValidationError("local API could not start")

    def watchdog_tick(self) -> None:
        now = self.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now
        if elapsed > self.wake_gap_seconds:
            self._restart_api("sleep_wake")
        elif not self.controller.status().running:
            self._restart_api("api_stopped")

    def _open_listener(self) -> socket.socket:
        self.paths.ensure()
        control = Path(self.paths.control_socket)
        if control.exists() or control.is_symlink():
            if control.is_symlink() or not stat.S_ISSOCK(control.stat().st_mode):
                raise ValidationError("background control socket is unsafe")
            os.unlink(str(control))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.paths.control_socket)
            os.chmod(self.paths.control_socket, 0o600)
            listener.listen(4)
            listener.settimeout(0.5)
        except Exception:
            listener.close()
            raise
        return listener

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(1.0)
        try:
            request = connection.recv(_MAX_CONTROL_BYTES + 1)
            if len(request) > _MAX_CONTROL_BYTES:
                raise ValidationError("background request is too large")
            payload = json.loads(request.split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"command"}:
                raise ValidationError("background request is invalid")
            command = payload["command"]
            if command == "start":
                if not self.controller.status().running:
                    self.controller.start()
            elif command == "stop":
                self._stop.set()
            elif command != "status":
                raise ValidationError("background command is invalid")
            response = self._status_payload()
        except Exception:
            response = {"error": "invalid_background_request"}
        try:
            connection.sendall(
                json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"
            )
        except OSError:
            pass

    def run(self) -> None:
        self._lock.acquire()
        try:
            self._start_api_with_retry()
            self._listener = self._open_listener()
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, lambda signum, frame: self._stop.set())
                signal.signal(signal.SIGINT, lambda signum, frame: self._stop.set())
            while not self._stop.is_set():
                try:
                    connection, unused_address = self._listener.accept()
                except socket.timeout:
                    connection = None
                if connection is not None:
                    with connection:
                        self._handle_connection(connection)
                try:
                    self.watchdog_tick()
                except ProjectSError:
                    pass
        finally:
            try:
                self.controller.stop()
            finally:
                if self._listener is not None:
                    self._listener.close()
                control = Path(self.paths.control_socket)
                if control.exists() and not control.is_symlink() and stat.S_ISSOCK(control.stat().st_mode):
                    os.unlink(str(control))
                self._lock.release()
