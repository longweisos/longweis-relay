"""Select the operating system's secure credential store."""

import sys
from typing import Optional

from .client_errors import ValidationError


def default_credential_store(platform: Optional[str] = None):
    value = platform or sys.platform
    if value == "darwin":
        from .macos_keychain import MacOSKeychainCredentialStore

        return MacOSKeychainCredentialStore(platform=value)
    if value == "win32":
        from .windows_credentials import WindowsCredentialManagerCredentialStore

        return WindowsCredentialManagerCredentialStore(platform=value)
    if value.startswith("linux"):
        from .linux_secret_service import LinuxSecretServiceCredentialStore

        return LinuxSecretServiceCredentialStore(platform=value)
    raise ValidationError("this desktop platform has no supported secure credential store")
