"""Human-readable egress inventory for the auditable local client."""

from typing import Dict, Tuple

from .client_setup import supported_client_providers


def egress_inventory() -> Tuple[Dict[str, object], ...]:
    """Return coarse destinations without credentials or private service data."""

    providers = tuple(item["label"] for item in supported_client_providers())
    return (
        {
            "purpose": "Relay service",
            "destination": "https://api.longweisos.com",
            "credential": "Relay account credential",
            "required": True,
        },
        {
            "purpose": "User-selected premium provider",
            "destination": "selected locally by the user",
            "credential": "premium-provider credential",
            "required": True,
            "available_provider_labels": providers,
            "sent_to_relay": False,
        },
        {
            "purpose": "Local OpenAI-compatible API",
            "destination": "http://127.0.0.1:18777",
            "credential": "device-local API key",
            "required": False,
            "external_network": False,
        },
    )
