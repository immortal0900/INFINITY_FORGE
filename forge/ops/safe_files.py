"""Deterministic file rules for ``safe_auto`` merge decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re


AUTO_MERGE_ALLOWED = "AUTO_MERGE_ALLOWED"
MANUAL_MERGE_REQUIRED = "MANUAL_MERGE_REQUIRED"
CHECK_ERROR = "CHECK_ERROR"

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_KNOWN_STATUSES = frozenset(
    {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}
)
_KNOWN_FILE_TYPES = frozenset({"file", "symlink", "submodule"})
_SAFE_STATUSES = frozenset({"added", "modified"})
_MANUAL_STATUSES = frozenset({"removed", "renamed", "copied", "changed", "unchanged"})
_DOCUMENT_SUFFIXES = (".md", ".markdown", ".rst", ".txt")
_PROTECTED_DOCS = frozenset(
    {
        "docs/plan.md",
        "docs/user-runbook.md",
        "docs/automation-architecture.md",
    }
)
_PROTECTED_DIRS = (
    "docs/weapon",
    "docs/setup",
    "forge",
)
_PROTECTED_CONFIG_DIRS = frozenset({".codex", ".claude", ".weapon", ".github"})
_RISK_WORDS = frozenset(
    {
        "alembic",
        "acl",
        "actions",
        "ansible",
        "auth",
        "authentication",
        "cert",
        "certificate",
        "certificates",
        "certs",
        "cd",
        "ci",
        "credential",
        "credentials",
        "database",
        "deploy",
        "deployment",
        "docker",
        "env",
        "helm",
        "iam",
        "infra",
        "infrastructure",
        "jwt",
        "k8s",
        "key",
        "keys",
        "kubernetes",
        "migration",
        "migrations",
        "oauth",
        "oidc",
        "password",
        "passwords",
        "permission",
        "permissions",
        "pipeline",
        "pipelines",
        "pulumi",
        "rbac",
        "saml",
        "schema",
        "secret",
        "secrets",
        "security",
        "systemd",
        "terraform",
        "token",
        "tokens",
        "workflow",
        "workflows",
    }
)
_DEPENDENCY_FILES = frozenset(
    {
        "cargo.toml",
        "cargo.lock",
        "composer.json",
        "composer.lock",
        "gemfile",
        "gemfile.lock",
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pipfile",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "uv.lock",
        "yarn.lock",
    }
)


@dataclass(frozen=True)
class ChangedFile:
    """One fully inspected file from the current GitHub pull-request commit."""

    path: str
    status: str
    is_text: bool | None
    file_type: str | None
    # RISK(breaking): 누락된 GitHub 증거가 자동 병합을 열지 않도록 완전성 값은 모두 의무 입력이다.
    data_complete: bool
    patch_complete: bool
    tree_entry_complete: bool


@dataclass(frozen=True)
class SafeFilesResult:
    """A fail-closed safe-file decision and the paths that caused it."""

    code: str
    reason: str
    paths: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.code == AUTO_MERGE_ALLOWED


@dataclass(frozen=True)
class SafeFilesEvidence:
    """One safe-file result bound to the exact inspected base and head."""

    base_commit: str
    head_commit: str
    result: SafeFilesResult

    def __post_init__(self) -> None:
        # RISK(security): an unbound file result could approve a different PR
        # revision, so both commits and the typed result are mandatory.
        if (
            not isinstance(self.base_commit, str)
            or _GIT_SHA_RE.fullmatch(self.base_commit) is None
        ):
            raise ValueError(
                "base_commit must be a lowercase 40-character Git SHA"
            )
        if (
            not isinstance(self.head_commit, str)
            or _GIT_SHA_RE.fullmatch(self.head_commit) is None
        ):
            raise ValueError(
                "head_commit must be a lowercase 40-character Git SHA"
            )
        if not isinstance(self.result, SafeFilesResult):
            raise TypeError("result must be a SafeFilesResult")


SafeFilesDecision = SafeFilesResult


# RISK(security): 이 결정이 자동 병합 권한을 열므로, 알 수 없거나 누락된 입력은 항상 CHECK_ERROR로 닫는다.
def check_safe_files(
    files: Sequence[ChangedFile], *, pagination_complete: bool
) -> SafeFilesDecision:
    """Classify complete GitHub file data without an LLM or risk score."""

    if pagination_complete is not True:
        return _check_error("GitHub file pagination is incomplete")
    if isinstance(files, (str, bytes)) or not isinstance(files, Sequence):
        return _check_error("changed files must be a sequence")

    inspected = tuple(files)
    if not inspected:
        return _check_error("GitHub returned no changed files")

    incomplete: list[str] = []
    malformed: list[str] = []
    for item in inspected:
        if not isinstance(item, ChangedFile):
            return _check_error("changed file data has an unexpected type")
        problem = _metadata_problem(item)
        if problem == "incomplete":
            incomplete.append(item.path)
        elif problem == "malformed":
            malformed.append(item.path)

    if incomplete:
        return _check_error("GitHub file data is incomplete", incomplete)
    if malformed:
        return _check_error("GitHub file data is malformed", malformed)

    manual_paths = tuple(item.path for item in inspected if _requires_manual(item))
    if manual_paths:
        return SafeFilesResult(
            code=MANUAL_MERGE_REQUIRED,
            reason="one or more changes are outside the safe_auto file rules",
            paths=manual_paths,
        )

    return SafeFilesResult(
        code=AUTO_MERGE_ALLOWED,
        reason="all changes match the safe_auto file rules",
        paths=tuple(item.path for item in inspected),
    )


def _check_error(reason: str, paths: Sequence[str] = ()) -> SafeFilesResult:
    return SafeFilesResult(code=CHECK_ERROR, reason=reason, paths=tuple(paths))


def _metadata_problem(item: ChangedFile) -> str | None:
    if not _valid_repository_path(item.path):
        return "malformed"
    if not isinstance(item.status, str):
        return "malformed"
    if item.file_type is not None and not isinstance(item.file_type, str):
        return "malformed"
    if item.is_text is not None and not isinstance(item.is_text, bool):
        return "malformed"
    if (
        item.data_complete is not True
        or item.patch_complete is not True
        or item.tree_entry_complete is not True
        or item.is_text is None
        or item.file_type is None
    ):
        return "incomplete"
    if item.status not in _KNOWN_STATUSES or item.file_type not in _KNOWN_FILE_TYPES:
        return "malformed"
    return None


def _valid_repository_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return False
    if path.startswith("/") or path.endswith("/"):
        return False
    parts = path.split("/")
    return all(
        part not in {"", ".", ".."} and part == part.strip() and not part.endswith(".")
        for part in parts
    )


def _requires_manual(item: ChangedFile) -> bool:
    if item.status in _MANUAL_STATUSES or item.status not in _SAFE_STATUSES:
        return True
    if item.file_type != "file" or item.is_text is not True:
        return True
    if _protected_or_risky_path(item.path):
        return True
    if _under(item.path, "docs"):
        return not _is_document_file(item.path)
    if _is_root_readme_or_changelog(item.path):
        return False
    # RISK(security): 새 test는 실행 가능한 code일 수 있지만 확정된 safe_auto
    # 계약상 추가만 허용한다. 기존 test 수정과 test 보조 파일은 계속 사람이 검토한다.
    if _under(item.path, "tests") or _under(item.path, "test"):
        return item.status != "added" or not _is_test_file(item.path)
    return True


def _protected_or_risky_path(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    basename = parts[-1]

    if lowered in _PROTECTED_DOCS:
        return True
    if any(
        lowered == directory or lowered.startswith(f"{directory}/")
        for directory in _PROTECTED_DIRS
    ):
        return True
    if any(part in _PROTECTED_CONFIG_DIRS for part in parts):
        return True
    if basename in {"agents.md", "skill.md"}:
        return True
    if basename in _DEPENDENCY_FILES or basename.startswith("requirements"):
        return True
    if basename.endswith(".lock") or "-lock." in basename:
        return True
    if basename.startswith("dockerfile") or basename in {"jenkinsfile", "gitlab-ci.yml"}:
        return True
    if basename.endswith((".service", ".timer", ".sql", ".tf", ".tfvars")):
        return True

    words = frozenset(word for word in re.split(r"[^a-z0-9]+", lowered) if word)
    return bool(words & _RISK_WORDS)


def _under(path: str, directory: str) -> bool:
    return path.startswith(f"{directory}/")


def _is_document_file(path: str) -> bool:
    return path.lower().endswith(_DOCUMENT_SUFFIXES)


def _is_root_readme_or_changelog(path: str) -> bool:
    if "/" in path:
        return False
    if not path.endswith((".md", ".txt", ".rst")):
        return False
    return path.startswith(("README.", "README-", "CHANGELOG.", "CHANGELOG-"))


def _is_test_file(path: str) -> bool:
    basename = path.rsplit("/", 1)[-1].lower()
    stem = basename.rsplit(".", 1)[0]
    return (
        stem in {"test", "spec"}
        or stem.startswith(("test_", "spec_"))
        or stem.endswith(("_test", "_spec"))
        or ".test." in basename
        or ".spec." in basename
    )
