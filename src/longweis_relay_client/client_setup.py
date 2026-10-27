"""Safe local settings and model discovery for the thin Relay client."""

import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from .client_modes import RelayBillingMode
from .client_model_selection import (
    DiscoveredModel,
    PremiumModelSelection,
    assess_cost_fit,
    model_catalog_request,
    parse_model_catalog,
)
from .client_profile import (
    ClientConnectionMode,
    LocalBridgeProfile,
    LocalBridgeProfileStore,
)
from .client_errors import ProviderResponseError, ValidationError
from .client_credentials import default_credential_store


CLIENT_SETUP_VERSION = "0.65.28"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024
_SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
)
_SUPPORTED_PROVIDERS = (
    ("openai", "OpenAI"),
    ("openrouter", "OpenRouter"),
    ("custom-openai-compatible", "Custom OpenAI-compatible API"),
)
CatalogTransport = Callable[[urllib.request.Request, float], Tuple[int, bytes]]


class _NoProviderRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent an API credential from being forwarded to a redirected origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _provider_tls_context() -> ssl.SSLContext:
    """Load platform trust even when a relocated Python has no CA path."""

    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context
    for candidate in _SYSTEM_CA_FILES:
        if Path(candidate).is_file():
            try:
                return ssl.create_default_context(cafile=candidate)
            except (OSError, ssl.SSLError):
                continue
    return context


