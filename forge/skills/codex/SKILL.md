---
name: codex
description: "구독 전용 공통 runner로 Codex CLI 작업을 실행한다."
platforms: [windows, linux, macos]
---

# Managed Codex Subscription CLI

이 skill은 명시적으로 선택될 때에만 실행하며, 일반 Hermes chat의 동작을 바꾸지 않는다. 각 도구의 구독 CLI 로그인 상태만 사용하고, runner가 인증·한도 분류를 담당한다. 작업 프롬프트를 명령 인수로 전달하지 않는다.

`$workspace`는 호출 전 선택된 절대 작업 폴더이고, `$prompt`는 호출 전 선택된 UTF-8 작업 내용이다. 이 skill은 공통 runner의 `codex-skill` 모드만 호출한다. runner의 receipt에 기록된 최종 런타임과 fallback 결과를 추측하거나 바꾸지 말고 그대로 보고한다.

## Windows

```powershell
if (-not $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON -or -not (Test-Path -LiteralPath $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON -PathType Leaf)) { throw "INFINITY_FORGE_SUBSCRIPTION_PYTHON is required" }
if (-not $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER -or -not (Test-Path -LiteralPath $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER -PathType Leaf)) { throw "INFINITY_FORGE_SUBSCRIPTION_RUNNER is required" }
if (-not [System.IO.Path]::IsPathRooted($workspace) -or -not (Test-Path -LiteralPath $workspace -PathType Container)) { throw "workspace must be an existing absolute directory" }

$promptFile = Join-Path ([System.IO.Path]::GetTempPath()) ("infinity-forge-" + [System.IO.Path]::GetRandomFileName())
try {
    $writer = [System.IO.StreamWriter]::new($promptFile, $false, [System.Text.UTF8Encoding]::new($false))
    try { $writer.Write($prompt) } finally { $writer.Dispose() }
    if (Get-Command icacls -ErrorAction SilentlyContinue) {
        & icacls $promptFile /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not restrict prompt-file permissions" }
    }
    & $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER codex-skill --workspace $workspace --prompt-file $promptFile
    $exitCode = $LASTEXITCODE
} finally {
    if (Test-Path -LiteralPath $promptFile) { Remove-Item -LiteralPath $promptFile -Force }
}
exit $exitCode
```

## Linux and macOS

```bash
if [[ -z "${INFINITY_FORGE_SUBSCRIPTION_PYTHON:-}" || ! -f "$INFINITY_FORGE_SUBSCRIPTION_PYTHON" ]]; then printf '%s\n' "INFINITY_FORGE_SUBSCRIPTION_PYTHON is required" >&2; exit 64; fi
if [[ -z "${INFINITY_FORGE_SUBSCRIPTION_RUNNER:-}" || ! -f "$INFINITY_FORGE_SUBSCRIPTION_RUNNER" ]]; then printf '%s\n' "INFINITY_FORGE_SUBSCRIPTION_RUNNER is required" >&2; exit 64; fi
if ! [[ "$workspace" = /* && -d "$workspace" ]]; then printf '%s\n' "workspace must be an existing absolute directory" >&2; exit 64; fi

umask 077
prompt_file="$(mktemp)"
chmod 600 "$prompt_file"
printf '%s' "$prompt" > "$prompt_file"
trap 'rm -f "$prompt_file"' EXIT
"$INFINITY_FORGE_SUBSCRIPTION_PYTHON" "$INFINITY_FORGE_SUBSCRIPTION_RUNNER" codex-skill --workspace "$workspace" --prompt-file "$prompt_file"
exit_code=$?
exit "$exit_code"
```

OS 분기는 문법과 환경 변수 표기만 다르며, 인증·한도·fallback 정책은 runner에 맡긴다.
