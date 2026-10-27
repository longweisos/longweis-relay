"""Local Relay Bridge premium-model selection contract.

This module is safe to ship with the thin client. It contains provider
discovery plumbing and a coarse customer warning only; it contains no private
memory, routing, calibration, prompt, provider credential, or margin logic.
"""

import json
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

from .client_errors import ValidationError


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:+~-]{0,255}$")
_LOW_COST_TOKENS = frozenset(
    {"flash", "free", "haiku", "lite", "mini", "nano"}
)
_SELECTION_FIELDS = frozenset({"provider", "model", "custom_base_url"})


@dataclass(frozen=True)
class ProviderOption:
    provider_id: str
    label: str
    aggregator: bool
    discovery: str
    manual_model_allowed: bool = True
    custom_base_url_required: bool = False

    def public_payload(self) -> Dict[str, object]:
        return {
            "id": self.provider_id,
            "label": self.label,
            "aggregator": self.aggregator,
            "discovery": self.discovery,
            "manual_model_allowed": self.manual_model_allowed,
            "custom_base_url_required": self.custom_base_url_required,
        }


_PROVIDERS = (
    ProviderOption("openai", "OpenAI", False, "api"),
    ProviderOption("anthropic", "Anthropic", False, "manual"),
    ProviderOption("google", "Google Gemini", False, "api"),
    ProviderOption("openrouter", "OpenRouter", True, "api"),
    ProviderOption(
        "custom-openai-compatible",
        "Custom OpenAI-compatible API",
        True,
        "api",
        custom_base_url_required=True,
    ),
)
_PROVIDER_BY_ID = {item.provider_id: item for item in _PROVIDERS}


def provider_options() -> Tuple[ProviderOption, ...]:
    return _PROVIDERS


def _provider(provider_id: object) -> ProviderOption:
    if not isinstance(provider_id, str):
        raise ValidationError("provider must be text")
    try:
        return _PROVIDER_BY_ID[provider_id.strip().lower()]
    except KeyError:
        raise ValidationError("provider is unsupported") from None


def _custom_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("custom_base_url is required")
    parsed = urlsplit(value.strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("custom_base_url must not contain credentials or parameters")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValidationError("custom_base_url must be one HTTPS origin or API base")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass(frozen=True)
class PremiumModelSelection:
    provider_id: str
    model_id: str
    custom_base_url: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "PremiumModelSelection":
        if not isinstance(payload, Mapping):
            raise ValidationError("model selection must be an object")
        unsupported = sorted(set(payload) - _SELECTION_FIELDS)
        if unsupported:
            raise ValidationError(
                "model selection contains unsupported fields: %s"
                % ", ".join(unsupported)
            )
        provider = _provider(payload.get("provider"))
        model = payload.get("model")
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model.strip()):
            raise ValidationError("a valid model id is required")
        custom = payload.get("custom_base_url")
        if provider.custom_base_url_required:
            custom = _custom_base_url(custom)
        elif custom is not None:
            raise ValidationError("custom_base_url is allowed only for a custom provider")
        return cls(provider.provider_id, model.strip(), custom)

    def relay_payload(self) -> Dict[str, str]:
        """Return the credential-free model identity safe to send to Relay."""

        return {"provider": self.provider_id, "premium_model": self.model_id}


@dataclass(frozen=True)
class CostFitNotice:
    level: str
    code: str
    message: str
    blocks_selection: bool = False

    def public_payload(self) -> Dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "blocks_selection": self.blocks_selection,
        }


def assess_cost_fit(selection: PremiumModelSelection) -> CostFitNotice:
    """Return a coarse warning, never an access decision or savings promise."""

    tokens = frozenset(
        item for item in re.split(r"[^a-z0-9]+", selection.model_id.lower()) if item
    )
    low_cost = bool(tokens & _LOW_COST_TOKENS)
    if low_cost:
        return CostFitNotice(
            level="red",
            code="possible_low_cost_model",
            message=(
                "This appears to be a low-cost model. Your provider charge plus "
                "the Relay charge may be higher than using it directly. You may "
                "continue anyway."
            ),
        )
    return CostFitNotice(
        level="neutral",
        code="cost_fit_not_verified",
        message=(
            "Cost fit has not been verified for this model. Compare the combined "
            "provider and Relay charges."
        ),
    )


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    label: str

    def public_payload(self) -> Dict[str, str]:
        return {"id": self.model_id, "label": self.label}


