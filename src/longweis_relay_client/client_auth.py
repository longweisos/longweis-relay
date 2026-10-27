"""Thin desktop client for Relay account registration and sign-in."""

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from .client_endpoint import normalize_relay_base_url
from .client_errors import ProviderResponseError, ValidationError


CLIENT_AUTH_VERSION = "0.1.0"
_MAXIMUM_RESPONSE_BYTES = 64 * 1024
_SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
)
AuthTransport = Callable[[urllib.request.Request, float], Tuple[int, bytes]]


class _NoAuthRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward verification codes or OAuth poll tokens to another URL."""

    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _tls_context() -> ssl.SSLContext:
    """Load the platform CA file when the bundled Python has no trust store."""
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


def _transport(request: urllib.request.Request, timeout_seconds: float) -> Tuple[int, bytes]:
    try:
        opener = urllib.request.build_opener(
            _NoAuthRedirects(),
            urllib.request.HTTPSHandler(context=_tls_context())
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(_MAXIMUM_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAXIMUM_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError):
        raise ProviderResponseError("Relay sign-in is unavailable") from None


class RelayAuthClient:
    def __init__(
        self,
        relay_base_url: str,
        transport: Optional[AuthTransport] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.relay_base_url = normalize_relay_base_url(relay_base_url)
        self._transport = transport or _transport
        self.timeout_seconds = float(timeout_seconds)

    def _request(
        self, path: str, method: str, payload: Optional[Mapping[str, object]] = None
    ) -> Mapping[str, object]:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "longweis-relay-client/%s" % CLIENT_AUTH_VERSION,
        }
        if payload is not None:
            body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.relay_base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        status, response_body = self._transport(request, self.timeout_seconds)
        if len(response_body) > _MAXIMUM_RESPONSE_BYTES:
            raise ProviderResponseError("Relay sign-in response is too large")
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderResponseError("Relay sign-in returned an invalid response") from None
        if status < 200 or status >= 300 or not isinstance(decoded, Mapping):
            raise ProviderResponseError("Registration or sign-in could not be completed")
        return decoded

    def methods(self) -> Dict[str, object]:
        payload = self._request("/auth/methods", "GET")
        if payload.get("object") != "longweis_relay.auth_methods":
            raise ProviderResponseError("Relay sign-in returned invalid methods")
        return dict(payload)

    def begin_email(self, email: str) -> Dict[str, object]:
        payload = self._request("/auth/email/start", "POST", {"email": email})
        if payload.get("object") != "longweis_relay.email_challenge":
            raise ProviderResponseError("Relay sign-in returned an invalid challenge")
        return dict(payload)

    def complete_email(self, challenge_id: str, verification_code: str) -> Dict[str, object]:
        payload = self._request(
            "/auth/email/complete",
            "POST",
            {"challenge_id": challenge_id, "verification_code": verification_code},
        )
        if payload.get("object") == "longweis_relay.email_activation_required":
            raise ProviderResponseError(
                "Relay unexpectedly requested restricted registration access"
            )
        return self._account_session(payload)

    @staticmethod
    def _account_session(payload: Mapping[str, object]) -> Dict[str, object]:
        relay_key = payload.get("relay_key")
        if (
            payload.get("object") != "longweis_relay.account_activation"
            or not isinstance(relay_key, str)
            or not relay_key.startswith("psk_test_")
        ):
            raise ProviderResponseError("Relay sign-in returned an invalid account session")
        return dict(payload)

    def begin_oauth(self, provider: str) -> Dict[str, object]:
        payload = self._request("/auth/oauth/start", "POST", {"provider": provider})
        if (
            payload.get("object") != "longweis_relay.oauth_flow"
            or payload.get("provider") != provider
            or not isinstance(payload.get("flow_id"), str)
            or not isinstance(payload.get("poll_token"), str)
            or not isinstance(payload.get("authorization_url"), str)
        ):
            raise ProviderResponseError("Relay sign-in returned an invalid authorization")
        return dict(payload)

    def poll_oauth(self, flow_id: str, poll_token: str) -> Dict[str, object]:
        payload = self._request(
            "/auth/oauth/poll",
            "POST",
            {"flow_id": flow_id, "poll_token": poll_token},
        )
        if payload.get("object") == "longweis_relay.oauth_poll":
            if payload.get("status") == "activation_required":
                raise ProviderResponseError(
                    "Relay unexpectedly requested restricted registration access"
                )
            if payload.get("status") != "pending":
                raise ProviderResponseError("Relay sign-in returned an invalid status")
            return dict(payload)
        return self._account_session(payload)
