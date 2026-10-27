"""Credential-free persistent profile for the local Relay Bridge."""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Mapping, Optional

from .client_endpoint import normalize_relay_base_url
from .client_modes import RelayBillingMode
from .client_model_selection import PremiumModelSelection
from .client_errors import IntegrityConflict, ValidationError
from .client_platform import default_client_data_dir


CLIENT_PROFILE_VERSION = "0.62.0"
_LEGACY_CLIENT_PROFILE_VERSION = "0.46.0"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_MAX_PROFILE_BYTES = 64 * 1024
_PROFILE_FIELDS = frozenset(
    {
        "version",
        "profile_id",
        "relay_base_url",
        "connection_mode",
        "relay_credential_reference",
        "premium_provider",
        "premium_model",
        "custom_base_url",
        "premium_credential_reference",
        "default_mode",
        "memory_budget_tokens",
        "created_at",
        "updated_at",
        "config_hash",
    }
)
_LEGACY_PROFILE_FIELDS = _PROFILE_FIELDS - {"connection_mode"}


class ClientConnectionMode(str, Enum):
    """The sole connection mode supported by the ordinary Relay client."""

    GENERAL_API = "general_api"


def default_client_profile_path() -> str:
    return str(Path(default_client_data_dir()) / "bridge-profile.json")


def _safe_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError("%s is invalid" % name)
    return value


