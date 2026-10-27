"""Authenticated loopback OpenAI-compatible API for Longweis Relay."""

import hmac
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from .client_bridge import (
    ClientOpenAIChatResponse,
    LocalPremiumClient,
    LocalRelayBridge,
    LocalRelayClient,
    LocalRelayConfig,
)
from .client_errors import ProjectSError, ProviderResponseError, ValidationError
from .client_modes import RelayBillingMode
from .client_profile import (
    ClientConnectionMode,
    LocalBridgeProfile,
    LocalBridgeProfileStore,
)
from .client_credentials import default_credential_store


LOCAL_API_VERSION = "0.65.28"
LOCAL_API_MODEL = "relay-auto"
LOCAL_API_HOST = "127.0.0.1"
LOCAL_API_PORT = 18777
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# OpenClaw often begins several independent tool turns together.  Queue those
# bounded requests behind the local execution slots instead of exposing a
# transient 429 that the host may not retry.  The wait remains finite so a
# wedged upstream cannot accumulate unbounded HTTP workers.
_ADMISSION_WAIT_SECONDS = 180.0
# These are the bounded text-completion controls accepted by the local
# OpenAI-compatible endpoint.  Keep this list explicit: accepting arbitrary
# OpenAI fields and ignoring them would make a caller believe a request was
# honored when it was not.
_CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "n",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream_options",
    }
)
BridgeFactory = Callable[
    [LocalBridgeProfile, Callable[[str], str]], LocalRelayBridge
]


def _default_bridge_factory(
    profile: LocalBridgeProfile, credential_reader: Callable[[str], str]
) -> LocalRelayBridge:
    relay = LocalRelayClient(
        LocalRelayConfig(profile.relay_base_url),
        profile.relay_credential_reference,
        credential_reader,
    )
    premium = LocalPremiumClient(
        profile.selection,
        profile.premium_credential_reference,
        credential_reader,
    )
    return LocalRelayBridge(relay, premium)


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _error_payload(code: str, message: str) -> Dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error" if code == "invalid_request" else "api_error",
            "code": code,
        }
    }


def _invalid_chat(message: str) -> "LocalApiResponse":
    return LocalApiResponse(
        400,
        "application/json",
        _json_bytes(_error_payload("invalid_request", message)),
    )


@dataclass(frozen=True)
class LocalApiResponse:
    status: int
    content_type: str
    body: bytes
    stream: Optional[Iterable[bytes]] = None
    close_stream: Optional[Callable[[], None]] = None


@dataclass(frozen=True)
class LocalApiStatus:
    running: bool
    host: str
    port: int
    base_url: str
    public_model: str = LOCAL_API_MODEL

    def public_payload(self) -> Dict[str, object]:
        return {
            "local_api_version": LOCAL_API_VERSION,
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            "model": self.public_model,
            "loopback_only": True,
            "authentication": "local_key_bearer",
        }


