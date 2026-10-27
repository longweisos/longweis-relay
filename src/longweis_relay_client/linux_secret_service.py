"""Linux Secret Service adapter using the standard libsecret command."""

import re
import shutil
import subprocess
import sys
from typing import Callable, Optional, Sequence

from .client_errors import ValidationError


SECRET_SERVICE_NAME = "com.longweis.relay.bridge"
_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
CommandRunner = Callable[[Sequence[str], Optional[str]], object]


def _default_runner(arguments: Sequence[str], stdin_text: Optional[str]):
    return subprocess.run(
        list(arguments),
        input=stdin_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class LinuxSecretServiceCredentialStore:
    """Keep credentials in the user's desktop keyring, never in argv."""

    storage_id = "linux_secret_service"

    def __init__(
        self,
        service: str = SECRET_SERVICE_NAME,
        runner: Optional[CommandRunner] = None,
        executable: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> None:
        if not isinstance(service, str) or not _REFERENCE.fullmatch(service):
            raise ValidationError("Secret Service name is invalid")
        self.service = service
        self._runner = runner or _default_runner
        self.executable = executable or shutil.which("secret-tool") or "secret-tool"
        self.platform = platform or sys.platform

    @property
    def available(self) -> bool:
        return self.platform.startswith("linux") and bool(
            shutil.which(self.executable)
            if self.executable == "secret-tool"
            else self.executable
        )

    def __repr__(self) -> str:
        return "LinuxSecretServiceCredentialStore(service=%r, available=%r)" % (
            self.service,
            self.available,
        )

    def _account(self, reference: object) -> str:
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ValidationError("Secret Service credential reference is invalid")
        return reference

    def _require_available(self) -> None:
        if not self.available:
            raise ValidationError("Linux Secret Service is unavailable")

    def store(self, reference: str, secret: str) -> None:
        self._require_available()
        account = self._account(reference)
        if not isinstance(secret, str) or not secret.strip():
            raise ValidationError("Secret Service secret must not be empty")
        result = self._runner(
            (
                self.executable,
                "store",
                "--label=Longweis Relay",
                "service",
                self.service,
                "account",
                account,
            ),
            secret.strip(),
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise ValidationError("Secret Service could not store the credential")

    def read(self, reference: str) -> str:
        self._require_available()
        account = self._account(reference)
        result = self._runner(
            (
                self.executable,
                "lookup",
                "service",
                self.service,
                "account",
                account,
            ),
            None,
        )
        value = getattr(result, "stdout", "")
        if int(getattr(result, "returncode", 1)) != 0 or not isinstance(value, str) or not value.strip():
            raise ValidationError("Secret Service credential is unavailable")
        return value.rstrip("\r\n")

    def contains(self, reference: str) -> bool:
        try:
            self.read(reference)
        except ValidationError:
            return False
        return True
