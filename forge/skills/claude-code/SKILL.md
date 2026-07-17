---
name: claude-code
description: "구독 전용 공통 runner로 Claude Code CLI 작업을 실행한다."
platforms: [windows, linux, macos]
---

# Managed Claude Code Subscription CLI

이 skill은 명시적으로 선택될 때에만 실행하며, 일반 Hermes chat의 동작을 바꾸지 않는다. 정확히 하나의 OS 절만 실행한다. Linux·macOS 절은 bash가 필요하다. 각 도구의 구독 CLI 로그인 상태만 사용하고, runner가 인증·한도 분류를 담당한다. 작업 프롬프트를 명령 인수로 전달하지 않는다.

`$workspace`는 호출 전 선택된 절대 작업 폴더이고, `$prompt`는 호출 전 선택된 UTF-8 작업 내용이다. 이 skill은 공통 runner의 `claude-skill` 모드만 호출하고 다른 런타임으로 라우팅하지 않는다.

## Windows

```powershell
function Test-FullyQualifiedWindowsPath([string]$path) {
    if ($path -like '\\*') { return $true }
    return $path -match '^[A-Za-z]:\\'
}

if (-not $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON -or -not (Test-Path -LiteralPath $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON -PathType Leaf)) { throw "INFINITY_FORGE_SUBSCRIPTION_PYTHON is required" }
if (-not $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER -or -not (Test-Path -LiteralPath $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER -PathType Leaf)) { throw "INFINITY_FORGE_SUBSCRIPTION_RUNNER is required" }
if (-not (Test-FullyQualifiedWindowsPath $workspace) -or -not (Test-Path -LiteralPath $workspace -PathType Container)) { throw "workspace must be an existing fully qualified directory" }

$promptFile = $null
$promptStream = $null
$writer = $null
try {
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $candidate = Join-Path ([System.IO.Path]::GetTempPath()) ("infinity-forge-" + [System.IO.Path]::GetRandomFileName())
        try {
            $promptStream = [System.IO.File]::Open($candidate, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
            $promptFile = $candidate
            break
        } catch [System.IO.IOException] { }
    }
    if ($null -eq $promptStream) { throw "could not create prompt file" }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($identity)) { throw "current Windows identity is required" }
    # RISK(security): keep the empty file open so it cannot be replaced while ACLs and prompt content are applied.
    & icacls $promptFile /inheritance:r /grant:r "${identity}:(R,W)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict prompt-file permissions" }
    $writer = [System.IO.StreamWriter]::new($promptStream, [System.Text.UTF8Encoding]::new($false), 1024, $true)
    $writer.Write($prompt)
    $writer.Flush()
    & $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER claude-skill --workspace $workspace --prompt-file $promptFile
    $exitCode = $LASTEXITCODE
} finally {
    if ($null -ne $writer) { $writer.Dispose() }
    if ($null -ne $promptStream) { $promptStream.Dispose() }
    if ($null -ne $promptFile -and (Test-Path -LiteralPath $promptFile)) { Remove-Item -LiteralPath $promptFile -Force }
}
exit $exitCode
```

## Linux and macOS (bash required)

```bash
prompt_file=''
cleanup() {
  if [[ -n "$prompt_file" ]]; then
    rm -f "$prompt_file"
  fi
}
trap cleanup EXIT

if [[ -z "${INFINITY_FORGE_SUBSCRIPTION_PYTHON:-}" || ! -f "$INFINITY_FORGE_SUBSCRIPTION_PYTHON" ]]; then printf '%s\n' "INFINITY_FORGE_SUBSCRIPTION_PYTHON is required" >&2; exit 64; fi
if [[ -z "${INFINITY_FORGE_SUBSCRIPTION_RUNNER:-}" || ! -f "$INFINITY_FORGE_SUBSCRIPTION_RUNNER" ]]; then printf '%s\n' "INFINITY_FORGE_SUBSCRIPTION_RUNNER is required" >&2; exit 64; fi
if ! [[ "$workspace" = /* && -d "$workspace" ]]; then printf '%s\n' "workspace must be an existing absolute directory" >&2; exit 64; fi

umask 077
prompt_file="$(mktemp "${TMPDIR:-/tmp}/infinity-forge.XXXXXX")" || exit 70
chmod 600 "$prompt_file" || exit 70
printf '%s' "$prompt" > "$prompt_file" || exit 70
"$INFINITY_FORGE_SUBSCRIPTION_PYTHON" "$INFINITY_FORGE_SUBSCRIPTION_RUNNER" claude-skill --workspace "$workspace" --prompt-file "$prompt_file"
exit_code=$?
exit "$exit_code"
```

OS 분기는 문법과 환경 변수 표기만 다르며, 인증·한도 정책은 runner에 맡긴다.
