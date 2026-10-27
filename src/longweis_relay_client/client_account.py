"""Client-only Relay balance and Square-hosted top-up workflow."""

import json
import ssl
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .client_endpoint import normalize_relay_base_url
from .client_errors import ProviderResponseError, ValidationError


CLIENT_ACCOUNT_VERSION = "0.65.30"
CLIENT_PRODUCT_VERSION = "P0.70"
TOPUP_AMOUNTS_USD = ("10.00", "25.00", "50.00", "100.00")
_MAXIMUM_RESPONSE_BYTES = 128 * 1024
_SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
)
AccountTransport = Callable[[urllib.request.Request, float], Tuple[int, bytes]]


class _NoRelayRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _tls_context() -> ssl.SSLContext:
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


def _transport(
    request: urllib.request.Request, timeout_seconds: float
) -> Tuple[int, bytes]:
    try:
        opener = urllib.request.build_opener(
            _NoRelayRedirects(),
            urllib.request.HTTPSHandler(context=_tls_context()),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(_MAXIMUM_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAXIMUM_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderResponseError("Relay account service is unavailable") from None


def _money(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderResponseError("Relay account returned an invalid balance")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ProviderResponseError("Relay account returned an invalid balance") from None
    if not amount.is_finite():
        raise ProviderResponseError("Relay account returned an invalid balance")
    return format(amount, "f")


def _checkout_url(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ProviderResponseError("Relay top-up returned an invalid checkout link")
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    square_owned = (
        hostname == "square.link"
        or hostname.endswith(".square.link")
        or hostname.endswith(".square.site")
        or hostname.endswith(".squareupsandbox.com")
    )
    if (
        parsed.scheme != "https"
        or not square_owned
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProviderResponseError("Relay top-up returned an invalid checkout link")
    return value


class RelayAccountClient:
    """Read prepaid balance and open a Square checkout without exposing keys."""

    def __init__(
        self,
        relay_base_url: str,
        credential_reference: str,
        credential_reader: Callable[[str], str],
        transport: Optional[AccountTransport] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.relay_base_url = normalize_relay_base_url(relay_base_url)
        if not isinstance(credential_reference, str) or not credential_reference:
            raise ValidationError("Relay credential reference is invalid")
        if not callable(credential_reader):
            raise ValidationError("Relay credential reader is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not (0.1 <= float(timeout_seconds) <= 120.0)
        ):
            raise ValidationError("Relay account timeout is invalid")
        self.credential_reference = credential_reference
        self._credential_reader = credential_reader
        self._transport = transport or _transport
        self.timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "RelayAccountClient(relay_base_url=%r)" % self.relay_base_url

    def _request(
        self, path: str, method: str, payload: Optional[Mapping[str, object]] = None
    ) -> Mapping[str, object]:
        try:
            credential = self._credential_reader(self.credential_reference)
        except Exception:
            raise ValidationError("Relay activation is unavailable") from None
        if not isinstance(credential, str) or not credential:
            raise ValidationError("Relay activation is unavailable")
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + credential,
            "User-Agent": "longweis-relay-client/%s" % CLIENT_ACCOUNT_VERSION,
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
        try:
            status, response_body = self._transport(request, self.timeout_seconds)
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("Relay account service is unavailable") from None
        if len(response_body) > _MAXIMUM_RESPONSE_BYTES:
            raise ProviderResponseError("Relay account response exceeded the size limit")
        if status < 200 or status >= 300:
            raise ProviderResponseError(
                "Relay account request returned HTTP %d" % status,
                status_code=status,
            )
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderResponseError("Relay account returned invalid JSON") from None
        if not isinstance(decoded, Mapping):
            raise ProviderResponseError("Relay account returned an invalid response")
        return decoded

    def balance(self) -> Dict[str, object]:
        payload = self._request("/relay/balance", "GET")
        if payload.get("object") != "longweis_relay.balance":
            raise ProviderResponseError("Relay account returned an invalid balance")
        return {
            "available_usd": _money(payload.get("available_usd")),
            "settled_usd": _money(payload.get("settled_usd")),
            "recovery_due_usd": _money(payload.get("recovery_due_usd", "0")),
            "can_run_relay_work": bool(payload.get("can_run_relay_work")),
        }

    def pricing(self) -> Dict[str, Dict[str, str]]:
        """Read display-only rates from Relay; never calculate a customer bill."""

        payload = self._request("/relay/pricing", "GET")
        modes = payload.get("modes")
        if (
            payload.get("object") != "longweis_relay.pricing"
            or payload.get("currency") != "USD"
            or payload.get("unit") != "per_million_relay_tokens"
            or not isinstance(modes, Mapping)
        ):
            raise ProviderResponseError("Relay account returned invalid pricing")
        normalized = {}
        for mode in ("execution_optimization", "memory_retrieval"):
            rate = modes.get(mode)
            if not isinstance(rate, Mapping):
                raise ProviderResponseError("Relay account returned invalid pricing")
            normalized[mode] = {
                "input_usd": _money(rate.get("input_usd")),
                "output_usd": _money(rate.get("output_usd")),
            }
        return normalized

    def compute_usage(self) -> Dict[str, object]:
        """Read the server-measured cumulative compute comparison.

        The premium-model figure is a transparent public-rate reference, not
        a claim that a premium model was also run for the same request.  Relay
        remains the only provider name exposed by the desktop client.
        """

        payload = self._request("/relay/account", "GET")
        usage = payload.get("usage")
        summary = usage.get("summary") if isinstance(usage, Mapping) else None
        if (
            payload.get("object") != "longweis_relay.account_dashboard"
            or not isinstance(summary, Mapping)
        ):
            raise ProviderResponseError("Relay account returned invalid compute usage")

        def whole_number(name: str) -> int:
            value = summary.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderResponseError("Relay account returned invalid compute usage")
            return value

        hybrid = summary.get("hybrid_token_usage")
        if not isinstance(hybrid, Mapping):
            raise ProviderResponseError("Relay account returned invalid compute usage")
        def hybrid_tokens(name: str) -> int:
            value = hybrid.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderResponseError("Relay account returned invalid compute usage")
            return value
        percent = hybrid.get("relay_token_share_percent")
        if percent is not None:
            if not isinstance(percent, str):
                raise ProviderResponseError("Relay account returned invalid compute usage")
            try:
                percentage = Decimal(percent)
            except InvalidOperation:
                raise ProviderResponseError("Relay account returned invalid compute usage") from None
            if not percentage.is_finite() or percentage < Decimal("0") or percentage > Decimal("100"):
                raise ProviderResponseError("Relay account returned invalid compute usage")
            percent = format(percentage, ".2f")
        return {
            "settled_request_count": whole_number("settled_request_count"),
            "total_tokens": hybrid_tokens("total_tokens"),
            "premium_model_tokens": hybrid_tokens("premium_model_tokens"),
            "relay_tokens": hybrid_tokens("relay_tokens"),
            "relay_token_share_percent": percent,
            "measured_turn_count": hybrid_tokens("measured_turn_count"),
            "premium_usage_unavailable_turn_count": hybrid_tokens(
                "premium_usage_unavailable_turn_count"
            ),
        }

    def create_topup(self, amount_usd: str) -> Dict[str, object]:
        if amount_usd not in TOPUP_AMOUNTS_USD:
            raise ValidationError("Select an available Relay top-up amount")
        payload = self._request(
            "/relay/topups",
            "POST",
            {
                "request_id": "client-%s" % uuid.uuid4().hex,
                "amount_usd": amount_usd,
            },
        )
        if (
            payload.get("object") != "longweis_relay.square_topup"
            or payload.get("amount_usd") != amount_usd
            or payload.get("currency") != "USD"
        ):
            raise ProviderResponseError("Relay top-up returned an invalid response")
        checkout_url = _checkout_url(payload.get("checkout_url"))
        return {
            "amount_usd": amount_usd,
            "status": str(payload.get("status")),
            "checkout_url": checkout_url,
        }

    def submit_feedback(self, feedback: Mapping[str, object]) -> Dict[str, object]:
        """Submit a content-minimized tester report for this Relay account."""

        payload = self._request("/relay/feedback", "POST", feedback)
        record = payload.get("feedback")
        if (
            payload.get("object") != "longweis_relay.feedback_receipt"
            or not isinstance(record, Mapping)
            or not isinstance(record.get("feedback_id"), str)
        ):
            raise ProviderResponseError("Relay feedback returned an invalid response")
        return dict(record)
