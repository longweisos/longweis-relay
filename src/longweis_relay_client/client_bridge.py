"""Customer-side Relay preparation and premium-provider execution.

This module is safe for the desktop client. It contains only the public
handoff contract and direct OpenAI-compatible transport. It does not contain
server routing, memory construction, provider credentials, or pricing policy.
"""

import json
import re
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .client_endpoint import normalize_relay_base_url
from .client_errors import ProviderResponseError, ValidationError
from .client_model_selection import PremiumModelSelection
from .client_modes import RelayBillingMode


BridgeTransport = Callable[
    [str, Mapping[str, str], bytes, float], Tuple[int, bytes]
]
PUBLIC_CLIENT_PROTOCOL = "longweis-relay-public-client/1"
CredentialReader = Callable[[str], str]
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
_MAX_RELAY_RESPONSE_BYTES = 1024 * 1024
_MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
# OpenClaw's configured Relay model exposes a 128k-token context.  Keep the
# client-side byte ceiling meaningfully below the 2 MiB HTTP request limit,
# while allowing normal long-context work instead of failing at 64 KiB.
_MAX_BRIDGE_MESSAGES_BYTES = 512 * 1024
_MAX_OPENAI_CHAT_MESSAGES_BYTES = 512 * 1024
_SUPPORTED_PROVIDERS = frozenset(
    {"openai", "openrouter", "custom-openai-compatible"}
)
_SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
)


class _NoCredentialRedirects(urllib.request.HTTPRedirectHandler):
    """Do not forward either customer credential to another origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _client_tls_context() -> ssl.SSLContext:
    """Use platform trust when the app's relocated Python has no CA path."""

    for candidate in _SYSTEM_CA_FILES:
        if Path(candidate).is_file():
            try:
                return ssl.create_default_context(cafile=candidate)
            except (OSError, ssl.SSLError):
                continue
    return ssl.create_default_context()


class ClientUsageMeasurement(str, Enum):
    REPORTED = "reported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ClientTokenUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    measurement: ClientUsageMeasurement = ClientUsageMeasurement.REPORTED

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValidationError("premium usage model is invalid")
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValidationError("premium token usage is invalid")
        if not isinstance(self.measurement, ClientUsageMeasurement):
            raise ValidationError("premium usage measurement is invalid")
        if self.measurement == ClientUsageMeasurement.UNAVAILABLE and any(values):
            raise ValidationError("unavailable premium usage cannot contain tokens")

    @property
    def is_available(self) -> bool:
        return self.measurement == ClientUsageMeasurement.REPORTED

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class ClientChatResponse:
    content: str
    usage: ClientTokenUsage
    response_id: Optional[str]
    finish_reason: Optional[str]


@dataclass(frozen=True)
class ClientOpenAIChatResponse:
    """A bounded OpenAI chat choice that may contain function calls.

    The normal Bridge path intentionally exposes text only.  This separate
    value is used when a local OpenAI-compatible client needs a function-call
    turn preserved for its own tool runtime (for example, OpenClaw).  Relay
    never executes those tools.
    """

    message: Mapping[str, object]
    usage: ClientTokenUsage
    response_id: Optional[str]
    finish_reason: Optional[str]
    relay_charge: Optional[Mapping[str, object]] = None
    relay_action: str = "call_premium_direct"
    relay_reason: str = "tool_passthrough"
    # Optional premium usage remains separate from the Relay response usage.
    premium_usage: Optional[ClientTokenUsage] = None


def _credential_reference(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SAFE_REFERENCE.fullmatch(value):
        raise ValidationError("%s credential reference is invalid" % name)
    return value


def _read_credential(reader: CredentialReader, reference: str, name: str) -> str:
    try:
        value = reader(reference)
    except Exception:
        raise ValidationError("%s credential is unavailable" % name) from None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s credential is unavailable" % name)
    return value.strip()


def _urllib_transport(
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    timeout: float,
) -> Tuple[int, bytes]:
    request = urllib.request.Request(
        url=url, data=payload, headers=dict(headers), method="POST"
    )
    try:
        opener = urllib.request.build_opener(
            _NoCredentialRedirects(),
            urllib.request.HTTPSHandler(context=_client_tls_context()),
        )
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderResponseError("provider request failed") from None


@dataclass(frozen=True)
class LocalRelayConfig:
    base_url: str = "https://api.longweisos.com/v1"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_relay_base_url(self.base_url))
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not (0.1 <= float(self.timeout_seconds) <= 600.0)
        ):
            raise ValidationError("Relay timeout is invalid")


