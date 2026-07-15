#!/usr/bin/env python3
"""INFINITY_FORGE label-mirror — GitHub 이슈 ↔ kanban 카드 동기화 (LLM 0, 2분 주기).
D7: forge:* 라벨의 단독 작성자는 이 스크립트다.

수입(Import): forge:need-execution 라벨이 달린 open 이슈 중 카드가 없는 것
  → executor 카드 생성 (멱등키 github-issue:OWNER/REPO#N).
  ※ 사람이 라벨을 다는 것이 투입 행위다. 라벨 없는 이슈는 건드리지 않는다(암묵 자동 투입 방지).
투영(Project): 이슈별 pipeline frontier → 이슈의 forge:* 라벨 교체.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping

from forge.ops.contracts import (
    ContractError,
    CriticResult,
    PipelineStage,
    ReviewerResult,
    StageOutcome,
    parse_stage_result,
    validate_stage_result_binding,
)
from forge.ops.label_projection import ProjectionState, projected_label

REPOS = ["immortal0900/INFINITY_FORGE"]
HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "kanban.db")
HERMES = os.path.join(HOME, ".local", "bin", "hermes")
GH = "/usr/bin/gh"
MIRROR_STATE = os.path.join(HOME, "forge", "mirror-state.json")
# D14 즉시 알림: 사람 조치가 필요한 최종 projection만 Slack 직발송한다.
NOTIFY_LABEL = {
    "forge:mergeable": "✅ 머지 승인 필요",
    "forge:blocked": "⛔ 결정/조치 필요",
    "forge:failed": "🔴 재시도 소진",
}

ROOT_KEY_RE = re.compile(
    r"^github-issue:(?P<repository>[^#\s]+/[^#\s]+)#(?P<issue>[1-9][0-9]*)$"
)
LEGACY_ROOT_KEY_RE = re.compile(
    r"^github-issue:[^#\s]+/[^#\s]+#[1-9][0-9]*-(?:exec|review|critic)$"
)
STAGE_KEY_RE = re.compile(
    r"^forge-stage:(?P<repository>[^#\s]+/[^#\s]+)#"
    r"(?P<issue>[1-9][0-9]*):"
    r"(?P<stage>reviewer|critic|executor-rework):[0-9a-f]{16}$"
)
PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[^/]+/[^/]+)/pull/(?P<number>[1-9][0-9]*)$"
)
CHECK_STATUSES = frozenset({"queued", "in_progress", "completed"})
CHECK_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)


class ProjectionError(RuntimeError):
    """Raised when a pipeline cannot be projected without guessing."""

def slack(text):
    try:
        token = ""
        for line in open(os.path.join(HOME, ".hermes", ".env")):
            if line.startswith("SLACK_BOT_TOKEN="):
                token = line.strip().split("=", 1)[1]; break
        if not token: return
        subprocess.run(["curl", "-s", "-m", "10", "-X", "POST", "https://slack.com/api/chat.postMessage",
                        "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
                        "-d", json.dumps({"channel": "#forge-cloud", "text": text})],
                       capture_output=True, timeout=15)
    except Exception:
        pass  # 알림 실패가 미러를 막지 않는다

ALL_LABELS = ["forge:spec-draft", "forge:adr", "forge:need-execution", "forge:in-progress",
              "forge:need-review", "forge:need-critic", "forge:mergeable", "forge:blocked", "forge:failed"]

def sh(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def _json_object(value: object, *, label: str, missing: object) -> object:
    if value is None:
        return missing
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise ProjectionError(f"{label} must be a JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ProjectionError(f"{label} is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise ProjectionError(f"{label} must be a JSON object")
    return parsed


def cards_by_key() -> dict[str, dict[str, object]]:
    """Read root and stage cards with lineage and their latest completed run."""

    query = """
        SELECT
            t.idempotency_key,
            t.status,
            t.title,
            t.id,
            t.body,
            t.created_at,
            l.parent_id,
            r.status,
            r.outcome,
            r.summary,
            r.metadata
        FROM tasks AS t
        LEFT JOIN task_links AS l ON l.child_id = t.id
        LEFT JOIN task_runs AS r ON r.id = (
            SELECT MAX(r2.id)
            FROM task_runs AS r2
            WHERE r2.task_id = t.id AND r2.status IN ('done', 'completed')
        )
        WHERE t.idempotency_key LIKE 'github-issue:%'
           OR t.idempotency_key LIKE 'forge-stage:%'
    """
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = con.execute(query).fetchall()
        finally:
            con.close()
    except sqlite3.Error as error:
        raise ProjectionError(f"Hermes read failed: {error}") from error

    cards: dict[str, dict[str, object]] = {}
    for (
        key,
        status,
        title,
        card_id,
        body,
        created_at,
        parent_id,
        run_status,
        run_outcome,
        summary,
        metadata,
    ) in rows:
        if not isinstance(key, str) or not key:
            raise ProjectionError("pipeline card has no idempotency key")
        if LEGACY_ROOT_KEY_RE.fullmatch(key) is not None:
            continue
        if key in cards:
            raise ProjectionError(f"duplicate pipeline card key: {key}")
        cards[key] = {
            "status": status,
            "title": title,
            "id": card_id,
            "body": body,
            "created_at": created_at,
            "parent_id": parent_id,
            "run_status": run_status,
            "run_outcome": run_outcome,
            "summary": _json_object(
                summary,
                label=f"run summary for {card_id}",
                missing=None,
            ),
            "metadata": _json_object(
                metadata,
                label=f"run metadata for {card_id}",
                missing={},
            ),
        }
    return cards

def _key_identity(key: str) -> tuple[str, int, PipelineStage, bool]:
    root_match = ROOT_KEY_RE.fullmatch(key)
    if root_match is not None:
        return (
            root_match.group("repository"),
            int(root_match.group("issue")),
            PipelineStage.EXECUTOR,
            True,
        )
    stage_match = STAGE_KEY_RE.fullmatch(key)
    if stage_match is not None:
        return (
            stage_match.group("repository"),
            int(stage_match.group("issue")),
            PipelineStage(stage_match.group("stage")),
            False,
        )
    raise ProjectionError(f"malformed pipeline key: {key}")


def _card_field(card: Mapping[str, object], name: str, expected: type) -> object:
    value = card.get(name)
    if not isinstance(value, expected):
        raise ProjectionError(f"pipeline card {name} is malformed")
    return value


def _frontier(
    entries: list[tuple[str, Mapping[str, object], PipelineStage, bool]],
) -> tuple[str, Mapping[str, object], PipelineStage, bool]:
    cards_by_id: dict[str, tuple[str, Mapping[str, object], PipelineStage, bool]] = {}
    parent_ids: set[str] = set()
    for entry in entries:
        card = entry[1]
        card_id = _card_field(card, "id", str)
        if card_id in cards_by_id:
            raise ProjectionError(f"duplicate pipeline task id: {card_id}")
        cards_by_id[card_id] = entry
        parent_id = card.get("parent_id")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ProjectionError("pipeline parent_id is malformed")
            parent_ids.add(parent_id)

    for _, card, _, is_root in entries:
        parent_id = card.get("parent_id")
        if is_root and parent_id is not None:
            raise ProjectionError("root pipeline card cannot have a parent")
        if not is_root and parent_id not in cards_by_id:
            raise ProjectionError("stage pipeline card has an unknown parent")

    leaves = [entry for card_id, entry in cards_by_id.items() if card_id not in parent_ids]
    if len(leaves) != 1:
        raise ProjectionError(
            f"pipeline must have exactly one frontier; found {len(leaves)}"
        )
    return leaves[0]


def _stage_outcome(
    stage: PipelineStage,
    card: Mapping[str, object],
) -> StageOutcome | None:
    if card.get("status") != "done" or stage in {
        PipelineStage.EXECUTOR,
        PipelineStage.EXECUTOR_REWORK,
    }:
        return None
    terminal_run = (card.get("run_status"), card.get("run_outcome"))
    if terminal_run not in {("done", "completed"), ("completed", "success")}:
        raise ProjectionError(
            f"{stage.value} result has no successful completed run"
        )
    summary = card.get("summary")
    metadata = card.get("metadata", {})
    if not isinstance(summary, Mapping) or not isinstance(metadata, Mapping):
        raise ProjectionError(f"{stage.value} result is missing")
    try:
        result = parse_stage_result(stage, summary, metadata)
    except ContractError as error:
        raise ProjectionError(f"{stage.value} result is invalid: {error}") from error
    if isinstance(result, ReviewerResult):
        return result.verdict
    if isinstance(result, CriticResult):
        return result.outcome
    raise ProjectionError(f"{stage.value} result has the wrong type")


def projection_targets(
    cards: Mapping[str, Mapping[str, object]],
    *,
    current_head_green: Callable[[Mapping[str, object]], bool],
    max_reworks: int = 3,
) -> dict[str, str]:
    """Project each root pipeline's unique leaf without using PR numbers as issue IDs."""

    pipelines: dict[
        tuple[str, int],
        list[tuple[str, Mapping[str, object], PipelineStage, bool]],
    ] = {}
    roots: dict[tuple[str, int], str] = {}
    for key, card in cards.items():
        if not isinstance(key, str) or not isinstance(card, Mapping):
            raise ProjectionError("pipeline cards mapping is malformed")
        repository, issue_number, stage, is_root = _key_identity(key)
        identity = (repository, issue_number)
        pipelines.setdefault(identity, []).append((key, card, stage, is_root))
        if is_root:
            if identity in roots:
                raise ProjectionError(f"duplicate root pipeline for {repository}#{issue_number}")
            roots[identity] = key

    if set(pipelines) != set(roots):
        raise ProjectionError("stage pipeline exists without its root card")

    targets: dict[str, str] = {}
    for identity, entries in pipelines.items():
        _, frontier_card, stage, _ = _frontier(entries)
        status = _card_field(frontier_card, "status", str)
        outcome = _stage_outcome(stage, frontier_card)
        rework_count = sum(
            1 for _, _, entry_stage, _ in entries
            if entry_stage is PipelineStage.EXECUTOR_REWORK
        )
        green = False
        if (
            stage is PipelineStage.CRITIC
            and status == "done"
            and outcome is StageOutcome.PASS
        ):
            green = current_head_green(frontier_card)
            if not isinstance(green, bool):
                raise ProjectionError("current HEAD gate must return a boolean")
        label = projected_label(
            ProjectionState(stage, status, outcome, green, rework_count),
            max_reworks=max_reworks,
        )
        if label is not None:
            targets[roots[identity]] = label
    return targets


