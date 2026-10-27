"""macOS Keychain credential adapter for the local Relay Bridge."""

import re
import shutil
import subprocess
import sys
from typing import Callable, Optional, Sequence

from .client_errors import ValidationError


KEYCHAIN_SERVICE = "com.longweis.relay.bridge"
_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
CommandRunner = Callable[[Sequence[str], Optional[str]], object]


def _reference(value: object) -> str:
    if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
        raise ValidationError("Keychain credential reference is invalid")
    return value


def _default_runner(arguments: Sequence[str], stdin_text: Optional[str]):
    return subprocess.run(
        list(arguments),
        input=stdin_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class MacOSKeychainCredentialStore:
    """Read and update generic passwords without putting secrets in argv."""

    storage_id = "macos_keychain"

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE,
        runner: Optional[CommandRunner] = None,
        executable: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> None:
        if not isinstance(service, str) or not _REFERENCE.fullmatch(service):
            raise ValidationError("Keychain service name is invalid")
        self.service = service
        self._runner = runner or _default_runner
        self.executable = executable or shutil.which("security") or "security"
        self.platform = platform or sys.platform

    @property
    def available(self) -> bool:
        return self.platform == "darwin" and bool(
            shutil.which(self.executable)
            if self.executable == "security"
            else self.executable
        )

    def __repr__(self) -> str:
        return "MacOSKeychainCredentialStore(service=%r, available=%r)" % (
            self.service,
            self.available,
        )

    def _require_available(self) -> None:
        if not self.available:
            raise ValidationError("macOS Keychain is unavailable")

    def store(self, reference: str, secret: str) -> None:
        self._require_available()
        account = _reference(reference)
        if not isinstance(secret, str) or not secret.strip():
            raise ValidationError("Keychain secret must not be empty")
        normalized = secret.strip()
        if "\n" in normalized or "\r" in normalized:
            raise ValidationError("Keychain secret must be a single line")
        # A trailing -w invokes security(1)'s interactive new-password flow,
        # which asks for the value twice.  Supplying both lines through stdin
        # keeps the secret out of the process argument vector.
        arguments = (
            self.executable,
            "add-generic-password",
            "-a",
            account,
            "-s",
            self.service,
            "-U",
            "-w",
        )
        result = self._runner(arguments, normalized + "\n" + normalized + "\n")
        if int(getattr(result, "returncode", 1)) != 0:
            raise ValidationError("Keychain could not store the credential")

    def read(self, reference: str) -> str:
        self._require_available()
        account = _reference(reference)
        arguments = (
            self.executable,
            "find-generic-password",
            "-a",
            account,
            "-s",
            self.service,
            "-w",
        )
        result = self._runner(arguments, None)
        if int(getattr(result, "returncode", 1)) != 0:
            raise ValidationError("Keychain credential is unavailable")
        value = getattr(result, "stdout", "")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("Keychain credential is unavailable")
        return value.rstrip("\r\n")

    def contains(self, reference: str) -> bool:
        try:
            self.read(reference)
        except ValidationError:
            return False
        return True

    def delete(self, reference: str) -> None:
        self._require_available()
        account = _reference(reference)
        arguments = (
            self.executable,
            "delete-generic-password",
            "-a",
            account,
            "-s",
            self.service,
        )
        result = self._runner(arguments, None)
        if int(getattr(result, "returncode", 1)) not in (0, 44):
            raise ValidationError("Keychain could not remove the credential")
