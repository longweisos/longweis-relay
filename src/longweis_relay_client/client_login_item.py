"""User-controlled macOS login startup for the local Relay API."""

import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .client_errors import ValidationError


LOGIN_ITEM_VERSION = "0.58.0"
LOGIN_ITEM_LABEL = "com.longweis.relay.background"
CommandRunner = Callable[[Sequence[str]], int]


def default_login_item_path() -> str:
    return str(Path.home() / "Library" / "LaunchAgents" / (LOGIN_ITEM_LABEL + ".plist"))


def _run(command: Sequence[str]) -> int:
    try:
        completed = subprocess.run(
            tuple(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    return int(completed.returncode)


class MacOSLoginItemManager:
    def __init__(
        self,
        app_executable: str,
        plist_path: Optional[str] = None,
        runner: Optional[CommandRunner] = None,
        uid: Optional[int] = None,
    ) -> None:
        candidate = Path(app_executable).expanduser()
        if (
            not isinstance(app_executable, str)
            or not app_executable.strip()
            or "\x00" in app_executable
            or candidate.is_symlink()
            or not candidate.is_file()
            or not os.access(str(candidate), os.X_OK)
        ):
            raise ValidationError("Longweis Relay application executable is invalid")
        self.app_executable = str(candidate.resolve())
        target = Path(plist_path or default_login_item_path()).expanduser()
        if target.is_symlink():
            raise ValidationError("Longweis Relay login item path is unsafe")
        self.plist_path = str(target.resolve())
        self.runner = runner or _run
        self.uid = os.getuid() if uid is None else int(uid)

    @property
    def domain(self) -> str:
        return "gui/%d" % self.uid

    @property
    def service(self) -> str:
        return self.domain + "/" + LOGIN_ITEM_LABEL

    def _payload(self) -> Mapping[str, object]:
        return {
            "Label": LOGIN_ITEM_LABEL,
            "ProgramArguments": [self.app_executable, "--background-service"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "ThrottleInterval": 10,
        }

    def enabled(self) -> bool:
        target = Path(self.plist_path)
        if not target.is_file() or target.is_symlink():
            return False
        if target.stat().st_mode & 0o077:
            return False
        try:
            with target.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return False
        return payload == self._payload()

    def enable(self) -> None:
        target = Path(self.plist_path)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise ValidationError("Longweis Relay login item path is unsafe")
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".relay-login-", dir=str(target.parent))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                plistlib.dump(dict(self._payload()), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(target))
            os.chmod(str(target), 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self.runner(("launchctl", "bootout", self.service))
        if self.runner(("launchctl", "bootstrap", self.domain, str(target))) != 0:
            raise ValidationError("Longweis Relay could not enable start at login")
        if not self.enabled():
            raise ValidationError("Longweis Relay login item verification failed")

    def disable(self) -> None:
        target = Path(self.plist_path)
        self.runner(("launchctl", "bootout", self.service))
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValidationError("Longweis Relay login item path is unsafe")
            target.unlink()
        if target.exists():
            raise ValidationError("Longweis Relay login item could not be removed")
