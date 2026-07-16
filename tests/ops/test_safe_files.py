from __future__ import annotations

from dataclasses import replace

import pytest

from forge.ops.safe_files import (
    AUTO_MERGE_ALLOWED,
    CHECK_ERROR,
    MANUAL_MERGE_REQUIRED,
    ChangedFile,
    SafeFilesDecision,
    SafeFilesEvidence,
    SafeFilesResult,
    check_safe_files,
)


BASE_COMMIT = "0" * 40
HEAD_COMMIT = "a" * 40


def changed(
    path: str,
    *,
    status: str = "modified",
    is_text: bool | None = True,
    file_type: str | None = "file",
    data_complete: bool = True,
    patch_complete: bool = True,
    tree_entry_complete: bool = True,
) -> ChangedFile:
    return ChangedFile(
        path=path,
        status=status,
        is_text=is_text,
        file_type=file_type,
        data_complete=data_complete,
        patch_complete=patch_complete,
        tree_entry_complete=tree_entry_complete,
    )


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("docs/guide.md", "added"),
        ("docs/guide.md", "modified"),
        ("README.md", "added"),
        ("README.ko.md", "modified"),
        ("CHANGELOG.md", "added"),
        ("CHANGELOG-2026.md", "modified"),
        ("tests/test_api.py", "added"),
        ("test/unit/test_api.py", "added"),
    ],
)
def test_allowed_text_changes_are_auto_mergeable(path: str, status: str) -> None:
    result = check_safe_files(
        [changed(path, status=status)], pagination_complete=True
    )

    assert isinstance(result, SafeFilesResult)
    assert isinstance(result, SafeFilesDecision)
    assert result.code == AUTO_MERGE_ALLOWED
    assert result.allowed is True
    assert result.paths == (path,)


@pytest.mark.parametrize(
    "path",
    [
        "README",
        "CHANGELOG",
        "README.py",
        "README.sh",
        "README.yml",
        "CHANGELOG.py",
    ],
)
def test_root_readme_and_changelog_require_document_extensions(path: str) -> None:
    result = check_safe_files(
        [changed(path, status="added")], pagination_complete=True
    )

    assert result.code == MANUAL_MERGE_REQUIRED


def test_every_changed_file_must_be_safe() -> None:
    result = check_safe_files(
        [changed("docs/guide.md"), changed("src/service.py")],
        pagination_complete=True,
    )

    assert result.code == MANUAL_MERGE_REQUIRED
    assert result.allowed is False
    assert result.paths == ("src/service.py",)


@pytest.mark.parametrize("status", ["removed", "renamed", "copied"])
def test_delete_rename_and_copy_require_manual_merge(status: str) -> None:
    result = check_safe_files(
        [changed("docs/guide.md", status=status)], pagination_complete=True
    )

    assert result.code == MANUAL_MERGE_REQUIRED


@pytest.mark.parametrize(
    "item",
    [
        changed("docs/image.png", is_text=False),
        changed("docs/link.md", file_type="symlink"),
        changed("docs/vendor.md", file_type="submodule"),
    ],
)
def test_binary_symlink_and_submodule_require_manual_merge(item: ChangedFile) -> None:
    result = check_safe_files([item], pagination_complete=True)

    assert result.code == MANUAL_MERGE_REQUIRED


def test_existing_test_change_requires_manual_merge() -> None:
    result = check_safe_files(
        [changed("tests/test_api.py", status="modified")],
        pagination_complete=True,
    )

    assert result.code == MANUAL_MERGE_REQUIRED


@pytest.mark.parametrize(
    "path",
    [
        "src/service.py",
        "requirements.txt",
        "uv.lock",
        "migrations/001.sql",
        "db/schema.sql",
        ".github/workflows/eval.yml",
        "Dockerfile",
        "systemd/forge.service",
        "deploy/production.yml",
        "infra/main.tf",
        "docs/security.md",
        "docs/secrets.md",
        "docs/permissions.md",
        "docs/oauth.md",
        "docs/token.md",
        "docs/password.md",
        "docs/helm/values.md",
        "docs/github-actions.md",
        "AGENTS.md",
        "nested/AGENTS.md",
        "forge/skills/example/SKILL.md",
        ".codex/config.toml",
        ".claude/settings.json",
        ".weapon/config.json",
        "forge/ops/safe_files.py",
        "docs/weapon/design.md",
        "docs/setup/install.md",
        "docs/plan.md",
        "docs/user-runbook.md",
        "docs/automation-architecture.md",
        "examples/demo.txt",
    ],
)
def test_risky_protected_and_unknown_paths_require_manual_merge(path: str) -> None:
    result = check_safe_files([changed(path)], pagination_complete=True)

    assert result.code == MANUAL_MERGE_REQUIRED
    assert result.paths == (path,)