class LocalOpenAIApplication:
    """Pure request dispatcher used by the loopback HTTP server."""

    def __init__(
        self,
        profile_store: LocalBridgeProfileStore,
        credential_store: object,
        bridge_factory: Optional[BridgeFactory] = None,
        maximum_in_flight: int = 2,
    ) -> None:
        if not isinstance(profile_store, LocalBridgeProfileStore):
            raise ValidationError("local API profile store is invalid")
        if (
            isinstance(maximum_in_flight, bool)
            or not isinstance(maximum_in_flight, int)
            or not (1 <= maximum_in_flight <= 16)
        ):
            raise ValidationError("local API concurrency limit is invalid")
        if bridge_factory is not None and not callable(bridge_factory):
            raise ValidationError("local API Bridge factory is invalid")
        self.profile_store = profile_store
        self.credential_store = credential_store
        self._bridge_factory = bridge_factory or _default_bridge_factory
        self._admission = threading.BoundedSemaphore(maximum_in_flight)

    def maximum_request_bytes_for(self, path: str) -> int:
        del path
        return _MAX_REQUEST_BYTES

    def _authenticate(
        self, headers: Mapping[str, str]
    ) -> Tuple[Optional[LocalBridgeProfile], Optional[LocalApiResponse]]:
        authorization = headers.get("authorization", "")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None, LocalApiResponse(
                401,
                "application/json",
                _json_bytes(_error_payload("invalid_api_key", "Invalid API key.")),
            )
        supplied = authorization[len("Bearer ") :].strip()
        try:
            profile = self.profile_store.load()
            expected = self.credential_store.read(
                profile.local_api_credential_reference
            )
        except ProjectSError:
            return None, LocalApiResponse(
                503,
                "application/json",
                _json_bytes(
                    _error_payload(
                        "local_setup_unavailable", "Local Relay setup is unavailable."
                    )
                ),
            )
        if not isinstance(expected, str) or not expected:
            return None, LocalApiResponse(
                503,
                "application/json",
                _json_bytes(
                    _error_payload(
                        "local_setup_unavailable", "Local Relay setup is unavailable."
                    )
                ),
            )
        if not supplied or not hmac.compare_digest(supplied, expected):
            return None, LocalApiResponse(
                401,
                "application/json",
                _json_bytes(_error_payload("invalid_api_key", "Invalid API key.")),
            )
        return profile, None

    @staticmethod
    def _request_id(headers: Mapping[str, str]) -> str:
        supplied = headers.get("idempotency-key", "")
        if isinstance(supplied, str) and _SAFE_REQUEST_ID.fullmatch(supplied):
            return supplied
        return "local-%s" % uuid.uuid4().hex

    @staticmethod
    def _chat_controls(payload: Mapping[str, object]) -> Dict[str, object]:
        """Validate controls that can be preserved by the text bridge.

        This is deliberately a small OpenAI-compatible subset.  A local
        caller gets a deterministic 400 for controls whose semantics are not
        implemented, rather than an apparently successful request with
        changed behavior.
        """

        raw_max_tokens = payload.get("max_tokens")
        raw_max_completion_tokens = payload.get("max_completion_tokens")
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
        ):
            raise ValidationError("max_tokens must be an integer from 1 to 131072")
        if len(limits) == 2 and limits[0] != limits[1]:
            raise ValidationError(
                "max_tokens and max_completion_tokens must match when both are set"
            )
        controls: Dict[str, object] = {}
        if limits:
            controls["max_output_tokens"] = limits[0]

        for field, minimum, maximum in (
            ("temperature", 0.0, 2.0),
            ("top_p", 0.0000001, 1.0),
        ):
            value = payload.get(field)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (minimum <= float(value) <= maximum)
            ):
                raise ValidationError("%s is invalid" % field)
            controls[field] = float(value)

        count = payload.get("n", 1)
        if isinstance(count, bool) or count != 1:
            raise ValidationError("only n=1 is supported")

        tools = payload.get("tools")
        if tools is not None:
            if not isinstance(tools, list):
                raise ValidationError("tools must be an array")
        tool_choice = payload.get("tool_choice")
        if tool_choice is not None and not isinstance(tool_choice, (str, Mapping)):
            raise ValidationError("tool_choice is invalid")
        parallel = payload.get("parallel_tool_calls")
        if parallel is not None and not isinstance(parallel, bool):
            raise ValidationError("parallel_tool_calls must be a boolean")
        stream_options = payload.get("stream_options")
        if stream_options is not None:
            if not isinstance(stream_options, Mapping) or set(stream_options) - {
                "include_usage"
            }:
                raise ValidationError("stream_options is invalid")
            include_usage = stream_options.get("include_usage", False)
            if not isinstance(include_usage, bool):
                raise ValidationError("stream_options.include_usage must be a boolean")
            # OpenClaw uses this standard OpenAI option.  It is meaningful only
            # for SSE, where the final empty-choice chunk contains usage.
            controls["stream_include_usage"] = include_usage
        return controls

    @staticmethod
    def _requires_openai_passthrough(payload: Mapping[str, object]) -> bool:
        """Return true when a request has function-call transcript semantics."""

        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            return True
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return False
        for message in messages:
            if not isinstance(message, Mapping):
                return False
            if (
                message.get("role") == "tool"
                or "tool_calls" in message
                or "tool_call_id" in message
            ):
                return True
        return False

    @staticmethod
    def _completion_payload(result: object, created: int) -> Dict[str, object]:
        usage = result.premium_usage
        prompt_tokens = usage.input_tokens if usage.is_available else 0
        completion_tokens = usage.output_tokens if usage.is_available else 0
        return {
            "id": "chatcmpl-%s" % result.request_id,
            "object": "chat.completion",
            "created": created,
            "model": LOCAL_API_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "relay": {
                "mode": result.mode.value,
                "action": result.relay_action,
                "premium_usage_available": usage.is_available,
                "charge": (
                    dict(result.relay_charge)
                    if isinstance(result.relay_charge, Mapping)
                    else None
                ),
            },
        }

    @staticmethod
    def _openai_completion_payload(
        result: ClientOpenAIChatResponse,
        request_id: str,
        mode: RelayBillingMode,
        created: int,
    ) -> Dict[str, object]:
        """Project a direct tool-capable premium response without hiding it."""

        usage = result.usage
        prompt_tokens = usage.input_tokens if usage.is_available else 0
        completion_tokens = usage.output_tokens if usage.is_available else 0
        message = dict(result.message)
        tool_calls = message.get("tool_calls")
        return {
            "id": "chatcmpl-%s" % request_id,
            "object": "chat.completion",
            "created": created,
            "model": LOCAL_API_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": (
                        result.finish_reason
                        or ("tool_calls" if isinstance(tool_calls, list) else "stop")
                    ),
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "relay": {
                "mode": mode.value,
                "action": result.relay_action,
                "reason": result.relay_reason,
                "tool_passthrough": True,
                "premium_usage_available": usage.is_available,
                "charge": (
                    dict(result.relay_charge)
                    if isinstance(result.relay_charge, Mapping)
                    else None
                ),
            },
        }

    @staticmethod
    def _event_stream(
        payload: Mapping[str, object], include_usage: bool = False
    ) -> bytes:
        choice = payload["choices"][0]
        message = choice["message"]
        base = {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "created": payload["created"],
            "model": LOCAL_API_MODEL,
        }
        content_chunk = dict(base)
        content_chunk["choices"] = [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": message["content"],
                },
                "finish_reason": None,
            }
        ]
        finish_chunk = dict(base)
        finish_chunk["choices"] = [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ]
        usage_chunk = b""
        if include_usage:
            usage = dict(base)
            usage["choices"] = []
            usage["usage"] = payload["usage"]
            usage_chunk = b"data: " + _json_bytes(usage) + b"\n\n"
        return (
            b"data: "
            + _json_bytes(content_chunk)
            + b"\n\ndata: "
            + _json_bytes(finish_chunk)
            + b"\n\n"
            + usage_chunk
            + b"data: [DONE]\n\n"
        )

    @staticmethod
    def _openai_event_stream(
        payload: Mapping[str, object], include_usage: bool = False
    ) -> bytes:
        """Emit a buffered OpenAI SSE response, including complete tool calls."""

        choice = payload["choices"][0]
        message = choice["message"]
        base = {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "created": payload["created"],
            "model": LOCAL_API_MODEL,
        }
        delta = {"role": "assistant"}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if isinstance(message.get("tool_calls"), list):
            delta["tool_calls"] = [
                dict(call, index=index)
                for index, call in enumerate(message["tool_calls"])
                if isinstance(call, Mapping)
            ]
        content_chunk = dict(base)
        content_chunk["choices"] = [
            {"index": 0, "delta": delta, "finish_reason": None}
        ]
        finish_chunk = dict(base)
        finish_chunk["choices"] = [
            {
                "index": 0,
                "delta": {},
                "finish_reason": choice["finish_reason"],
            }
        ]
        usage_chunk = b""
        if include_usage:
            usage = dict(base)
            usage["choices"] = []
            usage["usage"] = payload["usage"]
            usage_chunk = b"data: " + _json_bytes(usage) + b"\n\n"
        return (
            b"data: "
            + _json_bytes(content_chunk)
            + b"\n\ndata: "
            + _json_bytes(finish_chunk)
            + b"\n\n"
            + usage_chunk
            + b"data: [DONE]\n\n"
        )

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> LocalApiResponse:
        normalized_headers = {
            str(key).lower(): str(value) for key, value in headers.items()
        }
        request_path = path.partition("?")[0]
        if method == "GET" and request_path == "/healthz":
            return LocalApiResponse(
                200,
                "application/json",
                _json_bytes(
                    {
                        "status": "ok",
                        "service": "longweis-relay-local",
                        "loopback_only": True,
                    }
                ),
            )
        if request_path not in {"/v1/models", "/v1/chat/completions"}:
            return LocalApiResponse(
                404,
                "application/json",
                _json_bytes(_error_payload("not_found", "Endpoint not found.")),
            )
        profile, auth_error = self._authenticate(normalized_headers)
        if auth_error is not None:
            return auth_error
        if method == "GET" and request_path == "/v1/models":
            return LocalApiResponse(
                200,
                "application/json",
                _json_bytes(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": LOCAL_API_MODEL,
                                "object": "model",
                                "created": 0,
                                "owned_by": "longweis",
                            }
                        ],
                    }
                ),
            )
        if method != "POST" or request_path != "/v1/chat/completions":
            return LocalApiResponse(
                405,
                "application/json",
                _json_bytes(_error_payload("method_not_allowed", "Method not allowed.")),
            )
        if len(body) > _MAX_REQUEST_BYTES:
            return LocalApiResponse(
                413,
                "application/json",
                _json_bytes(_error_payload("request_too_large", "Request is too large.")),
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if not isinstance(payload, Mapping):
            return _invalid_chat("Invalid chat completion request.")
        unsupported_fields = sorted(set(payload) - _CHAT_FIELDS)
        if unsupported_fields:
            # Field names are safe to expose and make interoperability failures
            # diagnosable without logging prompt content, credentials, or tool
            # definitions.  Values remain deliberately undisclosed.
            return _invalid_chat(
                "Unsupported chat completion parameter: %s."
                % ", ".join(unsupported_fields)
            )
        stream = payload.get("stream", False)
        if payload.get("model") != LOCAL_API_MODEL or not isinstance(stream, bool):
            return _invalid_chat("Use model relay-auto and a boolean stream value.")
        try:
            controls = self._chat_controls(payload)
        except ValidationError as error:
            return _invalid_chat(str(error))
        stream_include_usage = bool(controls.pop("stream_include_usage", False))
        if not self._admission.acquire(timeout=_ADMISSION_WAIT_SECONDS):
            return LocalApiResponse(
                429,
                "application/json",
                _json_bytes(
                    _error_payload(
                        "local_busy",
                        "Local Relay remained busy beyond the bounded wait.",
                    )
                ),
            )
        try:
            bridge = self._bridge_factory(profile, self.credential_store.read)
            request_id = self._request_id(normalized_headers)
            tool_passthrough = self._requires_openai_passthrough(payload)
            if tool_passthrough:
                direct_request = {"messages": payload.get("messages")}
                for field in (
                    "tools",
                    "tool_choice",
                    "parallel_tool_calls",
                    "temperature",
                    "top_p",
                ):
                    if payload.get(field) is not None:
                        direct_request[field] = payload[field]
                if "max_output_tokens" in controls:
                    direct_request["max_tokens"] = controls["max_output_tokens"]
                result = bridge.complete_openai_chat(
                    direct_request, request_id=request_id
                )
            else:
                result = bridge.complete(
                    request_id=request_id,
                    mode=profile.default_mode,
                    messages=payload.get("messages"),
                    memory_budget_tokens=profile.memory_budget_tokens,
                    **controls,
                )
        except ValidationError:
            return _invalid_chat("Invalid chat completion request.")
        except ProviderResponseError as exc:
            return LocalApiResponse(
                502,
                "application/json",
                _json_bytes(
                    _error_payload(
                        "upstream_failed",
                        "The model request could not be completed: %s" % str(exc),
                    )
                ),
            )
        except Exception:
            return LocalApiResponse(
                500,
                "application/json",
                _json_bytes(_error_payload("local_error", "Local Relay failed safely.")),
            )
        finally:
            self._admission.release()
        try:
            record_usage = getattr(bridge, "record_usage", None)
            # Keep Relay response usage separate from optional premium usage.
            usage = (
                getattr(result, "premium_usage", None)
                if tool_passthrough
                else result.premium_usage
            )
            if tool_passthrough and usage is None:
                usage = result.usage
            action = str(result.relay_action)
            if callable(record_usage):
                record_usage(request_id, usage, action)
        except Exception:
            # The response path must remain independent of optional display
            # telemetry; retrying later cannot change the model result.
            pass
        if tool_passthrough:
            completion = self._openai_completion_payload(
                result, request_id, profile.default_mode, int(time.time())
            )
        else:
            completion = self._completion_payload(result, int(time.time()))
        if stream is True:
            return LocalApiResponse(
                200,
                "text/event-stream",
                (
                    self._openai_event_stream(completion, stream_include_usage)
                    if tool_passthrough
                    else self._event_stream(completion, stream_include_usage)
                ),
            )
        return LocalApiResponse(200, "application/json", _json_bytes(completion))


class _LoopbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_local_api_server(
    application: LocalOpenAIApplication, host: str, port: int
) -> ThreadingHTTPServer:
    if host != LOCAL_API_HOST:
        raise ValidationError("local API must bind to literal IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        server_version = "LongweisRelayLocal/0.61"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format_string, *arguments):
            del format_string, arguments

        def _dispatch(self) -> None:
            content_length = self.headers.get("Content-Length", "0")
            try:
                length = int(content_length)
            except ValueError:
                length = -1
            if length < 0 or length > application.maximum_request_bytes_for(self.path):
                response = LocalApiResponse(
                    413,
                    "application/json",
                    _json_bytes(
                        _error_payload("request_too_large", "Request is too large.")
                    ),
                )
            else:
                body = self.rfile.read(length) if length else b""
                response = application.handle(
                    self.command,
                    self.path,
                    dict(self.headers.items()),
                    body,
                )
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if response.stream is None:
                self.send_header("Content-Length", str(len(response.body)))
            else:
                # Chunked framing lets ordinary OpenAI-compatible callers
                # receive upstream SSE events while the loopback stays open.
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if response.stream is None:
                self.wfile.write(response.body)
                return
            try:
                for chunk in response.stream:
                    if not isinstance(chunk, bytes):
                        raise ProviderResponseError("invalid local API stream chunk")
                    if not chunk:
                        continue
                    self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # A user cancellation is normal.  The upstream generator is
                # closed below and no incomplete turn is cached.
                pass
            finally:
                if response.close_stream is not None:
                    response.close_stream()

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch

    try:
        return _LoopbackHttpServer((host, port), Handler)
    except OSError:
        raise ValidationError("local API port is unavailable") from None


class LocalApiController:
    """Owns one in-process loopback server and its clean lifecycle."""

    def __init__(
        self,
        profile_store: LocalBridgeProfileStore,
        credential_store: Optional[object] = None,
        bridge_factory: Optional[BridgeFactory] = None,
        host: str = LOCAL_API_HOST,
        port: int = LOCAL_API_PORT,
        maximum_in_flight: int = 2,
    ) -> None:
        if host != LOCAL_API_HOST:
            raise ValidationError("local API must use literal IPv4 loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
            raise ValidationError("local API port is invalid")
        self.profile_store = profile_store
        self.credential_store = credential_store or default_credential_store()
        self.bridge_factory = bridge_factory
        self.host = host
        self.port = port
        self.maximum_in_flight = maximum_in_flight
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "LocalApiController(host=%r, port=%r, running=%r)" % (
            self.host,
            self.port,
            self.status().running,
        )

    def status(self) -> LocalApiStatus:
        with self._lock:
            running = bool(self._thread is not None and self._thread.is_alive())
            port = int(self._server.server_address[1]) if self._server else self.port
        return LocalApiStatus(
            running=running,
            host=self.host,
            port=port,
            base_url="http://%s:%d/v1" % (self.host, port),
        )

    def start(self) -> LocalApiStatus:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if self._server is not None:
                self._server.server_close()
                self._server = None
                self._thread = None
            profile = self.profile_store.load()
            required_references = (
                profile.relay_credential_reference,
                profile.premium_credential_reference,
                profile.local_api_credential_reference,
            )
            if not all(self.credential_store.contains(reference) for reference in required_references):
                raise ValidationError(
                    "Relay, provider, and local API secure-store credentials are required"
                )
            application = LocalOpenAIApplication(
                self.profile_store,
                self.credential_store,
                self.bridge_factory,
                self.maximum_in_flight,
            )
            server = make_local_api_server(application, self.host, self.port)
            thread = threading.Thread(
                target=server.serve_forever,
                name="longweis-relay-local-api",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
        return self.status()

    def stop(self) -> LocalApiStatus:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise ValidationError("local API did not stop")
        return self.status()
