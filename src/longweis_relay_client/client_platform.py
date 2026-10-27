"""Platform-neutral local paths for Longweis Relay desktop clients."""

import os
import ntpath
import posixpath
import sys
from pathlib import Path
from typing import Mapping, Optional

from .client_errors import ValidationError


def normalized_client_platform(platform: Optional[str] = None) -> str:
    value = platform or sys.platform
    if value == "darwin":
        return "macos"
    if value == "win32":
        return "windows"
    if value.startswith("linux"):
        return "linux"
    raise ValidationError("this desktop platform is unsupported")


def default_client_data_dir(
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> str:
    family = normalized_client_platform(platform)
    values = environ if environ is not None else os.environ
    home_path = Path(home).expanduser() if home is not None else Path.home()
    if family == "macos":
        target = home_path / "Library" / "Application Support" / "Longweis Relay"
    elif family == "windows":
        local = values.get("LOCALAPPDATA")
        base = local or ntpath.join(str(home or Path.home()), "AppData", "Local")
        return ntpath.normpath(ntpath.join(base, "Longweis Relay"))
    else:
        configured = values.get("XDG_CONFIG_HOME")
        base = Path(configured) if configured and Path(configured).is_absolute() else home_path / ".config"
        target = base / "longweis-relay"
    return posixpath.normpath(str(target))


def default_client_runtime_dir(
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> str:
    family = normalized_client_platform(platform)
    values = environ if environ is not None else os.environ
    if family == "linux":
        configured = values.get("XDG_RUNTIME_DIR")
        if configured and Path(configured).is_absolute():
            return posixpath.normpath(str(Path(configured) / "longweis-relay"))
    data = default_client_data_dir(platform, values, home)
    if family == "windows":
        return ntpath.normpath(ntpath.join(data, "runtime"))
    return str(Path(data) / "runtime")