def test_test_file_must_be_new_text_regular_file() -> None:
    cases = [
        changed("tests/test_api.py", status="modified"),
        changed("tests/test_api.bin", status="added", is_text=False),
        changed("tests/test_api.py", status="added", file_type="symlink"),
    ]

    assert [
        check_safe_files([item], pagination_complete=True).code for item in cases
    ] == [
        MANUAL_MERGE_REQUIRED,
        MANUAL_MERGE_REQUIRED,
        MANUAL_MERGE_REQUIRED,
    ]


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "tests/pytest.ini",
        "tests/helpers.py",
        "test/unit/fixtures.py",
    ],
)
def test_test_support_and_configuration_files_require_manual_merge(path: str) -> None:
    result = check_safe_files(
        [changed(path, status="added")], pagination_complete=True
    )

    assert result.code == MANUAL_MERGE_REQUIRED


@pytest.mark.parametrize(
    "path",
    [
        "docs/conf.py",
        "docs/install.sh",
        "docs/mkdocs.yml",
    ],
)
def test_docs_directory_only_allows_document_files(path: str) -> None:
    result = check_safe_files([changed(path)], pagination_complete=True)

    assert result.code == MANUAL_MERGE_REQUIRED


@pytest.mark.parametrize(
    "item",
    [
        changed("docs/guide.md", data_complete=False),
        changed("docs/guide.md", patch_complete=False),
        changed("docs/guide.md", tree_entry_complete=False),
        changed("docs/guide.md", is_text=None),
        changed("docs/guide.md", file_type=None),
    ],
)
def test_incomplete_file_data_is_a_check_error(item: ChangedFile) -> None:
    result = check_safe_files([item], pagination_complete=True)

    assert result.code == CHECK_ERROR
    assert result.allowed is False
    assert result.paths == ("docs/guide.md",)


def test_incomplete_pagination_is_a_check_error() -> None:
    result = check_safe_files(
        [changed("docs/guide.md")], pagination_complete=False
    )

    assert result.code == CHECK_ERROR


def test_file_completeness_proof_is_required() -> None:
    with pytest.raises(TypeError):
        ChangedFile(
            path="docs/guide.md",
            status="modified",
            is_text=True,
            file_type="file",
        )


def test_pagination_completeness_proof_is_required() -> None:
    with pytest.raises(TypeError):
        check_safe_files([changed("docs/guide.md")])


@pytest.mark.parametrize(
    "item",
    [
        changed(""),
        changed("/docs/guide.md"),
        changed("docs\\guide.md"),
        changed("docs/../README.md"),
        changed("docs/line\nbreak.md"),
        changed("docs/guide.md "),
        changed("docs/guide.md."),
        changed("docs/guide.md", status="mystery"),
        changed("docs/guide.md", file_type="mystery"),
    ],
)
def test_malformed_github_file_data_is_a_check_error(item: ChangedFile) -> None:
    result = check_safe_files([item], pagination_complete=True)

    assert result.code == CHECK_ERROR


@pytest.mark.parametrize("field", ["status", "file_type"])
def test_unhashable_github_metadata_is_a_check_error(field: str) -> None:
    item = replace(changed("docs/guide.md"), **{field: []})

    try:
        result = check_safe_files([item], pagination_complete=True)
    except TypeError as exc:
        pytest.fail(f"malformed GitHub metadata escaped as TypeError: {exc}")

    assert result.code == CHECK_ERROR


@pytest.mark.parametrize(
    "path",
    [
        "docs/Weapon/design.md",
        "docs/Setup/install.md",
        "docs/Plan.md",
        "docs/User-Runbook.md",
        "docs/Automation-Architecture.md",
    ],
)
def test_protected_paths_are_case_insensitive(path: str) -> None:
    result = check_safe_files([changed(path)], pagination_complete=True)

    assert result.code == MANUAL_MERGE_REQUIRED


def test_empty_changed_file_list_is_a_check_error() -> None:
    result = check_safe_files([], pagination_complete=True)

    assert result.code == CHECK_ERROR


def test_result_is_deterministic_and_does_not_mutate_input() -> None:
    files = [changed("docs/z.md"), changed("docs/a.md")]
    before = [replace(item) for item in files]

    first = check_safe_files(files, pagination_complete=True)
    second = check_safe_files(files, pagination_complete=True)

    assert first == second
    assert files == before
    assert first.paths == ("docs/z.md", "docs/a.md")


def test_safe_file_result_is_bound_to_the_exact_base_and_head_commits() -> None:
    result = check_safe_files(
        [changed("docs/guide.md")],
        pagination_complete=True,
    )

    evidence = SafeFilesEvidence(
        base_commit=BASE_COMMIT,
        head_commit=HEAD_COMMIT,
        result=result,
    )

    assert evidence.base_commit == BASE_COMMIT
    assert evidence.head_commit == HEAD_COMMIT
    assert evidence.result is result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_commit", "not-a-commit"),
        ("head_commit", "b" * 39),
        ("result", object()),
    ],
)
def test_safe_file_evidence_rejects_unbound_or_malformed_input(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "base_commit": BASE_COMMIT,
        "head_commit": HEAD_COMMIT,
        "result": SafeFilesResult(
            code=AUTO_MERGE_ALLOWED,
            reason="fixture",
            paths=("docs/guide.md",),
        ),
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        SafeFilesEvidence(**values)  # type: ignore[arg-type]
