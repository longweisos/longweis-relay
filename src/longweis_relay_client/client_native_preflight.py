"""Credential-free native-host readiness check for portable clients."""

import importlib
from typing import Callable, Mapping, Optional

from .client_credentials import default_credential_store
from .client_errors import ValidationError
from .client_platform import normalized_client_platform


def native_client_preflight(
    expected_platform: Optional[str] = None,
    *,
    platform: Optional[str] = None,
    credential_store: Optional[object] = None,
    tkinter_loader: Callable[[str], object] = importlib.import_module,
) -> Mapping[str, object]:
    actual = normalized_client_platform(platform)
    target = expected_platform or actual
    if target not in {"macos", "windows", "linux"}:
        raise ValidationError("native preflight target is invalid")
    if actual != target:
        raise ValidationError("this bundle must be checked on its target operating system")
    try:
        tkinter_loader("tkinter")
    except Exception:
        raise ValidationError("Python Tk support is unavailable") from None
    store = credential_store or default_credential_store(platform)
    if not bool(getattr(store, "available", False)):
        raise ValidationError("the operating system secure credential store is unavailable")
    return {
        "status": "native_preflight_ready",
        "target_platform": target,
        "python_tk_available": True,
        "credential_storage": str(getattr(store, "storage_id", "secure_store")),
        "credential_store_available": True,
        "credentials_read": False,
        "credentials_written": False,
        "network_calls": 0,
    }
