"""Safe OTA discovery and user-approved download for Longweis Relay on macOS.

The updater never receives a Relay credential and never installs an app in the
background.  It fetches a small HTTPS manifest from the official download
origin, verifies a pinned-origin artifact hash after download, then opens the
DMG only after the user chooses to do so.
"""

import hashlib
import json
import os
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .client_errors import ProviderResponseError, ValidationError
from .client_platform import default_client_runtime_dir


OTA_UPDATE_VERSION = "P0.70"
OTA_MANIFEST_URL = "https://relay.longweisos.com/releases/macos/manifest.json"
_OFFICIAL_HOST = "relay.longweisos.com"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_CHECK_INTERVAL = timedelta(hours=12)
_REMIND_INTERVAL = timedelta(days=1)
_VERSION = re.compile(
    r"^(?P<edition>P)?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)(?:\.(?P<patch>0|[1-9][0-9]*))?$"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
ManifestTransport = Callable[[urllib.request.Request, float], Tuple[int, bytes]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_version(value: object) -> Tuple[str, int, int, int]:
    if not isinstance(value, str):
        raise ValidationError("update version is invalid")
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValidationError("update version is invalid")
    return (
        match.group("edition") or "",
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
    )


def _require_official_url(value: object, *, artifact: bool) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ProviderResponseError("Relay update returned an invalid URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != _OFFICIAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/releases/macos/")
        or (artifact and not parsed.path.endswith(".dmg"))
    ):
        raise ProviderResponseError("Relay update returned an invalid URL")
    return value


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _manifest_transport(request: urllib.request.Request, timeout: float) -> Tuple[int, bytes]:
    try:
        opener = urllib.request.build_opener(
            _NoRedirects(), urllib.request.HTTPSHandler(context=_tls_context())
        )
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(_MAX_MANIFEST_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAX_MANIFEST_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderResponseError("Relay update service is unavailable") from None


@dataclass(frozen=True)
class MacOSUpdate:
    version: str
    build: str
    artifact_url: str
    sha256: str
    size_bytes: int
    release_notes: str

    @property
    def filename(self) -> str:
        return "Longweis-Relay-macOS-v%s-arm64.dmg" % self.version


@dataclass(frozen=True)
class UpdatePreferences:
    automatic_checks: bool = True
    automatic_downloads: bool = False
    last_checked_at: Optional[str] = None
    remind_after: Optional[str] = None
    skipped_version: Optional[str] = None


class UpdatePreferencesStore:
    """Owner-only update preferences; no credentials or task data live here."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or Path(default_client_runtime_dir()) / "update-preferences.json")

    @staticmethod
    def _time(value: object) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 64:
            raise ValidationError("update preference time is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError("update preference time is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError("update preference time is invalid")
        return parsed.astimezone(timezone.utc).isoformat()

    def load(self) -> UpdatePreferences:
        if not self.path.exists():
            return UpdatePreferences()
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValidationError("update preferences are unsafe")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise ValidationError("update preferences are invalid") from None
        if not isinstance(payload, Mapping) or set(payload) != {
            "automatic_checks", "automatic_downloads", "last_checked_at", "remind_after", "skipped_version"
        }:
            raise ValidationError("update preferences are invalid")
        skipped = payload["skipped_version"]
        if skipped is not None:
            _parse_version(skipped)
        if not isinstance(payload["automatic_checks"], bool) or not isinstance(payload["automatic_downloads"], bool):
            raise ValidationError("update preferences are invalid")
        return UpdatePreferences(
            automatic_checks=payload["automatic_checks"],
            automatic_downloads=payload["automatic_downloads"],
            last_checked_at=self._time(payload["last_checked_at"]),
            remind_after=self._time(payload["remind_after"]),
            skipped_version=skipped,
        )

    def save(self, preferences: UpdatePreferences) -> None:
        if not isinstance(preferences, UpdatePreferences):
            raise ValidationError("update preferences are invalid")
        root = self.path.parent
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise ValidationError("update preference directory is unsafe")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        payload = {
            "automatic_checks": preferences.automatic_checks,
            "automatic_downloads": preferences.automatic_downloads,
            "last_checked_at": self._time(preferences.last_checked_at),
            "remind_after": self._time(preferences.remind_after),
            "skipped_version": preferences.skipped_version,
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=str(root))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class MacOSUpdateClient:
    """Check and download an official macOS update without silent installation."""

    def __init__(
        self,
        current_version: str,
        manifest_url: str = OTA_MANIFEST_URL,
        transport: Optional[ManifestTransport] = None,
        timeout_seconds: float = 20.0,
        updates_dir: Optional[str] = None,
    ) -> None:
        _parse_version(current_version)
        _require_official_url(manifest_url, artifact=False)
        if not (0.1 <= float(timeout_seconds) <= 120.0):
            raise ValidationError("update timeout is invalid")
        self.current_version = current_version
        self.manifest_url = manifest_url
        self.transport = transport or _manifest_transport
        self.timeout_seconds = float(timeout_seconds)
        self.updates_dir = Path(updates_dir or Path(default_client_runtime_dir()) / "updates")

    def check(self) -> Optional[MacOSUpdate]:
        request = urllib.request.Request(
            self.manifest_url,
            headers={"Accept": "application/json", "User-Agent": "longweis-relay-update/%s" % OTA_UPDATE_VERSION},
            method="GET",
        )
        try:
            status, body = self.transport(request, self.timeout_seconds)
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("Relay update service is unavailable") from None
        if status != 200 or len(body) > _MAX_MANIFEST_BYTES:
            raise ProviderResponseError("Relay update manifest is unavailable")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderResponseError("Relay update manifest is invalid") from None
        if not isinstance(payload, Mapping) or payload.get("object") != "longweis_relay.macos_update":
            raise ProviderResponseError("Relay update manifest is invalid")
        if payload.get("schema_version") != 1 or payload.get("channel") != "stable" or payload.get("architecture") != "arm64":
            raise ProviderResponseError("Relay update manifest is invalid")
        artifact = payload.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ProviderResponseError("Relay update manifest is invalid")
        version = payload.get("version")
        build = payload.get("build")
        release_notes = payload.get("release_notes", "")
        size = artifact.get("size_bytes")
        sha256 = artifact.get("sha256")
        if (
            not isinstance(build, str)
            or not build.isdigit()
            or not isinstance(release_notes, str)
            or len(release_notes) > 4000
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not (1 <= size <= _MAX_ARTIFACT_BYTES)
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise ProviderResponseError("Relay update manifest is invalid")
        update = MacOSUpdate(
            version=str(version),
            build=build,
            artifact_url=_require_official_url(artifact.get("url"), artifact=True),
            sha256=sha256,
            size_bytes=size,
            release_notes=release_notes,
        )
        candidate = _parse_version(update.version)
        current = _parse_version(self.current_version)
        # Product lines must not update one another.  In particular, an old
        # non-prefixed release cannot silently replace the ordinary P edition.
        if candidate[0] != current[0]:
            return None
        return update if candidate[1:] > current[1:] else None

    def download(self, update: MacOSUpdate) -> Path:
        if not isinstance(update, MacOSUpdate):
            raise ValidationError("update is invalid")
        self.updates_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.updates_dir.is_symlink() or not self.updates_dir.is_dir():
            raise ValidationError("update directory is unsafe")
        os.chmod(self.updates_dir, 0o700)
        target = self.updates_dir / update.filename
        if target.exists() and target.is_file() and not target.is_symlink() and self._sha256(target) == update.sha256:
            return target
        temporary = self.updates_dir / (".download-" + update.version + ".dmg")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink(missing_ok=True)
        try:
            opener = urllib.request.build_opener(
                _NoRedirects(), urllib.request.HTTPSHandler(context=_tls_context())
            )
            request = urllib.request.Request(
                update.artifact_url,
                headers={"Accept": "application/x-apple-diskimage", "User-Agent": "longweis-relay-update/%s" % OTA_UPDATE_VERSION},
                method="GET",
            )
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise ProviderResponseError("Relay update download is unavailable")
                digest = hashlib.sha256()
                size = 0
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > update.size_bytes or size > _MAX_ARTIFACT_BYTES:
                            raise ProviderResponseError("Relay update download exceeded its limit")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        except ProviderResponseError:
            temporary.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            temporary.unlink(missing_ok=True)
            raise ProviderResponseError("Relay update download is unavailable") from None
        if size != update.size_bytes or digest.hexdigest() != update.sha256:
            temporary.unlink(missing_ok=True)
            raise ProviderResponseError("Relay update integrity check failed")
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def open_installer(path: Path) -> None:
        if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
            raise ValidationError("downloaded update is unavailable")
        try:
            subprocess.run(["/usr/bin/open", str(path)], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise ProviderResponseError("could not open the verified update") from None


def checked_preferences(preferences: UpdatePreferences, now: Optional[datetime] = None) -> UpdatePreferences:
    return replace(preferences, last_checked_at=(now or _utc_now()).astimezone(timezone.utc).isoformat())


def remind_tomorrow(preferences: UpdatePreferences, now: Optional[datetime] = None) -> UpdatePreferences:
    return replace(preferences, remind_after=((now or _utc_now()) + _REMIND_INTERVAL).astimezone(timezone.utc).isoformat())


def should_check_automatically(preferences: UpdatePreferences, now: Optional[datetime] = None) -> bool:
    if not preferences.automatic_checks:
        return False
    if preferences.last_checked_at is None:
        return True
    try:
        checked = datetime.fromisoformat(preferences.last_checked_at)
    except ValueError:
        return True
    return (now or _utc_now()) >= checked + _CHECK_INTERVAL


def should_notify_about(update: MacOSUpdate, preferences: UpdatePreferences, now: Optional[datetime] = None) -> bool:
    if preferences.skipped_version == update.version:
        return False
    if preferences.remind_after is None:
        return True
    try:
        after = datetime.fromisoformat(preferences.remind_after)
    except ValueError:
        return True
    return (now or _utc_now()) >= after
