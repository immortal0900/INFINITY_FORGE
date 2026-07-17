from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import forge.hermes_plugin.infinity_forge as plugin


@pytest.fixture(autouse=True)
def _restore_sys_path() -> Iterator[None]:
    original = sys.path.copy()
    yield
    sys.path[:] = original


def _make_release(local_app_data: Path, sha: str = "a" * 40) -> Path:
    release = local_app_data / "InfinityForge" / "releases" / sha
    (release / "forge" / "ops").mkdir(parents=True)
    (release / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (release / "forge" / "ops" / "task_setup.py").write_text(
        "", encoding="utf-8"
    )
    return release


def _make_plugin_file(tmp_path: Path, pointer: str | None) -> Path:
    plugin_file = tmp_path / "plugin" / "__init__.py"
    plugin_file.parent.mkdir()
    plugin_file.write_text("", encoding="utf-8")
    if pointer is not None:
        (plugin_file.parent / "release-path.txt").write_text(
            pointer, encoding="utf-8"
        )
    return plugin_file


def test_missing_pointer_keeps_existing_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_file = _make_plugin_file(tmp_path, pointer=None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    before = sys.path.copy()

    result = plugin._activate_managed_release(plugin_file)

    assert result is None
    assert sys.path == before


def test_valid_pointer_prepends_exact_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "Local"
    release = _make_release(local_app_data)
    plugin_file = _make_plugin_file(tmp_path, pointer=str(release))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    result = plugin._activate_managed_release(plugin_file)

    assert result == release.resolve()
    assert Path(sys.path[0]) == release.resolve()


def test_repeated_activation_does_not_duplicate_release_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "Local"
    release = _make_release(local_app_data)
    plugin_file = _make_plugin_file(tmp_path, pointer=str(release))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    plugin._activate_managed_release(plugin_file)
    plugin._activate_managed_release(plugin_file)

    assert sys.path.count(str(release.resolve())) == 1
    assert Path(sys.path[0]) == release.resolve()


@pytest.mark.parametrize("pointer", ["", "relative/release"])
def test_non_absolute_pointer_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer: str,
) -> None:
    plugin_file = _make_plugin_file(tmp_path, pointer=pointer)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    with pytest.raises(RuntimeError, match="managed release pointer"):
        plugin._activate_managed_release(plugin_file)


def test_pointer_outside_managed_release_root_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _make_release(tmp_path / "Outside")
    plugin_file = _make_plugin_file(tmp_path, pointer=str(outside))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    with pytest.raises(RuntimeError, match="outside managed release root"):
        plugin._activate_managed_release(plugin_file)


@pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "g" * 40])
def test_malformed_release_sha_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sha: str,
) -> None:
    local_app_data = tmp_path / "Local"
    release = _make_release(local_app_data, sha=sha)
    plugin_file = _make_plugin_file(tmp_path, pointer=str(release))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    with pytest.raises(RuntimeError, match="40-character lowercase Git SHA"):
        plugin._activate_managed_release(plugin_file)


def test_incomplete_release_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "Local"
    release = local_app_data / "InfinityForge" / "releases" / ("b" * 40)
    release.mkdir(parents=True)
    plugin_file = _make_plugin_file(tmp_path, pointer=str(release))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    with pytest.raises(RuntimeError, match="managed release is incomplete"):
        plugin._activate_managed_release(plugin_file)