def _reference(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_REFERENCE.fullmatch(value):
        raise ValidationError("%s credential reference is invalid" % name)
    return value


def _timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("%s is invalid" % name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError("%s is invalid" % name) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("%s must be timezone-aware" % name)
    return parsed


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _config_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class LocalBridgeProfile:
    profile_id: str
    relay_base_url: str
    relay_credential_reference: str
    selection: PremiumModelSelection
    premium_credential_reference: str
    default_mode: RelayBillingMode
    memory_budget_tokens: int
    created_at: datetime
    updated_at: datetime
    connection_mode: ClientConnectionMode = ClientConnectionMode.GENERAL_API

    @property
    def local_api_credential_reference(self) -> str:
        """Return the device-local bearer credential reference.

        This key authenticates applications on this Mac to the loopback API.
        It is deliberately separate from the Relay service credential.
        """

        return "%s/local-api" % self.profile_id

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _safe_id("profile id", self.profile_id))
        object.__setattr__(
            self, "relay_base_url", normalize_relay_base_url(self.relay_base_url)
        )
        if not isinstance(self.connection_mode, ClientConnectionMode):
            raise ValidationError("client connection mode is invalid")
        object.__setattr__(
            self,
            "relay_credential_reference",
            _reference("Relay", self.relay_credential_reference),
        )
        if not isinstance(self.selection, PremiumModelSelection):
            raise ValidationError("premium model selection is invalid")
        object.__setattr__(
            self,
            "premium_credential_reference",
            _reference("premium", self.premium_credential_reference),
        )
        if not isinstance(self.default_mode, RelayBillingMode):
            raise ValidationError("default Relay mode is invalid")
        if (
            isinstance(self.memory_budget_tokens, bool)
            or not isinstance(self.memory_budget_tokens, int)
            or not (1 <= self.memory_budget_tokens <= 8_000)
        ):
            raise ValidationError("memory budget is invalid")
        for value, name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValidationError("%s must be timezone-aware" % name)
        if self.updated_at < self.created_at:
            raise ValidationError("profile update time precedes creation")

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        relay_base_url: str,
        selection: PremiumModelSelection,
        connection_mode: ClientConnectionMode = ClientConnectionMode.GENERAL_API,
        default_mode: RelayBillingMode,
        memory_budget_tokens: int = 800,
        now: Optional[datetime] = None,
    ) -> "LocalBridgeProfile":
        occurred = now or datetime.now(timezone.utc)
        safe_profile_id = _safe_id("profile id", profile_id)
        return cls(
            profile_id=safe_profile_id,
            relay_base_url=relay_base_url,
            relay_credential_reference="%s/relay" % safe_profile_id,
            connection_mode=connection_mode,
            selection=selection,
            premium_credential_reference="%s/premium" % safe_profile_id,
            default_mode=default_mode,
            memory_budget_tokens=memory_budget_tokens,
            created_at=occurred,
            updated_at=occurred,
        )

    def _unsigned_payload(self) -> Dict[str, object]:
        return {
            "version": CLIENT_PROFILE_VERSION,
            "profile_id": self.profile_id,
            "relay_base_url": self.relay_base_url,
            "connection_mode": self.connection_mode.value,
            "relay_credential_reference": self.relay_credential_reference,
            "premium_provider": self.selection.provider_id,
            "premium_model": self.selection.model_id,
            "custom_base_url": self.selection.custom_base_url,
            "premium_credential_reference": self.premium_credential_reference,
            "default_mode": self.default_mode.value,
            "memory_budget_tokens": self.memory_budget_tokens,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "updated_at": self.updated_at.astimezone(timezone.utc).isoformat(),
        }

    def public_payload(self) -> Dict[str, object]:
        payload = self._unsigned_payload()
        payload["config_hash"] = _config_hash(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "LocalBridgeProfile":
        if not isinstance(payload, Mapping) or set(payload) not in {
            _PROFILE_FIELDS,
            _LEGACY_PROFILE_FIELDS,
        }:
            raise ValidationError("Bridge profile schema is invalid")
        version = payload.get("version")
        if version not in {CLIENT_PROFILE_VERSION, _LEGACY_CLIENT_PROFILE_VERSION}:
            raise ValidationError("Bridge profile version is unsupported")
        unsigned = {key: payload[key] for key in payload if key != "config_hash"}
        config_hash = payload.get("config_hash")
        if (
            not isinstance(config_hash, str)
            or len(config_hash) != 64
            or config_hash != _config_hash(unsigned)
        ):
            raise IntegrityConflict("Bridge profile integrity check failed")
        try:
            mode = RelayBillingMode(str(payload["default_mode"]))
        except ValueError:
            raise ValidationError("default Relay mode is invalid") from None
        try:
            stored_connection_mode = (
                ClientConnectionMode.GENERAL_API.value
                if version == _LEGACY_CLIENT_PROFILE_VERSION
                else str(payload["connection_mode"])
            )
            connection_mode = ClientConnectionMode(stored_connection_mode)
        except ValueError:
            raise ValidationError("client connection mode is invalid") from None
        selection_payload = {
            "provider": payload["premium_provider"],
            "model": payload["premium_model"],
        }
        if payload["custom_base_url"] is not None:
            selection_payload["custom_base_url"] = payload["custom_base_url"]
        return cls(
            profile_id=_safe_id("profile id", payload["profile_id"]),
            relay_base_url=str(payload["relay_base_url"]),
            relay_credential_reference=_reference(
                "Relay", payload["relay_credential_reference"]
            ),
            connection_mode=connection_mode,
            selection=PremiumModelSelection.from_payload(selection_payload),
            premium_credential_reference=_reference(
                "premium", payload["premium_credential_reference"]
            ),
            default_mode=mode,
            memory_budget_tokens=payload["memory_budget_tokens"],
            created_at=_timestamp("created_at", payload["created_at"]),
            updated_at=_timestamp("updated_at", payload["updated_at"]),
        )


class LocalBridgeProfileStore:
    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValidationError("Bridge profile path is invalid")
        candidate = Path(path).expanduser()
        if candidate.exists() and candidate.is_symlink():
            raise ValidationError("Bridge profile must not be a symbolic link")
        self.path = str(candidate.resolve())

    def save(self, profile: LocalBridgeProfile) -> None:
        if not isinstance(profile, LocalBridgeProfile):
            raise ValidationError("Bridge profile is invalid")
        target = Path(self.path)
        if target.exists() and target.is_symlink():
            raise ValidationError("Bridge profile must not be a symbolic link")
        parent_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and parent_existed and target.parent.stat().st_mode & 0o077:
            raise ValidationError("Bridge profile directory must be owner-only")
        if os.name == "posix":
            os.chmod(str(target.parent), 0o700)
        encoded = _canonical(profile.public_payload()) + b"\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".%s." % target.name,
            dir=str(target.parent),
        )
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(target))
            if os.name == "posix":
                os.chmod(str(target), 0o600)
                directory = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self) -> LocalBridgeProfile:
        target = Path(self.path)
        if not target.is_file() or target.is_symlink():
            raise ValidationError("Bridge profile is unavailable")
        if os.name == "posix" and target.stat().st_mode & 0o077:
            raise ValidationError("Bridge profile permissions must be owner-only")
        try:
            encoded = target.read_bytes()
        except OSError:
            raise ValidationError("Bridge profile is unavailable") from None
        if len(encoded) > _MAX_PROFILE_BYTES:
            raise ValidationError("Bridge profile exceeds the size limit")
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValidationError("Bridge profile JSON is invalid") from None
        return LocalBridgeProfile.from_payload(payload)

    def delete(self) -> None:
        target = Path(self.path)
        if not target.exists():
            return
        if target.is_symlink() or not target.is_file():
            raise ValidationError("Bridge profile path is unsafe")
        if os.name == "posix" and target.stat().st_mode & 0o077:
            raise ValidationError("Bridge profile permissions must be owner-only")
        target.unlink()
