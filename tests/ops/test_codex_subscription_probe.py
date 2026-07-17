from __future__ import annotations

import shutil
import sys
import platform
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from forge.ops.codex_subscription_probe import CodexAppServerProbe, ProbeError


class FakeClient:
    def __init__(self, results: Mapping[str, object]) -> None:
        self.results = results
        self.methods: list[str] = []
        self.initialize_kwargs: dict[str, object] | None = None
        self.request_calls: list[tuple[str, object, object]] = []
        self.closed = False
        self.failure: Exception | None = None

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def initialize(self, **kwargs: object) -> None:
        self.methods.append("initialize")
        self.initialize_kwargs = kwargs
        if self.failure is not None:
            raise self.failure

    def request(
        self, method: str, params: object = None, timeout: object = None
    ) -> dict[str, object]:
        self.methods.append(method)
        self.request_calls.append((method, params, timeout))
        if self.failure is not None:
            raise self.failure
        result = self.results[method]
        assert isinstance(result, dict)
        return result


def _npm_native_executable(
    prefix: Path, package: str, vendor: str
) -> tuple[Path, Path]:
    shim = prefix / "codex.cmd"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.touch()
    executable = (
        prefix
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / package
        / "vendor"
        / vendor
        / "bin"
        / "codex.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.touch()
    return shim, executable


def test_probe_reads_account_and_rate_limits() -> None:
    client = FakeClient(
        results={
            "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
            "account/rateLimits/read": {
                "rateLimits": {"rateLimitReachedType": "primary"},
            },
        }
    )
    environment = {"PATH": "kept"}
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    snapshot = CodexAppServerProbe(factory).probe(
        r"C:\\tools\\codex.exe", environment, 1
    )

    assert snapshot.account_type == "chatgpt"
    assert snapshot.plan_type == "plus"
    assert snapshot.rate_limit_reached_type == "primary"
    assert snapshot.spend_control_reached is False
    assert client.methods == ["initialize", "account/read", "account/rateLimits/read"]
    assert client.initialize_kwargs == {
        "client_name": "infinity_forge_subscription_probe",
        "client_title": "Infinity Forge Subscription Probe",
        "client_version": "1.0",
        "timeout": 1,
    }
    assert client.request_calls == [
        ("account/read", {"refreshToken": False}, 1),
        ("account/rateLimits/read", {}, 1),
    ]
    assert factory_calls == [
        {"codex_bin": r"C:\\tools\\codex.exe", "env": {"PATH": "kept"}}
    ]
    assert factory_calls[0]["env"] is not environment
    assert environment == {"PATH": "kept"}
    assert client.closed is True


def test_windows_probe_resolves_bare_codex_to_native_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    factory_calls: list[dict[str, object]] = []
    environment = {"PATH": "kept"}

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: r"C:\\tools\\codex.exe",
    )

    CodexAppServerProbe(factory).probe("codex", environment, 1)

    assert factory_calls == [
        {
            "codex_bin": r"C:\\tools\\codex.exe",
            "env": {"PATH": "kept"},
        }
    ]
    assert factory_calls[0]["env"] is not environment
    assert environment == {"PATH": "kept"}


def test_windows_probe_keeps_explicit_native_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: pytest.fail("explicit executable must not be resolved"),
    )

    CodexAppServerProbe(factory).probe(r"C:\\tools\\codex.exe", {}, 1)

    assert factory_calls[0]["codex_bin"] == r"C:\\tools\\codex.exe"


def test_windows_probe_uses_npm_native_executable_when_which_finds_windows_apps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    shim, vendor_executable = _npm_native_executable(
        tmp_path / "trusted-npm",
        "codex-win32-x64",
        "x86_64-pc-windows-msvc",
    )
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            r"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\codex.exe"
            if name == "codex.exe"
            else str(shim) if name == "codex.cmd" else None
        ),
    )
    monkeypatch.setenv("APPDATA", str(tmp_path / "untrusted"))

    CodexAppServerProbe(factory).probe("codex", {}, 1)

    assert factory_calls[0]["codex_bin"] == str(vendor_executable)


def test_windows_probe_ignores_lookalike_and_wrong_arch_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    prefix = tmp_path / "npm"
    shim, expected_executable = _npm_native_executable(
        prefix, "codex-win32-x64", "x86_64-pc-windows-msvc"
    )
    _, wrong_arch_executable = _npm_native_executable(
        prefix, "codex-win32-arm64", "aarch64-pc-windows-msvc"
    )
    lookalike = (
        tmp_path
        / "lookalike"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    lookalike.parent.mkdir(parents=True)
    lookalike.touch()
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            r"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\codex.exe"
            if name == "codex.exe"
            else str(shim) if name == "codex.cmd" else None
        ),
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    CodexAppServerProbe(factory).probe("codex", {}, 1)

    assert factory_calls[0]["codex_bin"] == str(expected_executable)
    assert factory_calls[0]["codex_bin"] != str(wrong_arch_executable)
    assert factory_calls[0]["codex_bin"] != str(lookalike)


def test_windows_probe_rejects_unsupported_machine_architecture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shim, _ = _npm_native_executable(
        tmp_path / "npm", "codex-win32-x64", "x86_64-pc-windows-msvc"
    )

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "mips64")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            r"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\codex.exe"
            if name == "codex.exe"
            else str(shim) if name == "codex.cmd" else None
        ),
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    with pytest.raises(ProbeError, match="native .exe"):
        CodexAppServerProbe().probe("codex", {}, 1)