class LocalRelayClient:
    """Calls Relay without reading the premium-provider credential."""

    def __init__(
        self,
        config: LocalRelayConfig,
        relay_credential_reference: str,
        credential_reader: CredentialReader,
        transport: Optional[BridgeTransport] = None,
    ) -> None:
        if not isinstance(config, LocalRelayConfig):
            raise ValidationError("Relay client config is invalid")
        if not callable(credential_reader):
            raise ValidationError("Relay credential reader is invalid")
        self.config = config
        self.credential_reference = _credential_reference(
            "Relay", relay_credential_reference
        )
        self._credential_reader = credential_reader
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:
        return "LocalRelayClient(base_url=%r, credential_reference=%r)" % (
            self.config.base_url,
            self.credential_reference,
        )

    def prepare(self, payload: Mapping[str, object]) -> Dict[str, object]:
        if not isinstance(payload, Mapping):
            raise ValidationError("Relay Bridge payload must be an object")
        relay_key = _read_credential(
            self._credential_reader, self.credential_reference, "Relay"
        )
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            status, body = self._transport(
                self.config.base_url + "/bridge/prepare",
                {
                    "Authorization": "Bearer " + relay_key,
                    "Content-Type": "application/json",
                    "User-Agent": "longweis-relay-bridge/0.49",
                },
                encoded,
                float(self.config.timeout_seconds),
            )
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("Relay transport failed") from None
        if status < 200 or status >= 300:
            raise ProviderResponseError("Relay returned HTTP %d" % status)
        if len(body) > _MAX_RELAY_RESPONSE_BYTES:
            raise ProviderResponseError("Relay response exceeded the size limit")
        try:
            response = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderResponseError("Relay returned malformed JSON") from None
        if not isinstance(response, dict):
            raise ProviderResponseError("Relay returned an invalid handoff")
        return response

    def record_premium_usage(
        self, request_id: str, usage: Optional[ClientTokenUsage]
    ) -> None:
        """Best-effort, content-free token receipt for the account display."""

        if not isinstance(request_id, str) or not _SAFE_REQUEST_ID.fullmatch(request_id):
            raise ValidationError("premium usage request ID is invalid")
        available = isinstance(usage, ClientTokenUsage) and usage.is_available
        payload = {
            "request_id": request_id,
            "premium_usage_available": available,
            "input_tokens": usage.input_tokens if available else 0,
            "output_tokens": usage.output_tokens if available else 0,
            "cache_read_tokens": usage.cache_read_tokens if available else 0,
            "cache_write_tokens": usage.cache_write_tokens if available else 0,
        }
        relay_key = _read_credential(
            self._credential_reader, self.credential_reference, "Relay"
        )
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            status, body = self._transport(
                self.config.base_url + "/relay/usage",
                {
                    "Authorization": "Bearer " + relay_key,
                    "Content-Type": "application/json",
                    "User-Agent": "longweis-relay-usage/0.68",
                },
                encoded,
                float(self.config.timeout_seconds),
            )
        except Exception:
            return
        if status < 200 or status >= 300 or len(body) > _MAX_RELAY_RESPONSE_BYTES:
            return


    def complete_tool_chat(
        self, request_id: str, request: Mapping[str, object]
    ) -> ClientOpenAIChatResponse:
        """Ask Relay to plan one tool turn and return its metered receipt."""

        if not isinstance(request_id, str) or not _SAFE_REQUEST_ID.fullmatch(request_id):
            raise ValidationError("tool request ID is invalid")
        if not isinstance(request, Mapping):
            raise ValidationError("OpenAI tool request is invalid")
        relay_key = _read_credential(
            self._credential_reader, self.credential_reference, "Relay"
        )
        payload = dict(request)
        payload["request_id"] = request_id
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            status, body = self._transport(
                self.config.base_url + "/bridge/tool-completions",
                {
                    "Authorization": "Bearer " + relay_key,
                    "Content-Type": "application/json",
                    "User-Agent": "longweis-relay-tool/0.64",
                },
                encoded,
                float(self.config.timeout_seconds),
            )
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("Relay tool transport failed") from None
        if status < 200 or status >= 300:
            raise ProviderResponseError("Relay tool request returned HTTP %d" % status)
        if len(body) > _MAX_RELAY_RESPONSE_BYTES:
            raise ProviderResponseError("Relay tool response exceeded the size limit")
        try:
            decoded = json.loads(body.decode("utf-8"))
            message = decoded.get("message")
            usage = decoded.get("usage")
            charge = decoded.get("relay_charge")
            if (
                decoded.get("object") != "longweis_relay.tool_completion"
                or decoded.get("request_id") != request_id
                or not isinstance(message, Mapping)
                or message.get("role") != "assistant"
                or not isinstance(usage, Mapping)
                or not isinstance(charge, Mapping)
            ):
                raise ValueError("invalid tool completion")
            content = message.get("content")
            calls = message.get("tool_calls")
            if not isinstance(content, (str, type(None))) or (
                content is None and not isinstance(calls, list)
            ):
                raise ValueError("invalid tool completion message")
            input_tokens = int(usage.get("prompt_tokens"))
            output_tokens = int(usage.get("completion_tokens"))
            cache_read = int(usage.get("cache_read_tokens", 0))
            cache_write = int(usage.get("cache_write_tokens", 0))
            if any(item < 0 for item in (input_tokens, output_tokens, cache_read, cache_write)):
                raise ValueError("invalid tool completion usage")
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ProviderResponseError("Relay returned an invalid tool completion") from None
        public_message = {"role": "assistant", "content": content}
        if isinstance(calls, list):
            public_message["tool_calls"] = list(calls)
        return ClientOpenAIChatResponse(
            message=public_message,
            usage=ClientTokenUsage(
                model="longweis-relay/relay-auto",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
            response_id=None,
            finish_reason=(str(decoded["finish_reason"]) if decoded.get("finish_reason") else None),
            relay_charge=dict(charge),
            relay_action="relay_tool_model",
            relay_reason="relay_tool_completion",
        )


def _premium_base_url(selection: PremiumModelSelection) -> str:
    if selection.provider_id == "openai":
        return "https://api.openai.com/v1"
    if selection.provider_id == "openrouter":
        return "https://openrouter.ai/api/v1"
    if selection.provider_id == "custom-openai-compatible":
        if selection.custom_base_url is None:
            raise ValidationError("custom premium base URL is required")
        return selection.custom_base_url
    raise ValidationError(
        "this client supports OpenAI, OpenRouter, or a custom "
        "OpenAI-compatible premium provider"
    )


class LocalPremiumClient:
    """Reads the premium credential only for the direct local provider call."""

    def __init__(
        self,
        selection: PremiumModelSelection,
        premium_credential_reference: str,
        credential_reader: CredentialReader,
        transport: Optional[BridgeTransport] = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
    ) -> None:
        if not isinstance(selection, PremiumModelSelection):
            raise ValidationError("premium model selection is invalid")
        if selection.provider_id not in _SUPPORTED_PROVIDERS:
            _premium_base_url(selection)
        if not callable(credential_reader):
            raise ValidationError("premium credential reader is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not (0.1 <= float(timeout_seconds) <= 600.0)
            or isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not (1 <= max_output_tokens <= 131_072)
        ):
            raise ValidationError("premium provider limits are invalid")
        self.selection = selection
        self.credential_reference = _credential_reference(
            "premium", premium_credential_reference
        )
        self._credential_reader = credential_reader
        self._transport = transport or _urllib_transport
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = max_output_tokens

    def __repr__(self) -> str:
        return (
            "LocalPremiumClient(provider=%r, model=%r, credential_reference=%r)"
            % (
                self.selection.provider_id,
                self.selection.model_id,
                self.credential_reference,
            )
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
    ) -> ClientChatResponse:
        """Run one text-only OpenAI-compatible completion.

        The loopback API may receive standard client controls such as
        ``max_tokens``, ``temperature``, and ``top_p``.  Preserve the controls
        we advertise instead of accepting then silently discarding them.
        Tool-bearing requests intentionally use a separate compatibility path;
        this text boundary must never flatten tool calls into a prompt.
        """

        normalized = LocalRelayBridge._messages(messages)
        output_limit = (
            self.max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        if (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or not (1 <= output_limit <= 131_072)
        ):
            raise ValidationError("premium output token limit is invalid")
        sampled_temperature = 1.0 if temperature is None else temperature
        if (
            isinstance(sampled_temperature, bool)
            or not isinstance(sampled_temperature, (int, float))
            or not (0.0 <= float(sampled_temperature) <= 2.0)
        ):
            raise ValidationError("premium temperature is invalid")
        if top_p is not None and (
            isinstance(top_p, bool)
            or not isinstance(top_p, (int, float))
            or not (0.0 < float(top_p) <= 1.0)
        ):
            raise ValidationError("premium top_p is invalid")
        premium_key = _read_credential(
            self._credential_reader,
            self.credential_reference,
            "premium provider",
        )
        request_payload = {
            "model": self.selection.model_id,
            "messages": [dict(item) for item in normalized],
            "max_tokens": output_limit,
            "temperature": float(sampled_temperature),
        }
        if top_p is not None:
            request_payload["top_p"] = float(top_p)
        payload = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            status, body = self._transport(
                _premium_base_url(self.selection).rstrip("/")
                + "/chat/completions",
                {
                    "Authorization": "Bearer " + premium_key,
                    "Content-Type": "application/json",
                    "User-Agent": "longweis-relay/0.49",
                },
                payload,
                self.timeout_seconds,
            )
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("provider transport failed") from None
        if status < 200 or status >= 300:
            raise ProviderResponseError("provider returned HTTP %d" % status)
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderResponseError("provider response exceeded the size limit")
        try:
            decoded = json.loads(body.decode("utf-8"))
            choice = decoded["choices"][0]
            content_value = choice["message"]["content"]
            if isinstance(content_value, str):
                content = content_value
            elif isinstance(content_value, list):
                content = "".join(
                    str(part.get("text", ""))
                    for part in content_value
                    if isinstance(part, Mapping)
                )
            else:
                raise ValueError("invalid content")
            usage_payload = decoded.get("usage") or {}
            usage_available = (
                isinstance(usage_payload, Mapping)
                and "prompt_tokens" in usage_payload
                and "completion_tokens" in usage_payload
            )
            input_tokens = int(usage_payload.get("prompt_tokens", 0))
            output_tokens = int(usage_payload.get("completion_tokens", 0))
            if not content.strip() or input_tokens < 0 or output_tokens < 0:
                raise ValueError("invalid provider response")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise ProviderResponseError("provider returned a malformed response") from None
        return ClientChatResponse(
            content=content.strip(),
            usage=ClientTokenUsage(
                model=self.selection.model_id,
                input_tokens=input_tokens if usage_available else 0,
                output_tokens=output_tokens if usage_available else 0,
                measurement=(
                    ClientUsageMeasurement.REPORTED
                    if usage_available
                    else ClientUsageMeasurement.UNAVAILABLE
                ),
            ),
            response_id=(str(decoded["id"]) if decoded.get("id") else None),
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason")
                else None
            ),
        )

    def complete_openai_chat(
        self, request: Mapping[str, object]
    ) -> ClientOpenAIChatResponse:
        """Run Relay's tool-capable model turn against the chosen provider.

        The current hosted preparation contract accepts only text messages.
        Relay therefore keeps the tool transcript intact in its local model
        turn rather than flattening it into text.  It remains the OpenClaw
        model endpoint; OpenClaw executes any returned call under its own
        permission policy and supplies the next tool-result turn to Relay.
        """

        if not isinstance(request, Mapping):
            raise ValidationError("OpenAI chat request is invalid")
        allowed = frozenset(
            {
                "messages",
                "max_tokens",
                "max_completion_tokens",
                "temperature",
                "top_p",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
            }
        )
        if set(request) - allowed:
            raise ValidationError("OpenAI chat request contains unsupported fields")
        messages = request.get("messages")
        tools = request.get("tools")
        if not isinstance(messages, list) or not messages or len(messages) > 128:
            raise ValidationError("OpenAI chat messages are invalid")
        if tools is not None and (
            not isinstance(tools, list) or len(tools) > 64
        ):
            raise ValidationError("OpenAI chat tools are invalid")
        try:
            encoded_messages = json.dumps(
                messages, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            encoded_tools = json.dumps(
                tools or [], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValidationError("OpenAI chat request is not JSON serializable") from None
        if (
            len(encoded_messages) > _MAX_OPENAI_CHAT_MESSAGES_BYTES
            or len(encoded_tools) > 64 * 1024
        ):
            raise ValidationError("OpenAI chat request exceeds local tool limits")
        if not all(isinstance(item, Mapping) for item in messages):
            raise ValidationError("OpenAI chat messages are invalid")
        if tools is not None and not all(isinstance(item, Mapping) for item in tools):
            raise ValidationError("OpenAI chat tools are invalid")
        for tool in tools or []:
            function = tool.get("function")
            if (
                tool.get("type") != "function"
                or not isinstance(function, Mapping)
                or not isinstance(function.get("name"), str)
                or not _SAFE_TOOL_NAME.fullmatch(function["name"])
            ):
                raise ValidationError("OpenAI chat tool definition is invalid")

        raw_max_tokens = request.get("max_tokens")
        raw_max_completion_tokens = request.get("max_completion_tokens")
        limits = [
            value
            for value in (raw_max_tokens, raw_max_completion_tokens)
            if value is not None
        ]
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (1 <= value <= 131_072)
            for value in limits
        ) or (len(limits) == 2 and limits[0] != limits[1]):
            raise ValidationError("OpenAI chat output limit is invalid")
        output_limit = limits[0] if limits else self.max_output_tokens
        temperature = request.get("temperature", 1.0)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not (0.0 <= float(temperature) <= 2.0)
        ):
            raise ValidationError("OpenAI chat temperature is invalid")
        top_p = request.get("top_p")
        if top_p is not None and (
            isinstance(top_p, bool)
            or not isinstance(top_p, (int, float))
            or not (0.0 < float(top_p) <= 1.0)
        ):
            raise ValidationError("OpenAI chat top_p is invalid")
        tool_choice = request.get("tool_choice")
        if tool_choice is not None and not isinstance(tool_choice, (str, Mapping)):
            raise ValidationError("OpenAI chat tool_choice is invalid")
        parallel = request.get("parallel_tool_calls")
        if parallel is not None and not isinstance(parallel, bool):
            raise ValidationError("OpenAI chat parallel_tool_calls is invalid")

        provider_payload = {
            "model": self.selection.model_id,
            "messages": list(messages),
            "max_tokens": output_limit,
            "temperature": float(temperature),
            # Relay emits a verified, buffered local response.  Streaming the
            # provider response through this direct compatibility path would
            # permit partially formed tool calls to escape to the caller.
            "stream": False,
        }
        if top_p is not None:
            provider_payload["top_p"] = float(top_p)
        if tools is not None:
            provider_payload["tools"] = list(tools)
        if tool_choice is not None:
            provider_payload["tool_choice"] = dict(tool_choice) if isinstance(tool_choice, Mapping) else tool_choice
        if parallel is not None:
            provider_payload["parallel_tool_calls"] = parallel
        premium_key = _read_credential(
            self._credential_reader,
            self.credential_reference,
            "premium provider",
        )
        try:
            status, body = self._transport(
                _premium_base_url(self.selection).rstrip("/") + "/chat/completions",
                {
                    "Authorization": "Bearer " + premium_key,
                    "Content-Type": "application/json",
                    "User-Agent": "longweis-relay/0.61",
                },
                json.dumps(
                    provider_payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                self.timeout_seconds,
            )
        except ProviderResponseError:
            raise
        except Exception:
            raise ProviderResponseError("provider transport failed") from None
        if status < 200 or status >= 300:
            raise ProviderResponseError("provider returned HTTP %d" % status)
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderResponseError("provider response exceeded the size limit")
        try:
            decoded = json.loads(body.decode("utf-8"))
            choice = decoded["choices"][0]
            message = choice["message"]
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                raise ValueError("invalid message")
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            if not isinstance(content, (str, type(None))) or (
                content is None and not isinstance(tool_calls, list)
            ):
                raise ValueError("invalid content")
            if tool_calls is not None and (
                not isinstance(tool_calls, list) or not tool_calls or len(tool_calls) > 64
            ):
                raise ValueError("invalid tool calls")
            for tool_call in tool_calls or []:
                function = tool_call.get("function") if isinstance(tool_call, Mapping) else None
                if (
                    not isinstance(tool_call, Mapping)
                    or not isinstance(tool_call.get("id"), str)
                    or not _SAFE_REQUEST_ID.fullmatch(tool_call["id"])
                    or tool_call.get("type") != "function"
                    or not isinstance(function, Mapping)
                    or not isinstance(function.get("name"), str)
                    or not _SAFE_TOOL_NAME.fullmatch(function["name"])
                    or not isinstance(function.get("arguments"), str)
                    or len(function["arguments"].encode("utf-8")) > 64 * 1024
                ):
                    raise ValueError("invalid tool call")
            public_message = {"role": "assistant", "content": content}
            if tool_calls is not None:
                public_message["tool_calls"] = list(tool_calls)
            usage_payload = decoded.get("usage") or {}
            usage_available = (
                isinstance(usage_payload, Mapping)
                and "prompt_tokens" in usage_payload
                and "completion_tokens" in usage_payload
            )
            input_tokens = int(usage_payload.get("prompt_tokens", 0))
            output_tokens = int(usage_payload.get("completion_tokens", 0))
            if input_tokens < 0 or output_tokens < 0:
                raise ValueError("invalid usage")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise ProviderResponseError("provider returned a malformed response") from None
        return ClientOpenAIChatResponse(
            message=public_message,
            usage=ClientTokenUsage(
                model=self.selection.model_id,
                input_tokens=input_tokens if usage_available else 0,
                output_tokens=output_tokens if usage_available else 0,
                measurement=(
                    ClientUsageMeasurement.REPORTED
                    if usage_available
                    else ClientUsageMeasurement.UNAVAILABLE
                ),
            ),
            response_id=(str(decoded["id"]) if decoded.get("id") else None),
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason")
                else None
            ),
        )


@dataclass(frozen=True)
class LocalBridgeCompletion:
    request_id: str
    mode: RelayBillingMode
    provider: str
    model: str
    content: str
    premium_usage: ClientTokenUsage
    relay_charge: Optional[Mapping[str, object]]
    relay_action: str
    relay_reason: str

    def public_payload(self) -> Dict[str, object]:
        usage = self.premium_usage
        return {
            "object": "longweis_relay.local_completion",
            "request_id": self.request_id,
            "mode": self.mode.value,
            "premium": {"provider": self.provider, "model": self.model},
            "content": self.content,
            "relay_action": self.relay_action,
            "relay_reason": self.relay_reason,
            "premium_usage": {
                "measurement": usage.measurement.value,
                "input_tokens": usage.input_tokens if usage.is_available else None,
                "output_tokens": usage.output_tokens if usage.is_available else None,
                "cache_read_tokens": (
                    usage.cache_read_tokens if usage.is_available else None
                ),
                "cache_write_tokens": (
                    usage.cache_write_tokens if usage.is_available else None
                ),
                "total_tokens": usage.total_tokens if usage.is_available else None,
            },
            "relay_charge": (
                dict(self.relay_charge)
                if isinstance(self.relay_charge, Mapping)
                else None
            ),
            "premium_provider_charge_included": False,
        }


class LocalRelayBridge:
    """Execute the public handoff contract without embedding service policy.

    The client validates the server response and either returns a Relay result
    or calls the user-selected provider with the original request. Routing,
    prompts, thresholds, and memory construction remain server-side.
    """

    def __init__(
        self, relay_client: LocalRelayClient, premium_client: LocalPremiumClient
    ) -> None:
        if not isinstance(relay_client, LocalRelayClient):
            raise ValidationError("local Relay client is invalid")
        if not isinstance(premium_client, LocalPremiumClient):
            raise ValidationError("local premium client is invalid")
        self.relay_client = relay_client
        self.premium_client = premium_client

    def record_usage(
        self,
        request_id: str,
        usage: Optional[ClientTokenUsage],
        relay_action: str,
    ) -> None:
        """Never let optional account telemetry affect a customer response."""

        if relay_action == "relay_tool_model":
            usage = None
        try:
            self.relay_client.record_premium_usage(request_id, usage)
        except Exception:
            pass

    @staticmethod
    def _messages(value: object) -> Tuple[Mapping[str, str], ...]:
        if not isinstance(value, (list, tuple)) or not value or len(value) > 128:
            raise ValidationError("Bridge messages are invalid")
        messages = []
        total_bytes = 0
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
                raise ValidationError("Bridge messages are invalid")
            role = item.get("role")
            content = item.get("content")
            if role not in {"developer", "system", "user", "assistant"}:
                raise ValidationError("Bridge message role is invalid")
            if not isinstance(content, str) or not content.strip():
                raise ValidationError("Bridge message content is invalid")
            total_bytes += len(content.encode("utf-8"))
            messages.append({"role": str(role), "content": content.strip()})
        if (
            total_bytes > _MAX_BRIDGE_MESSAGES_BYTES
            or messages[-1]["role"] != "user"
        ):
            raise ValidationError("Bridge conversation is invalid")
        return tuple(messages)

    def complete(
        self,
        *,
        request_id: str,
        mode: RelayBillingMode,
        messages: Sequence[Mapping[str, str]],
        memory_budget_tokens: int = 800,
        max_output_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
    ) -> LocalBridgeCompletion:
        if not isinstance(request_id, str) or not _SAFE_REQUEST_ID.fullmatch(request_id):
            raise ValidationError("Bridge request ID is invalid")
        if not isinstance(mode, RelayBillingMode):
            raise ValidationError("Bridge mode is invalid")
        original_messages = self._messages(messages)
        selection = self.premium_client.selection
        try:
            relay_response = self.relay_client.prepare(
                {
                    "client_protocol": PUBLIC_CLIENT_PROTOCOL,
                    "request_id": request_id,
                    "mode": mode.value,
                    "premium_provider": selection.provider_id,
                    "premium_model": selection.model_id,
                    "messages": [dict(item) for item in original_messages],
                    "memory_budget_tokens": memory_budget_tokens,
                    "allow_relay_final": True,
                }
            )
        except ProviderResponseError:
            premium_response = self.premium_client.complete(
                original_messages,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                top_p=top_p,
            )
            return LocalBridgeCompletion(
                request_id=request_id,
                mode=mode,
                provider=selection.provider_id,
                model=selection.model_id,
                content=premium_response.content,
                premium_usage=premium_response.usage,
                relay_charge=None,
                relay_action="call_premium_direct",
                relay_reason="relay_unavailable",
            )
        expected_premium = {
            "provider": selection.provider_id,
            "model": selection.model_id,
        }
        if (
            relay_response.get("object") != "longweis_relay.bridge_handoff"
            or relay_response.get("client_protocol") != PUBLIC_CLIENT_PROTOCOL
            or relay_response.get("request_id") != request_id
            or relay_response.get("mode") != mode.value
            or relay_response.get("premium") != expected_premium
        ):
            raise ProviderResponseError("Relay handoff identity mismatch")
        action = relay_response.get("action")
        use_original = relay_response.get("use_original_messages")
        relay_charge = relay_response.get("relay_charge")
        if relay_charge is not None and not isinstance(relay_charge, Mapping):
            raise ProviderResponseError("Relay returned an invalid charge")
        if action == "return_relay_final" and use_original is False:
            if not isinstance(relay_charge, Mapping):
                raise ProviderResponseError("Relay final response has no charge receipt")
            content = relay_response.get("relay_output")
            if not isinstance(content, str) or not content.strip():
                raise ProviderResponseError("Relay returned an invalid final answer")
            return LocalBridgeCompletion(
                request_id=request_id,
                mode=mode,
                provider=selection.provider_id,
                model=selection.model_id,
                content=content.strip(),
                premium_usage=ClientTokenUsage(
                    model=selection.model_id,
                    measurement=ClientUsageMeasurement.UNAVAILABLE,
                ),
                relay_charge=relay_charge,
                relay_action="return_relay_final",
                relay_reason=str(relay_response.get("reason") or "relay_final"),
            )
        if action != "call_premium_direct" or use_original is not True:
            raise ProviderResponseError("Relay returned an invalid handoff action")
        if relay_charge is not None:
            raise ProviderResponseError("Relay direct handoff unexpectedly included a charge")
        premium_response = self.premium_client.complete(
            original_messages,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        return LocalBridgeCompletion(
            request_id=request_id,
            mode=mode,
            provider=selection.provider_id,
            model=selection.model_id,
            content=premium_response.content,
            premium_usage=premium_response.usage,
            relay_charge=relay_charge,
            relay_action="call_premium_direct",
            relay_reason=str(relay_response.get("reason") or "relay_handoff"),
        )

    def complete_openai_chat(
        self, request: Mapping[str, object], *, request_id: Optional[str] = None
    ) -> ClientOpenAIChatResponse:
        """Run one Relay tool turn, falling back to the selected provider."""

        tool_request_id = request_id or ("tool-" + uuid.uuid4().hex)
        try:
            return self.relay_client.complete_tool_chat(tool_request_id, request)
        except ProviderResponseError:
            response = self.premium_client.complete_openai_chat(request)
            return ClientOpenAIChatResponse(
                message=response.message,
                usage=response.usage,
                response_id=response.response_id,
                finish_reason=response.finish_reason,
                relay_charge=None,
                relay_action="call_premium_direct",
                relay_reason="relay_tool_unavailable",
                premium_usage=response.usage,
            )