def _catalog_transport(
    request: urllib.request.Request, timeout_seconds: float
) -> Tuple[int, bytes]:
    try:
        opener = urllib.request.build_opener(
            _NoProviderRedirects(),
            urllib.request.HTTPSHandler(context=_provider_tls_context()),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(_MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAX_CATALOG_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderResponseError("premium provider model discovery failed") from None


def supported_client_providers() -> Tuple[Mapping[str, str], ...]:
    return tuple(
        {"id": provider_id, "label": label}
        for provider_id, label in _SUPPORTED_PROVIDERS
    )


def _provider_supported(provider_id: object) -> str:
    supported = {item[0] for item in _SUPPORTED_PROVIDERS}
    if not isinstance(provider_id, str) or provider_id not in supported:
        raise ValidationError("this local client does not support that provider")
    return provider_id


class LocalModelCatalogClient:
    """Discovers models directly from a premium provider, never through Relay."""

    def __init__(
        self,
        transport: Optional[CatalogTransport] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not (0.1 <= float(timeout_seconds) <= 120.0)
        ):
            raise ValidationError("model discovery timeout is invalid")
        self._transport = transport or _catalog_transport
        self.timeout_seconds = float(timeout_seconds)

    def discover(
        self,
        provider_id: str,
        premium_key: str,
        custom_base_url: Optional[str] = None,
    ) -> Tuple[DiscoveredModel, ...]:
        provider = _provider_supported(provider_id)
        request = model_catalog_request(provider, premium_key, custom_base_url)
        try:
            status, body = self._transport(request, self.timeout_seconds)
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError(
                "premium provider model discovery failed"
            ) from None
        if status < 200 or status >= 300:
            raise ProviderResponseError(
                "premium provider model discovery returned HTTP %d" % status
            )
        if len(body) > _MAX_CATALOG_BYTES:
            raise ProviderResponseError(
                "premium provider model catalog exceeded the size limit"
            )
        return parse_model_catalog(provider, body)


@dataclass(frozen=True)
class LocalSettingsSnapshot:
    configured: bool
    profile_path: str
    profile_id: str
    relay_base_url: str
    connection_mode: ClientConnectionMode
    provider_id: str
    model_id: str
    custom_base_url: Optional[str]
    memory_retrieval_enabled: bool
    memory_budget_tokens: int
    keychain_available: bool
    relay_key_available: bool
    premium_key_available: bool
    local_api_key_available: bool

    @property
    def ready(self) -> bool:
        premium_ready = (
            self.premium_key_available
            if self.connection_mode == ClientConnectionMode.GENERAL_API
            else True
        )
        return bool(
            self.configured
            and self.keychain_available
            and self.relay_key_available
            and premium_ready
            and self.local_api_key_available
        )

    def public_payload(self) -> Dict[str, object]:
        return {
            "client_setup_version": CLIENT_SETUP_VERSION,
            "configured": self.configured,
            "ready": self.ready,
            "profile_path": self.profile_path,
            "profile_id": self.profile_id,
            "relay_base_url": self.relay_base_url,
            "connection_mode": self.connection_mode.value,
            "premium": {
                "provider": self.provider_id,
                "model": self.model_id,
                "custom_base_url": self.custom_base_url,
            },
            "memory_retrieval_enabled": self.memory_retrieval_enabled,
            "memory_budget_tokens": self.memory_budget_tokens,
            "keychain_available": self.keychain_available,
            "relay_key_available": self.relay_key_available,
            "premium_key_available": self.premium_key_available,
            "local_api_key_available": self.local_api_key_available,
            "credentials_in_output": False,
            "network_calls": 0,
        }


class LocalBridgeSettingsService:
    """Owns the non-secret local setup workflow used by a desktop UI."""

    def __init__(
        self,
        profile_store: LocalBridgeProfileStore,
        credential_store: Optional[object] = None,
        catalog_client: Optional[LocalModelCatalogClient] = None,
    ) -> None:
        if not isinstance(profile_store, LocalBridgeProfileStore):
            raise ValidationError("Bridge profile store is invalid")
        self.profile_store = profile_store
        self.credential_store = (
            credential_store or default_credential_store()
        )
        self.catalog_client = catalog_client or LocalModelCatalogClient()

    def __repr__(self) -> str:
        return "LocalBridgeSettingsService(profile_path=%r)" % (
            self.profile_store.path,
        )

    def _existing_profile(self) -> Optional[LocalBridgeProfile]:
        if not Path(self.profile_store.path).exists():
            return None
        return self.profile_store.load()

    def snapshot(self) -> LocalSettingsSnapshot:
        profile = self._existing_profile()
        if profile is None:
            return LocalSettingsSnapshot(
                configured=False,
                profile_path=self.profile_store.path,
                profile_id="default",
                relay_base_url="https://api.longweisos.com/v1",
                connection_mode=ClientConnectionMode.GENERAL_API,
                provider_id="openrouter",
                model_id="",
                custom_base_url=None,
                memory_retrieval_enabled=False,
                memory_budget_tokens=800,
                keychain_available=bool(self.credential_store.available),
                relay_key_available=False,
                premium_key_available=False,
                local_api_key_available=False,
            )
        provider_supported = True
        try:
            _provider_supported(profile.selection.provider_id)
        except ValidationError:
            provider_supported = False
        return LocalSettingsSnapshot(
            configured=provider_supported,
            profile_path=self.profile_store.path,
            profile_id=profile.profile_id,
            relay_base_url=profile.relay_base_url,
            connection_mode=profile.connection_mode,
            provider_id=profile.selection.provider_id,
            model_id=profile.selection.model_id,
            custom_base_url=profile.selection.custom_base_url,
            memory_retrieval_enabled=(
                profile.default_mode == RelayBillingMode.MEMORY_RETRIEVAL
            ),
            memory_budget_tokens=profile.memory_budget_tokens,
            keychain_available=bool(self.credential_store.available),
            relay_key_available=self.credential_store.contains(
                profile.relay_credential_reference
            ),
            premium_key_available=self.credential_store.contains(
                profile.premium_credential_reference
            ),
            local_api_key_available=self.credential_store.contains(
                profile.local_api_credential_reference
            ),
        )

    def save(
        self,
        *,
        profile_id: str,
        relay_base_url: str,
        connection_mode: str = ClientConnectionMode.GENERAL_API.value,
        provider_id: str,
        model_id: str,
        custom_base_url: Optional[str],
        memory_retrieval_enabled: bool,
        memory_budget_tokens: int,
        relay_key: Optional[str] = None,
        premium_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if not self.credential_store.available:
            raise ValidationError("the operating system secure credential store is unavailable")
        try:
            selected_connection_mode = ClientConnectionMode(connection_mode)
        except ValueError:
            raise ValidationError("client connection mode is invalid") from None
        provider = _provider_supported(provider_id)
        selection_payload: Dict[str, object] = {
            "provider": provider,
            "model": model_id,
        }
        if provider == "custom-openai-compatible":
            selection_payload["custom_base_url"] = custom_base_url
        selection = PremiumModelSelection.from_payload(selection_payload)
        if not isinstance(memory_retrieval_enabled, bool):
            raise ValidationError("memory retrieval selection is invalid")
        mode = (
            RelayBillingMode.MEMORY_RETRIEVAL
            if memory_retrieval_enabled
            else RelayBillingMode.EXECUTION_OPTIMIZATION
        )
        occurred = now or datetime.now(timezone.utc)
        candidate = LocalBridgeProfile.create(
            profile_id=profile_id,
            relay_base_url=relay_base_url,
            selection=selection,
            connection_mode=selected_connection_mode,
            default_mode=mode,
            memory_budget_tokens=memory_budget_tokens,
            now=occurred,
        )
        existing = self._existing_profile()
        if existing is not None and existing.profile_id == candidate.profile_id:
            candidate = LocalBridgeProfile(
                profile_id=candidate.profile_id,
                relay_base_url=candidate.relay_base_url,
                relay_credential_reference=existing.relay_credential_reference,
                connection_mode=candidate.connection_mode,
                selection=candidate.selection,
                premium_credential_reference=existing.premium_credential_reference,
                default_mode=candidate.default_mode,
                memory_budget_tokens=candidate.memory_budget_tokens,
                created_at=existing.created_at,
                updated_at=occurred,
            )
        relay_secret = relay_key.strip() if isinstance(relay_key, str) else ""
        premium_secret = (
            premium_key.strip() if isinstance(premium_key, str) else ""
        )
        relay_available = bool(relay_secret) or self.credential_store.contains(
            candidate.relay_credential_reference
        )
        premium_available = bool(
            premium_secret
        ) or self.credential_store.contains(candidate.premium_credential_reference)
        if (
            selected_connection_mode == ClientConnectionMode.GENERAL_API
            and not premium_available
        ):
            raise ValidationError(
                "the premium provider credential is required for the first saved setup"
            )
        if not relay_available:
            raise ValidationError(
                "sign in or register before saving the first setup"
            )
        if relay_secret:
            self.credential_store.store(
                candidate.relay_credential_reference, relay_secret
            )
        if premium_secret:
            self.credential_store.store(
                candidate.premium_credential_reference, premium_secret
            )
        local_api_key_created = not self.credential_store.contains(
            candidate.local_api_credential_reference
        )
        if local_api_key_created:
            self.credential_store.store(
                candidate.local_api_credential_reference,
                "lsk_" + secrets.token_urlsafe(32),
            )
        self.profile_store.save(candidate)
        return {
            "status": "configured",
            "client_setup_version": CLIENT_SETUP_VERSION,
            "profile_path": self.profile_store.path,
            "profile_id": candidate.profile_id,
            "relay_base_url": candidate.relay_base_url,
            "connection_mode": candidate.connection_mode.value,
            "premium": {
                "provider": candidate.selection.provider_id,
                "model": candidate.selection.model_id,
                "custom_base_url": candidate.selection.custom_base_url,
            },
            "memory_retrieval_enabled": memory_retrieval_enabled,
            "memory_budget_tokens": candidate.memory_budget_tokens,
            "cost_fit": (
                assess_cost_fit(candidate.selection).public_payload()
                if candidate.connection_mode == ClientConnectionMode.GENERAL_API
                else None
            ),
            "credential_storage": str(
                getattr(self.credential_store, "storage_id", "secure_store")
            ),
            "relay_key_replaced": bool(relay_secret),
            "premium_key_replaced": bool(premium_secret),
            "local_api_key_created": local_api_key_created,
            "credentials_in_profile": False,
            "credentials_in_output": False,
            "network_calls": 0,
        }

    def read_local_api_key(self) -> str:
        """Read the device-only API key for an explicit user copy action."""

        profile = self._existing_profile()
        if profile is None:
            raise ValidationError("save settings before copying the fused API key")
        if not self.credential_store.contains(profile.local_api_credential_reference):
            raise ValidationError("save settings before copying the fused API key")
        return self.credential_store.read(profile.local_api_credential_reference)

    def discover_models(
        self,
        *,
        provider_id: str,
        custom_base_url: Optional[str],
        premium_key: Optional[str] = None,
    ) -> Tuple[DiscoveredModel, ...]:
        provider = _provider_supported(provider_id)
        secret = premium_key.strip() if isinstance(premium_key, str) else ""
        if not secret:
            existing = self._existing_profile()
            if (
                existing is None
                or existing.selection.provider_id != provider
                or existing.selection.custom_base_url != (
                    custom_base_url if provider == "custom-openai-compatible" else None
                )
            ):
                raise ValidationError(
                    "enter the premium provider key before finding models"
                )
            secret = self.credential_store.read(
                existing.premium_credential_reference
            )
        return self.catalog_client.discover(provider, secret, custom_base_url)

    def reset(self) -> Dict[str, object]:
        profile = self._existing_profile()
        if profile is None:
            return {
                "status": "already_removed",
                "credentials_removed": 0,
                "profile_removed": False,
            }
        remover = getattr(self.credential_store, "delete", None)
        if not callable(remover):
            raise ValidationError("the operating system credential store cannot remove keys")
        removed = 0
        for reference in (
            profile.relay_credential_reference,
            profile.premium_credential_reference,
            profile.local_api_credential_reference,
        ):
            remover(reference)
            removed += 1
        self.profile_store.delete()
        return {
            "status": "removed",
            "credentials_removed": removed,
            "profile_removed": True,
        }