def _stage_binding(body: object) -> Mapping[str, object]:
    if not isinstance(body, str):
        raise ProjectionError("critic card has no binding body")
    match = re.search(r"```json\s*(\{.*?\})\s*```", body, flags=re.DOTALL)
    if match is None:
        raise ProjectionError("critic card binding body is malformed")
    value = _json_object(match.group(1), label="critic card binding", missing={})
    if not isinstance(value, Mapping):
        raise ProjectionError("critic card binding must be an object")
    return value


def _gh_json(args: list[str], *, label: str) -> Mapping[str, object]:
    rc, out, err = sh(args)
    if rc != 0:
        raise ProjectionError(f"{label} failed: {err[:120]}")
    value = _json_object(out, label=label, missing={})
    if not isinstance(value, Mapping):
        raise ProjectionError(f"{label} returned a non-object")
    return value


def critic_current_head_green(card: Mapping[str, object]) -> bool:
    """Verify a completed critic pass against its live PR HEAD and exact eval check."""

    summary = card.get("summary")
    metadata = card.get("metadata", {})
    if not isinstance(summary, Mapping) or not isinstance(metadata, Mapping):
        raise ProjectionError("critic result is missing")
    try:
        result = parse_stage_result(PipelineStage.CRITIC, summary, metadata)
    except ContractError as error:
        raise ProjectionError(f"critic result is invalid: {error}") from error
    if not isinstance(result, CriticResult) or result.outcome is not StageOutcome.PASS:
        raise ProjectionError("current HEAD gate requires a critic pass")

    pr_match = PR_URL_RE.fullmatch(result.pr_url)
    if pr_match is None:
        raise ProjectionError("critic PR URL is malformed")
    repository = pr_match.group("repository")
    binding = _stage_binding(card.get("body"))
    try:
        validate_stage_result_binding(
            result,
            expected_repository=repository,
            expected_pr_url=binding.get("pr_url"),
            expected_source_digest=binding.get("source_digest"),
            expected_head_sha=binding.get("bound_head_sha"),
        )
    except ContractError as error:
        raise ProjectionError(f"critic binding is invalid: {error}") from error

    pr = _gh_json(
        [GH, "api", f"repos/{repository}/pulls/{pr_match.group('number')}"],
        label="GitHub PR read",
    )
    expected_pr_number = int(pr_match.group("number"))
    api_pr_number = pr.get("number")
    if (
        not isinstance(api_pr_number, int)
        or isinstance(api_pr_number, bool)
        or api_pr_number != expected_pr_number
    ):
        raise ProjectionError("GitHub PR number does not match critic result")
    head = pr.get("head")
    if not isinstance(head, Mapping) or not isinstance(head.get("sha"), str):
        raise ProjectionError("GitHub PR head is malformed")
    if pr.get("html_url") != result.pr_url:
        raise ProjectionError("GitHub PR URL does not match critic result")
    state = pr.get("state")
    if state not in {"open", "closed"}:
        raise ProjectionError("GitHub PR state is malformed")
    draft = pr.get("draft")
    if not isinstance(draft, bool):
        raise ProjectionError("GitHub PR draft flag is malformed")
    if state != "open" or draft:
        return False
    if head["sha"] != result.result_head_sha:
        return False

    checks_payload = _gh_json(
        [
            GH,
            "api",
            f"repos/{repository}/commits/{result.result_head_sha}/check-runs?per_page=100",
        ],
        label="GitHub check-runs read",
    )
    check_runs = checks_payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise ProjectionError("GitHub check-runs payload is malformed")
    matches = [
        check for check in check_runs
        if isinstance(check, Mapping) and check.get("name") == "eval"
    ]
    if len(matches) != 1:
        raise ProjectionError(f"expected exactly one eval check; found {len(matches)}")
    check = matches[0]
    if check.get("head_sha") != result.result_head_sha:
        raise ProjectionError("eval check is bound to a different HEAD")
    status = check.get("status")
    if status not in CHECK_STATUSES:
        raise ProjectionError("eval check status is malformed")
    conclusion = check.get("conclusion")
    if status == "completed":
        if conclusion not in CHECK_CONCLUSIONS:
            raise ProjectionError("eval check conclusion is malformed")
    elif conclusion is not None:
        raise ProjectionError("pending eval check cannot have a conclusion")
    return status == "completed" and conclusion == "success"


