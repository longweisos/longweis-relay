"""Windows Credential Manager adapter for the local Relay client."""

import re
import sys
from typing import Optional

from .client_errors import ValidationError


WINDOWS_CREDENTIAL_PREFIX = "com.longweis.relay.bridge"
_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_MAX_BLOB_BYTES = 2560


class _CtypesCredentialBackend:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self.ctypes = ctypes
        self.Credential = Credential
        self.advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.advapi.CredWriteW.restype = wintypes.BOOL
        self.advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
        self.advapi.CredReadW.restype = wintypes.BOOL
        self.advapi.CredFree.argtypes = [ctypes.c_void_p]

    def write(self, target: str, secret: str) -> None:
        encoded = secret.encode("utf-16-le")
        blob = self.ctypes.create_string_buffer(encoded)
        credential = self.Credential()
        credential.Type = 1
        credential.TargetName = target
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = self.ctypes.cast(blob, self.ctypes.POINTER(self.ctypes.c_ubyte))
        credential.Persist = 2
        credential.UserName = "Longweis Relay"
        if not self.advapi.CredWriteW(self.ctypes.byref(credential), 0):
            raise OSError("CredWriteW failed")

    def read(self, target: str) -> str:
        pointer = self.ctypes.POINTER(self.Credential)()
        if not self.advapi.CredReadW(target, 1, 0, self.ctypes.byref(pointer)):
            raise OSError("CredReadW failed")
        try:
            credential = pointer.contents
            encoded = self.ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return encoded.decode("utf-16-le")
        finally:
            self.advapi.CredFree(pointer)


class WindowsCredentialManagerCredentialStore:
    """Use Windows Credential Manager without persisting plaintext keys."""

    storage_id = "windows_credential_manager"

    def __init__(
        self,
        prefix: str = WINDOWS_CREDENTIAL_PREFIX,
        backend: Optional[object] = None,
        platform: Optional[str] = None,
    ) -> None:
        if not isinstance(prefix, str) or not _REFERENCE.fullmatch(prefix):
            raise ValidationError("Windows credential prefix is invalid")
        self.prefix = prefix
        self.platform = platform or sys.platform
        self._backend = backend
        if self._backend is None and self.platform == "win32":
            try:
                self._backend = _CtypesCredentialBackend()
            except Exception:
                self._backend = None

    @property
    def available(self) -> bool:
        return self.platform == "win32" and self._backend is not None

    def __repr__(self) -> str:
        return "WindowsCredentialManagerCredentialStore(prefix=%r, available=%r)" % (
            self.prefix,
            self.available,
        )

    def _target(self, reference: object) -> str:
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ValidationError("Windows credential reference is invalid")
        return self.prefix + "/" + reference

    def _require_available(self) -> None:
        if not self.available:
            raise ValidationError("Windows Credential Manager is unavailable")

    def store(self, reference: str, secret: str) -> None:
        self._require_available()
        target = self._target(reference)
        if not isinstance(secret, str) or not secret.strip():
            raise ValidationError("Windows credential must not be empty")
        value = secret.strip()
        if len(value.encode("utf-16-le")) > _MAX_BLOB_BYTES:
            raise ValidationError("Windows credential exceeds the size limit")
        try:
            self._backend.write(target, value)
        except Exception:
            raise ValidationError("Windows Credential Manager could not store the credential") from None

    def read(self, reference: str) -> str:
        self._require_available()
        target = self._target(reference)
        try:
            value = self._backend.read(target)
        except Exception:
            raise ValidationError("Windows credential is unavailable") from None
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("Windows credential is unavailable")
        return value.strip()

    def contains(self, reference: str) -> bool:
        try:
            self.read(reference)
        except ValidationError:
            return False
        return True
