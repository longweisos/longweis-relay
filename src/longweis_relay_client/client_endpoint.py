"""Safe Relay endpoint normalization shared by the local client modules.

The published client intentionally has no arbitrary remote Relay setting.  A
Relay account credential may be sent only to the production Relay origin.  A
literal loopback origin remains available for offline tests and local service
development.
"""

from urllib.parse import urlsplit, urlunsplit

from .client_errors import ValidationError


def normalize_relay_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Relay base URL is invalid")
    parsed = urlsplit(value.strip())
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "::1",
    }
    production = (
        parsed.scheme == "https"
        and parsed.hostname == "api.longweisos.com"
        and parsed.port in (None, 443)
    )
    if (
        (not production and not loopback_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "Relay base URL must use api.longweisos.com or literal loopback HTTP"
        )
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ValidationError("Relay base URL path must be /v1")
    path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