def notify_transitions(
    targets: Mapping[str, str],
    cards: Mapping[str, Mapping[str, object]],
) -> None:
    """Notify only newly projected states that require human action."""

    previous: dict[str, str] = {}
    if os.path.exists(MIRROR_STATE):
        try:
            with open(MIRROR_STATE, encoding="utf-8") as state_file:
                value = json.load(state_file)
            if isinstance(value, dict):
                previous = {str(key): str(label) for key, label in value.items()}
        except (OSError, json.JSONDecodeError):
            previous = {}
    for key, label in targets.items():
        if previous.get(key) == label or label not in NOTIFY_LABEL:
            continue
        card = cards[key]
        issue_ref = key.replace("github-issue:", "")
        slack(
            f"{NOTIFY_LABEL[label]} [{issue_ref}] "
            f"{card['title']} (카드 {card['id']})"
        )
    temporary = MIRROR_STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as state_file:
        json.dump(dict(targets), state_file, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, MIRROR_STATE)


def import_new_issues(repo: str, *, existing_keys: set[str]) -> None:
    """Keep the original root executor import path and its deterministic key."""

    rc, out, err = sh(
        [
            GH,
            "api",
            f"repos/{repo}/issues?state=open&labels=forge:need-execution&per_page=50",
        ]
    )
    if rc != 0:
        raise ProjectionError(f"GitHub issue import failed for {repo}: {err[:120]}")
    try:
        issues = json.loads(out or "[]")
    except json.JSONDecodeError as error:
        raise ProjectionError(f"GitHub issue import returned invalid JSON for {repo}") from error
    if not isinstance(issues, list):
        raise ProjectionError(f"GitHub issue import returned a non-array for {repo}")

    for issue in issues:
        if not isinstance(issue, Mapping):
            raise ProjectionError("GitHub issue entry is malformed")
        if "pull_request" in issue:
            continue
        number = issue.get("number")
        title = issue.get("title")
        html_url = issue.get("html_url")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or not isinstance(title, str)
            or not isinstance(html_url, str)
        ):
            raise ProjectionError("GitHub issue entry is missing required fields")
        key = f"github-issue:{repo}#{number}"
        if key in existing_keys:
            continue
        body = (
            f"GitHub 이슈: {html_url}\n\n"
            "AC의 원본(SoT)은 위 이슈 본문이다 — 재해석 금지, 리뷰는 이슈 기준.\n"
            "kanban-codex-delegate 절차로 작업하고 핸드오프 3필드"
            "(not_implemented는 JSON 배열)로 kanban_complete."
        )
        argv = [
            HERMES,
            "kanban",
            "create",
            f"[mirror] {title}",
            "--body",
            body,
            "--assignee",
            "executor",
            "--workspace",
            f"dir:{HOME}/work/{repo.split('/')[1]}",
            "--idempotency-key",
            key,
            "--max-retries",
            "4",
            "--goal",
            "--goal-max-turns",
            "20",
        ]
        rc, _, create_error = sh(argv, timeout=60)
        if rc != 0:
            raise ProjectionError(
                f"Hermes root import failed for {key}: {create_error[:120]}"
            )
        print(f"import {key}: ok")


