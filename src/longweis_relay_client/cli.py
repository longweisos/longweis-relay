"""Command-line entry point for the auditable Relay client."""

import argparse
import getpass
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, Optional, Sequence
from urllib.parse import urlsplit

from . import __version__
from .client_auth import RelayAuthClient
from .client_bridge import PUBLIC_CLIENT_PROTOCOL
from .client_credentials import default_credential_store
from .client_errors import ProjectSError, ProviderResponseError, ValidationError
from .client_local_api import LocalApiController
from .client_native_preflight import native_client_preflight
from .client_profile import LocalBridgeProfileStore, default_client_profile_path
from .client_setup import LocalBridgeSettingsService
from .egress import egress_inventory


RELAY_BASE_URL = "https://api.longweisos.com/v1"
RELAY_HEALTH_URL = "https://api.longweisos.com/healthz"
DEFAULT_PROFILE_ID = "default"
DEFAULT_RELAY_REFERENCE = DEFAULT_PROFILE_ID + "/relay"
_MAX_PUBLIC_RESPONSE_BYTES = 64 * 1024


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _public_json(url: str) -> Dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "longweis-relay-client/%s" % __version__,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=15.0, context=ssl.create_default_context()
        ) as response:
            body = response.read(_MAX_PUBLIC_RESPONSE_BYTES + 1)
            status = int(response.status)
    except (OSError, urllib.error.URLError):
        raise ProviderResponseError("Relay public preflight is unavailable") from None
    if status != 200 or len(body) > _MAX_PUBLIC_RESPONSE_BYTES:
        raise ProviderResponseError("Relay public preflight returned an invalid response")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProviderResponseError("Relay public preflight returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise ProviderResponseError("Relay public preflight returned an invalid object")
    return payload


def _settings_service(profile_path: Optional[str] = None) -> LocalBridgeSettingsService:
    return LocalBridgeSettingsService(
        LocalBridgeProfileStore(profile_path or default_client_profile_path())
    )


def _store_relay_key(payload: Dict[str, object]) -> Dict[str, object]:
    relay_key = payload.get("relay_key")
    if not isinstance(relay_key, str) or not relay_key.startswith("psk_test_"):
        raise ProviderResponseError("Relay sign-in did not return a valid account session")
    store = default_credential_store()
    if not store.available:
        raise ValidationError("the operating-system credential store is unavailable")
    store.store(DEFAULT_RELAY_REFERENCE, relay_key)
    account = payload.get("account")
    balance = payload.get("balance")
    return {
        "status": "signed_in",
        "credential_storage": getattr(store, "storage_id", "secure_store"),
        "credential_reference": DEFAULT_RELAY_REFERENCE,
        "credential_printed": False,
        "account": account if isinstance(account, dict) else None,
        "balance": balance if isinstance(balance, dict) else None,
    }


def _command_doctor(arguments: argparse.Namespace) -> int:
    native = dict(native_client_preflight())
    native_ready = native.get("status") == "native_preflight_ready"
    result: Dict[str, object] = {
        "status": "ready" if native_ready else "attention_required",
        "offline": bool(arguments.offline),
        "native": native,
        "relay_origin": "https://api.longweisos.com",
        "credentials_read": False,
        "credentials_printed": False,
    }
    if arguments.offline:
        _print(result)
        return 0 if native_ready else 1

    health = _public_json(RELAY_HEALTH_URL)
    methods = RelayAuthClient(RELAY_BASE_URL).methods()
    promotion = methods.get("promotion")
    protocols = methods.get("client_protocols")
    protocol_ready = (
        isinstance(protocols, list) and PUBLIC_CLIENT_PROTOCOL in protocols
    )
    valid_promotion = isinstance(promotion, dict) and (
        promotion.get("signup_credit_usd") == "10.00"
        and promotion.get("stage") == "public_launch"
    )
    registration_ready = (
        health.get("status") == "ok"
        and methods.get("email") is True
        and methods.get("google") is True
        and methods.get("invite_only") is False
        and methods.get("topups") is True
        and valid_promotion
        and protocol_ready
    )
    result["public_service"] = {
        "healthy": health.get("status") == "ok",
        "email_registration": methods.get("email") is True,
        "google_registration": methods.get("google") is True,
        "open_registration": methods.get("invite_only") is False,
        "signup_credit_usd": (
            promotion.get("signup_credit_usd") if isinstance(promotion, dict) else None
        ),
        "production_topups": methods.get("topups") is True,
        "public_client_protocol_v1": protocol_ready,
    }
    result["status"] = (
        "ready" if registration_ready and native_ready else "attention_required"
    )
    _print(result)
    return 0 if result["status"] == "ready" else 1


def _command_egress(unused: argparse.Namespace) -> int:
    del unused
    _print({"status": "auditable", "destinations": egress_inventory()})
    return 0


def _command_login_email(arguments: argparse.Namespace) -> int:
    email = arguments.email or input("Email: ").strip()
    if not email:
        raise ValidationError("email is required")
    client = RelayAuthClient(RELAY_BASE_URL)
    challenge = client.begin_email(email)
    code = getpass.getpass("Email verification code: ").strip()
    receipt = client.complete_email(str(challenge["challenge_id"]), code)
    _print(_store_relay_key(dict(receipt)))
    return 0


def _google_authorization_url(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderResponseError("Relay returned an invalid Google authorization URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "accounts.google.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProviderResponseError("Relay returned an invalid Google authorization URL")
    return value


def _command_login_google(arguments: argparse.Namespace) -> int:
    client = RelayAuthClient(RELAY_BASE_URL)
    flow = client.begin_oauth("google")
    authorization_url = _google_authorization_url(flow.get("authorization_url"))
    if not webbrowser.open(authorization_url):
        print("Open this Google authorization URL in your browser:\n%s" % authorization_url)
    deadline = time.monotonic() + float(arguments.timeout)
    while time.monotonic() < deadline:
        result = client.poll_oauth(str(flow["flow_id"]), str(flow["poll_token"]))
        if result.get("object") != "longweis_relay.oauth_poll":
            _print(_store_relay_key(dict(result)))
            return 0
        time.sleep(2.0)
    raise ProviderResponseError("Google sign-in timed out before completion")


def _command_configure(arguments: argparse.Namespace) -> int:
    premium_key = getpass.getpass("Premium-provider API key: ").strip()
    if not premium_key:
        raise ValidationError("premium-provider API key is required")
    service = _settings_service(arguments.profile_path)
    receipt = service.save(
        profile_id=DEFAULT_PROFILE_ID,
        relay_base_url=RELAY_BASE_URL,
        provider_id=arguments.provider,
        model_id=arguments.model,
        custom_base_url=arguments.custom_base_url,
        memory_retrieval_enabled=bool(arguments.memory),
        memory_budget_tokens=arguments.memory_budget,
        premium_key=premium_key,
    )
    _print(receipt)
    return 0


def _command_status(arguments: argparse.Namespace) -> int:
    snapshot = _settings_service(arguments.profile_path).snapshot()
    _print(snapshot.public_payload())
    return 0 if snapshot.ready else 1


def _command_show_local_key(arguments: argparse.Namespace) -> int:
    if not arguments.acknowledge_secret_output:
        raise ValidationError(
            "use --acknowledge-secret-output to print the device-local API key"
        )
    print(_settings_service(arguments.profile_path).read_local_api_key())
    return 0


def _command_serve(arguments: argparse.Namespace) -> int:
    profile = LocalBridgeProfileStore(arguments.profile_path or default_client_profile_path())
    controller = LocalApiController(profile)
    status = controller.start()
    _print({"status": "running", "base_url": status.base_url, "model": "relay-auto"})
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        controller.stop()
        return 0


def _command_reset(arguments: argparse.Namespace) -> int:
    if arguments.confirm != "remove-local-relay-setup":
        raise ValidationError(
            "reset requires --confirm remove-local-relay-setup"
        )
    _print(_settings_service(arguments.profile_path).reset())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay-client")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="run secret-free client checks")
    doctor.add_argument("--offline", action="store_true")
    doctor.set_defaults(handler=_command_doctor)

    egress = commands.add_parser("egress", help="show possible network destinations")
    egress.set_defaults(handler=_command_egress)

    email = commands.add_parser("login-email", help="register or sign in with email")
    email.add_argument("--email")
    email.set_defaults(handler=_command_login_email)

    google = commands.add_parser("login-google", help="register or sign in with Google")
    google.add_argument("--timeout", type=int, default=180)
    google.set_defaults(handler=_command_login_google)

    configure = commands.add_parser("configure", help="store provider settings securely")
    configure.add_argument("--provider", required=True)
    configure.add_argument("--model", required=True)
    configure.add_argument("--custom-base-url")
    configure.add_argument("--memory", action="store_true")
    configure.add_argument("--memory-budget", type=int, default=800)
    configure.add_argument("--profile-path")
    configure.set_defaults(handler=_command_configure)

    status = commands.add_parser("status", help="show secret-free local status")
    status.add_argument("--profile-path")
    status.set_defaults(handler=_command_status)

    show_key = commands.add_parser(
        "show-local-api-key", help="print the device-local integration key"
    )
    show_key.add_argument("--profile-path")
    show_key.add_argument("--acknowledge-secret-output", action="store_true")
    show_key.set_defaults(handler=_command_show_local_key)

    serve = commands.add_parser("serve", help="run the loopback OpenAI-compatible API")
    serve.add_argument("--profile-path")
    serve.set_defaults(handler=_command_serve)

    reset = commands.add_parser("reset", help="remove local profile and credentials")
    reset.add_argument("--profile-path")
    reset.add_argument("--confirm")
    reset.set_defaults(handler=_command_reset)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except ProjectSError as error:
        print("relay-client: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