def test_windows_probe_rejects_native_executable_resolving_outside_package_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shim, executable = _npm_native_executable(
        tmp_path / "npm", "codex-win32-x64", "x86_64-pc-windows-msvc"
    )
    outside = tmp_path / "outside.exe"
    outside.touch()
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        resolved = original_resolve(path, strict=strict)
        if resolved == executable:
            return outside
        return resolved

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            r"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\codex.exe"
            if name == "codex.exe"
            else str(shim) if name == "codex.cmd" else None
        ),
    )
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    with pytest.raises(ProbeError, match="native .exe"):
        CodexAppServerProbe().probe("codex", {}, 1)


def test_windows_probe_rejects_unlaunchable_windows_apps_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        raise AssertionError("unlaunchable executable must be rejected")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            r"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\codex.exe"
            if name == "codex.exe"
            else None
        ),
    )

    with pytest.raises(ProbeError, match="native .exe"):
        CodexAppServerProbe(factory).probe("codex", {}, 1)

    assert factory_calls == []


@pytest.mark.parametrize("script_path", [r"C:\\tools\\codex.cmd", r"C:\\tools\\codex.bat", r"C:\\tools\\codex.ps1"])
def test_windows_probe_rejects_script_shims_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, script_path: str
) -> None:
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        raise AssertionError("script shim must be rejected before client creation")

    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(ProbeError, match="native .exe") as raised:
        CodexAppServerProbe(factory).probe(script_path, {}, 1)

    assert script_path not in str(raised.value)
    assert factory_calls == []


def test_windows_probe_fails_closed_when_native_executable_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        raise AssertionError("client must not be created without a native executable")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(ProbeError, match="native .exe"):
        CodexAppServerProbe(factory).probe("codex", {}, 1)

    assert factory_calls == []


def test_non_windows_probe_preserves_bare_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        factory_calls.append(kwargs)
        return client

    monkeypatch.setattr(sys, "platform", "linux")

    CodexAppServerProbe(factory).probe("codex", {}, 1)

    assert factory_calls[0]["codex_bin"] == "codex"


@pytest.mark.parametrize(
    ("account", "rate_limits"),
    [
        ({"type": "chatgpt"}, {}),
        ({"type": "chatgpt", "planType": None}, {"rateLimitReachedType": None}),
    ],
)
def test_probe_normalizes_optional_subscription_fields(
    account: dict[str, object], rate_limits: dict[str, object]
) -> None:
    client = FakeClient(
        {
            "account/read": {"account": account},
            "account/rateLimits/read": {"rateLimits": rate_limits},
        }
    )

    snapshot = CodexAppServerProbe(lambda **_: client).probe("codex", {}, 1)

    assert snapshot.plan_type is None
    assert snapshot.rate_limit_reached_type is None
    assert snapshot.spend_control_reached is False


@pytest.mark.parametrize(
    ("account_response", "limits_response"),
    [
        ({}, {"rateLimits": {}}),
        ({"account": {}}, {"rateLimits": {}}),
        ({"account": {"type": 42}}, {"rateLimits": {}}),
        ({"account": {"type": "chatgpt", "planType": 42}}, {"rateLimits": {}}),
        ({"account": {"type": "chatgpt"}}, {}),
        ({"account": {"type": "chatgpt"}}, {"rateLimits": {"rateLimitReachedType": 42}}),
        ({"account": {"type": "chatgpt"}}, {"rateLimits": {"spendControlReached": "false"}}),
    ],
)
def test_probe_rejects_malformed_structured_responses(
    account_response: dict[str, object], limits_response: dict[str, object]
) -> None:
    client = FakeClient(
        {
            "account/read": account_response,
            "account/rateLimits/read": limits_response,
        }
    )

    with pytest.raises(ProbeError):
        CodexAppServerProbe(lambda **_: client).probe("codex", {}, 1)


@pytest.mark.parametrize("operation", ["initialize", "account/read", "account/rateLimits/read"])
def test_probe_redacts_client_operation_errors(operation: str) -> None:
    client = FakeClient(
        {
            "account/read": {"account": {"type": "chatgpt"}},
            "account/rateLimits/read": {"rateLimits": {}},
        }
    )
    original_request = client.request
    secret = "account-id-should-not-appear"

    if operation == "initialize":
        def fail_initialize(**kwargs: object) -> None:
            raise TimeoutError(secret)

        client.initialize = fail_initialize  # type: ignore[method-assign]
    else:
        def fail_request(
            method: str, params: object = None, timeout: object = None
        ) -> dict[str, object]:
            if method == operation:
                raise TimeoutError(secret)
            return original_request(method, params, timeout)

        client.request = fail_request  # type: ignore[method-assign]

    with pytest.raises(ProbeError) as raised:
        CodexAppServerProbe(lambda **_: client).probe("codex", {}, 1)

    assert operation in str(raised.value)
    assert secret not in str(raised.value)
    assert isinstance(raised.value.__cause__, TimeoutError)


def test_probe_redacts_client_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(**kwargs: object) -> Any:
        raise ImportError("secret installation path")

    monkeypatch.setattr(
        "forge.ops.codex_subscription_probe._default_client_factory", fail_import
    )

    with pytest.raises(ProbeError) as raised:
        CodexAppServerProbe().probe("codex", {}, 1)

    assert str(raised.value) == "Codex App Server client import failed"
    assert "secret installation path" not in str(raised.value)
    assert isinstance(raised.value.__cause__, ImportError)