def project_issue_label(repo: str, issue_number: int, target: str) -> None:
    """Read and replace forge labels through the mirror's single write path."""

    issue = _gh_json(
        [GH, "api", f"repos/{repo}/issues/{issue_number}"],
        label=f"GitHub issue read for {repo}#{issue_number}",
    )
    if issue.get("state") != "open":
        return
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise ProjectionError("GitHub issue labels are malformed")
    current: list[str] = []
    for label in labels:
        if not isinstance(label, Mapping) or not isinstance(label.get("name"), str):
            raise ProjectionError("GitHub issue label entry is malformed")
        current.append(label["name"])
    forge_now = [label for label in current if label.startswith("forge:")]
    if forge_now == [target]:
        return
    keep = [label for label in current if not label.startswith("forge:")] + [target]
    patch = [
        GH,
        "api",
        "-X",
        "PATCH",
        f"repos/{repo}/issues/{issue_number}",
        "--input",
        "-",
    ]
    result = subprocess.run(
        patch,
        input=json.dumps({"labels": keep}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ProjectionError(
            f"GitHub label patch failed for {repo}#{issue_number}: "
            f"{result.stderr.strip()[:120]}"
        )
    print(f"project #{issue_number}: {forge_now} -> [{target}] ok")


def main() -> int:
    try:
        cards = cards_by_key()
        targets = projection_targets(
            cards,
            current_head_green=critic_current_head_green,
        )
        existing_keys = set(cards)
        for repo in REPOS:
            import_new_issues(repo, existing_keys=existing_keys)

        for root_key, target in targets.items():
            match = ROOT_KEY_RE.fullmatch(root_key)
            if match is None:
                raise ProjectionError(f"malformed root key: {root_key}")
            project_issue_label(
                match.group("repository"),
                int(match.group("issue")),
                target,
            )
        notify_transitions(targets, cards)
    except (
        ContractError,
        OSError,
        ProjectionError,
        subprocess.SubprocessError,
    ) as error:
        print(f"GATE_ERROR: {str(error)[:240]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
