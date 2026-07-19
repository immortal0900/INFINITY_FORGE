"""Read Codex subscription state through the Hermes App Server adapter."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .subscription_runtime import CodexSubscriptionSnapshot


_WINDOWS_NATIVE_TARGETS = {
    "amd64": ("codex-win32-x64", "x86_64-pc-windows-msvc"),
    "x86_64": ("codex-win32-x64", "x86_64-pc-windows-msvc"),
    "arm64": ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
    "aarch64": ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
}


class ProbeError(RuntimeError):
    """Raised when the structured Codex subscription probe cannot be trusted."""


def _default_client_factory(**kwargs: object) -> Any:
    """Import Hermes only when the probe is actually used."""

    from agent.transports.codex_app_server import CodexAppServerClient

    return CodexAppServerClient(**kwargs)


class CodexAppServerProbe:
    """Read only the two App Server endpoints used for quota classification."""

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    def probe(
        self,
        codex_bin: str,
        env: Mapping[str, str],
        timeout: float = 10.0,
    ) -> CodexSubscriptionSnapshot:
        """Return a validated subscription snapshot without invoking a model or tool."""

        resolved_codex_bin = _resolve_codex_bin(codex_bin)
        try:
            # RISK(security): this opens the external Codex auth/process boundary.
            # Copy the caller's environment without injecting credentials or mutating it.
            client_context = self._client_factory(
                codex_bin=resolved_codex_bin, env=dict(env)
            )
        except ImportError as error:
            raise ProbeError("Codex App Server client import failed") from error
        except Exception as error:
            raise ProbeError("Codex App Server client setup failed") from error

        try:
            with client_context as client:
                try:
                    client.initialize(
                        client_name="infinity_forge_subscription_probe",
                        client_title="Infinity Forge Subscription Probe",
                        client_version="1.0",
                        timeout=timeout,
                    )
                except Exception as error:
                    raise ProbeError("Codex App Server initialize failed") from error
                try:
                    account_response = client.request(
                        "account/read", {"refreshToken": False}, timeout=timeout
                    )
                except Exception as error:
                    raise ProbeError("Codex App Server account/read failed") from error
                try:
                    limits_response = client.request(
                        "account/rateLimits/read", {}, timeout=timeout
                    )
                except Exception as error:
                    raise ProbeError(
                        "Codex App Server account/rateLimits/read failed"
                    ) from error
        except ProbeError:
            raise
        except Exception as error:
            raise ProbeError("Codex App Server client lifecycle failed") from error

        return _parse_snapshot(account_response, limits_response)


def _parse_snapshot(
    account_response: object, limits_response: object
) -> CodexSubscriptionSnapshot:
    account_payload = _require_mapping(account_response, "account response")
    account = _require_mapping(account_payload.get("account"), "account")
    account_type = account.get("type")
    if not isinstance(account_type, str):
        raise ProbeError("Codex App Server account response is invalid")

    plan_type = account.get("planType")
    if plan_type is not None and not isinstance(plan_type, str):
        raise ProbeError("Codex App Server account response is invalid")

    limits_payload = _require_mapping(limits_response, "rate limits response")
    rate_limits = _require_mapping(limits_payload.get("rateLimits"), "rateLimits")
    reached_type = rate_limits.get("rateLimitReachedType")
    if reached_type is not None and not isinstance(reached_type, str):
        raise ProbeError("Codex App Server rate limits response is invalid")

    spend_control_reached = rate_limits.get("spendControlReached", False)
    if not isinstance(spend_control_reached, bool):
        raise ProbeError("Codex App Server rate limits response is invalid")

    return CodexSubscriptionSnapshot(
        account_type=account_type,
        plan_type=plan_type,
        rate_limit_reached_type=reached_type,
        spend_control_reached=spend_control_reached,
    )


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbeError(f"Codex App Server {label} is invalid")
    return value


def _resolve_codex_bin(codex_bin: str) -> str:
    """Resolve Windows bare names to a directly launchable native executable."""

    if not sys.platform.startswith("win"):
        return codex_bin

    suffix = codex_bin.lower()
    if suffix.endswith((".cmd", ".bat", ".ps1")):
        raise ProbeError("Codex App Server requires a native .exe executable on Windows")
    if os.path.isabs(codex_bin):
        if suffix.endswith(".exe"):
            return codex_bin
        raise ProbeError("Codex App Server requires a native .exe executable on Windows")

    # RISK(security): resolve only an absolute native executable for direct Popen use.
    native_executable = shutil.which("codex.exe")
    if (
        native_executable is None
        or not native_executable.lower().endswith(".exe")
        or not os.path.isabs(native_executable)
    ):
        raise ProbeError("Codex App Server requires a native .exe executable on Windows")
    if "\\windowsapps\\" in native_executable.replace("/", "\\").lower():
        npm_native_executable = _npm_native_executable()
        if npm_native_executable is not None:
            return npm_native_executable
        raise ProbeError("Codex App Server requires a native .exe executable on Windows")
    return native_executable


def _npm_native_executable() -> str | None:
    """Return the exact native executable next to the resolved npm shim."""

    shim = shutil.which("codex.cmd")
    if shim is None:
        return None
    shim_path = Path(shim)
    if not shim_path.is_absolute():
        return None
    target = _WINDOWS_NATIVE_TARGETS.get(_machine_name().lower())
    if target is None:
        return None
    package, vendor = target
    try:
        resolved_shim = shim_path.resolve(strict=True)
        if not resolved_shim.is_file() or resolved_shim.suffix.lower() != ".cmd":
            return None
        package_root = (
            resolved_shim.parent
            / "node_modules"
            / "@openai"
            / "codex"
        ).resolve(strict=True)
        candidate = (
            package_root
            / "node_modules"
            / "@openai"
            / package
            / "vendor"
            / vendor
            / "bin"
            / "codex.exe"
        ).resolve(strict=True)
    except OSError:
        return None
    if (
        not package_root.is_dir()
        or not candidate.is_file()
        or not candidate.is_relative_to(package_root)
    ):
        return None
    # RISK(security): only this resolved shim-relative native path may cross Popen.
    return str(candidate)


def _machine_name() -> str:
    """Return the host architecture through a private test seam."""

    return platform.machine()