def filter_discovered_models(
    models: Sequence[DiscoveredModel], query: object, *, limit: int = 100
) -> Tuple[DiscoveredModel, ...]:
    """Return a bounded, forgiving local model picker result set.

    Discovery may return hundreds of models. This filter stays fully local and
    accepts either a contiguous match (``gpt``) or an ordered-character match
    (``g5p``) across the public model ID and display label.
    """

    if not isinstance(query, str):
        return ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("model search limit is invalid")
    compact_query = re.sub(r"[^a-z0-9]+", "", query.lower())
    if not compact_query:
        return tuple(models[:limit])

    def match_rank(item: DiscoveredModel) -> Optional[int]:
        model_id = item.model_id.lower()
        label = item.label.lower()
        compact_id = re.sub(r"[^a-z0-9]+", "", model_id)
        compact_label = re.sub(r"[^a-z0-9]+", "", label)
        if compact_query in compact_id:
            return 0
        if compact_query in compact_label:
            return 1
        for candidate in (compact_id, compact_label):
            position = 0
            for character in compact_query:
                position = candidate.find(character, position)
                if position < 0:
                    break
                position += 1
            else:
                return 2
        return None

    matched = []
    for item in models:
        rank = match_rank(item)
        if rank is not None:
            matched.append((rank, item))
    matched.sort(key=lambda pair: (pair[0], pair[1].label.lower(), pair[1].model_id))
    return tuple(item for unused, item in matched[:limit])


def model_catalog_request(
    provider_id: object,
    api_key: object,
    custom_base_url: Optional[object] = None,
) -> Request:
    """Build a direct client-to-provider discovery request.

    The returned request is executed by the local Bridge.  Relay never receives
    the credential or proxies model discovery.
    """

    provider = _provider(provider_id)
    if provider.discovery != "api":
        raise ValidationError("this provider requires manual model entry")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValidationError("provider API key is required for model discovery")
    credential = api_key.strip()
    headers = {"Accept": "application/json"}
    if provider.provider_id == "openai":
        url = "https://api.openai.com/v1/models"
        headers["Authorization"] = "Bearer %s" % credential
    elif provider.provider_id == "openrouter":
        url = "https://openrouter.ai/api/v1/models?output_modalities=text"
        headers["Authorization"] = "Bearer %s" % credential
    elif provider.provider_id == "google":
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        headers["X-Goog-Api-Key"] = credential
    else:
        base = _custom_base_url(custom_base_url)
        url = "%s/models" % base
        headers["Authorization"] = "Bearer %s" % credential
    return Request(url, headers=headers, method="GET")


def _bounded_catalog(payload: object) -> Mapping[str, object]:
    if isinstance(payload, bytes):
        if len(payload) > 4 * 1024 * 1024:
            raise ValidationError("model catalog is too large")
        try:
            payload = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValidationError("model catalog is invalid") from None
    if not isinstance(payload, Mapping):
        raise ValidationError("model catalog must be an object")
    return payload


def parse_model_catalog(
    provider_id: object, payload: object
) -> Tuple[DiscoveredModel, ...]:
    provider = _provider(provider_id)
    catalog = _bounded_catalog(payload)
    raw_models: object
    if provider.provider_id == "google":
        raw_models = catalog.get("models")
    else:
        raw_models = catalog.get("data")
    if not isinstance(raw_models, Sequence) or isinstance(raw_models, (str, bytes)):
        raise ValidationError("model catalog does not contain a model list")
    found: Dict[str, DiscoveredModel] = {}
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        if provider.provider_id == "google":
            actions = item.get("supportedGenerationMethods")
            if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
                if "generateContent" not in actions:
                    continue
            model_id = item.get("baseModelId") or item.get("name")
            if isinstance(model_id, str) and model_id.startswith("models/"):
                model_id = model_id[len("models/") :]
            label = item.get("displayName") or model_id
        else:
            model_id = item.get("id")
            label = item.get("name") or model_id
        if (
            isinstance(model_id, str)
            and _MODEL_ID.fullmatch(model_id)
            and isinstance(label, str)
            and label.strip()
        ):
            found[model_id] = DiscoveredModel(model_id, label.strip())
        if len(found) > 5_000:
            raise ValidationError("model catalog exceeds the client limit")
    return tuple(sorted(found.values(), key=lambda item: (item.label.lower(), item.model_id)))


def model_setup_contract() -> Dict[str, object]:
    """Return the data needed to render the local selection screen."""

    return {
        "providers": [item.public_payload() for item in provider_options()],
        "model_required": True,
        "manual_model_entry": True,
        "api_key_storage": "local_os_credential_store_only",
        "low_cost_warning_color": "red",
        "low_cost_warning_blocks_selection": False,
    }
