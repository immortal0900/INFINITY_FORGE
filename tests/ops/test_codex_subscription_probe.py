from __future__ import annotations

from collections.abc import Mapping
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

    snapshot = CodexAppServerProbe(factory).probe("codex", environment, 1)

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
    assert factory_calls == [{"codex_bin": "codex", "env": {"PATH": "kept"}}]
    assert factory_calls[0]["env"] is not environment
    assert environment == {"PATH": "kept"}
    assert client.closed is True


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
