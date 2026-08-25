#!/usr/bin/env pwsh
# setup-windows.ps1 - install the Brain wiring into a Claude Code config dir on Windows.
#
# DEPRECATED (2026-08-25) — use brain-setup.py.
#
#     python3 <repo>/brain-setup.py            # interactive
#     python3 <repo>/brain-setup.py --non-interactive --vault P --claude-dir D
#
# brain-setup.py is cross-platform and is the installer this repo's docs,
# CLAUDE.md, and the generated wrappers all point at. It does everything this
# script does, and additionally gates on Python >= 3.11. THIS script does not:
# Try-Python accepts any `py -3`/`python`/`python3` it finds, and its error text
# still says '3.10+', which pyproject.toml refuses. It also runs under Windows
# PowerShell 5.1, whose ANSI-default file reads have corrupted the generated
# CLAUDE.md before (see the encoding gotcha in CLAUDE.md).
#
# setup-windows.ps1 still works and still installs a correct Brain. It will not
# receive new behaviour — PR #25's pytest extra and post-install self-test
# already landed only in brain-setup.py. See ROADMAP 'Retire the
# platform-specific installers'.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 <claude-config-dir> <vault-path>
#
# Examples:
#     powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
#         "$env:USERPROFILE\.claude" "$env:USERPROFILE\Vaults\Ai-Brain"
#     powershell -ExecutionPolicy Bypass -File C:\src\Ai-Brain\setup-windows.ps1 `
#         "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Vaults\Ai-Brain"
#
# The config dir can be any path. Single-account users use %USERPROFILE%\.claude;
# multi-account users pick their own names (anything starting with .claude is
# auto-discovered by the cross-platform brain-setup.py wizard).
#
# By default the Brain is driven through the `brain` CLI (via the generated brain.cmd
# wrapper, the skill, and the global CLAUDE.md) — no MCP server is registered with
# Claude Code, saving the ~3k tokens of tool schemas every session. Pass -WithMcp to
# also register the MCP server. Without -WithMcp, any existing user-scope 'brain' MCP
# registration is REMOVED so the token saving actually lands.
#
# Idempotent: re-running updates the global CLAUDE.md, skill, hook block, MCP
# registration, and generated brain-launch.cmd / brain.cmd in place. Other
# settings.json keys are left alone, and so are third-party hooks registered for the
# same events -- the Brain hook groups are APPENDED to whatever is already there, not
# assigned over the event. If settings.json cannot be parsed, the merge REFUSES and
# this script exits 3 rather than replace the file.

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true, Position=0)][string]$ClaudeDir,
  [Parameter(Mandatory=$true, Position=1)][string]$VaultPath,
  [switch]$WithMcp
)

$ErrorActionPreference = 'Stop'

function Expand-UserPath([string]$p) {
  if ($p.StartsWith('~')) { $p = $p -replace '^~', $HOME }
  return [System.IO.Path]::GetFullPath($p)
}

$ClaudeDir    = Expand-UserPath $ClaudeDir
$VaultRoot    = Expand-UserPath $VaultPath
$RepoDir      = Split-Path -Parent $MyInvocation.MyCommand.Path

# A header comment nobody reads is exactly the red herring deprecating this is meant
# to remove, so say it where the operator is looking. Write-Warning, not a native
# write to stderr: $ErrorActionPreference = 'Stop' turns any native stderr into a
# terminating NativeCommandError under PS 5.1, and failing over a deprecation notice
# would be worse than the duplication it is meant to retire.
Write-Warning "setup-windows.ps1 is DEPRECATED and no longer receives new behaviour."
Write-Warning "Use the cross-platform installer instead: python3 '$RepoDir\brain-setup.py'"
Write-Warning "Continuing anyway in 3s (Ctrl-C to abort)..."
Start-Sleep -Seconds 3
$HooksDir     = Join-Path $RepoDir 'hooks'
$McpServerDir = Join-Path $RepoDir 'mcp-server'
$TemplatesDir = Join-Path $RepoDir 'templates'
$VenvDir      = Join-Path $McpServerDir '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip      = Join-Path $VenvDir 'Scripts\pip.exe'
$VenvBrain    = Join-Path $VenvDir 'Scripts\brain.exe'

if (-not (Test-Path $VaultRoot)) {
  Write-Error "vault path does not exist: $VaultRoot"
  exit 1
}

Write-Host "Brain setup"
Write-Host "  repo:         $RepoDir"
Write-Host "  vault:        $VaultRoot"
Write-Host "  config dir:   $ClaudeDir"
Write-Host ""

# 1. Ensure the venv exists and brain-mcp is installed (non-editable; editable installs
#    use a .pth that doesn't always activate at startup, breaking imports from foreign cwds).
#    Health check: if an existing venv's python or pip can't run (e.g. the repo was
#    renamed and Scripts\*.exe launchers now point at a dead interpreter path), blow
#    it away and rebuild rather than emit a confusing FileNotFoundError from pip later.
function Test-VenvHealthy {
  if (-not (Test-Path $VenvPython)) { return $false }
  if (-not (Test-Path $VenvPip))    { return $false }
  try {
    & $VenvPython -c "import sys" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $VenvPip --version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    return $true
  } catch {
    return $false
  }
}

if (-not (Test-VenvHealthy)) {
  if (Test-Path $VenvDir) {
    Write-Host "[1/6] rebuilding stale venv at $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
  } else {
    Write-Host "[1/6] creating Python venv at $VenvDir"
  }

  function Try-Python([string]$exe, [string]$arg) {
    try {
      if ($arg) { & $exe $arg --version 2>&1 | Out-Null }
      else      { & $exe --version 2>&1 | Out-Null }
      return ($LASTEXITCODE -eq 0)
    } catch { return $false }
  }

  $pyExe = $null; $pyArg = $null
  foreach ($pair in @(@('py','-3'), @('python',$null), @('python3',$null))) {
    if (Try-Python $pair[0] $pair[1]) { $pyExe = $pair[0]; $pyArg = $pair[1]; break }
  }
  if (-not $pyExe) {
    Write-Error "could not find a Python 3 interpreter. Install Python 3.10+ from python.org and re-run."
    exit 1
  }

  if ($pyArg) { & $pyExe $pyArg -m venv $VenvDir } else { & $pyExe -m venv $VenvDir }
  if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed"; exit $LASTEXITCODE }

  & $VenvPip install --quiet --upgrade pip
}

Write-Host "[1/6] installing brain-mcp into venv"
& $VenvPip install --quiet --force-reinstall --no-deps $McpServerDir | Out-Null
& $VenvPip install --quiet $McpServerDir | Out-Null

# 2. Sanity-check brain_mcp imports from a foreign cwd (catches editable-install regressions).
Push-Location $env:TEMP
try {
  $env:BRAIN_VAULT = $VaultRoot
  & $VenvPython -c "from brain_mcp import vault, server, embed, compact"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "brain_mcp module failed to import from a foreign cwd. Aborting."
    exit 2
  }

  # Warm up the fastembed model so the first brain_recall isn't a 30s stall.
  # embed.py pins a stable, machine-local cache at %LOCALAPPDATA%\Ai-Brain\fastembed
  # (NOT the temp dir, which the harness rewrites — that caused a recall hang), so this
  # one-time download lands there and every later load is offline. Override with
  # BRAIN_EMBED_CACHE; force-online updates with BRAIN_EMBED_OFFLINE=0.
  Write-Host "      warming up embedding model (one-time ONNX download, ~65MB) into %LOCALAPPDATA%\Ai-Brain\fastembed..."
  $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
  & $VenvPython -c "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()"
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "embed warm-up failed; vector recall will fall back to ripgrep until resolved."
  }
} finally {
  Pop-Location
  Remove-Item Env:BRAIN_VAULT -ErrorAction SilentlyContinue
  Remove-Item Env:HF_HUB_DISABLE_SYMLINKS_WARNING -ErrorAction SilentlyContinue
}

# 3. Ensure the Brain/ layout exists in the vault.
foreach ($sub in @('user', 'feedback', 'references', 'projects')) {
  New-Item -ItemType Directory -Force -Path (Join-Path $VaultRoot "Brain\$sub") | Out-Null
}
New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeDir 'skills\brain') | Out-Null

# 4. Generate the per-install brain.cmd CLI wrapper. This is what the model runs
#    via the Bash tool (`<config>/brain.cmd recall ...`) — it bakes in BRAIN_VAULT
#    and forwards all arguments to the venv's brain.exe. Forward slashes in the
#    substituted path for the same Git Bash backslash-stripping reason as
#    brain-launch.cmd below.
#
#    It also sets BRAIN_AGENT_SURFACE=1, which makes the CLI refuse --file,
#    --from-pi and --from-cherryd. This wrapper is the invocation setup pre-approves
#    in permissions.allow, so it runs unattended: without the gate a prompt-injected
#    model could `brain.cmd save user x --file <any path>` and read the result back
#    out with an ordinary recall. Operators and the pi extension invoke the venv's
#    brain.exe directly and keep the full CLI surface.
Write-Host "[2/6] writing $ClaudeDir\brain.cmd"
$BrainCmdFile = Join-Path $ClaudeDir 'brain.cmd'
$brainCmdBody = @"
@echo off
rem Generated by setup-windows.ps1 - do not edit by hand. Re-run setup-windows.ps1 to regenerate.
setlocal
set "BRAIN_VAULT=$VaultRoot"
set "BRAIN_AGENT_SURFACE=1"
"$VenvBrain" %*
exit /b %ERRORLEVEL%
"@
[System.IO.File]::WriteAllText($BrainCmdFile, $brainCmdBody)
$BrainCmdToken = $BrainCmdFile.Replace('\', '/')

# 5. Write the global CLAUDE.md with __BRAIN_VAULT__ / __BRAIN_CMD__ substituted
#    (preserving LF line endings), and the brain skill with __BRAIN_CMD__.
#
#    Read and write UTF-8 *explicitly*. Windows PowerShell 5.1 -- which the documented
#    `powershell -ExecutionPolicy Bypass -File ...` invocation runs, and which is not
#    pwsh 7 despite the shebang -- defaults Get-Content to the ANSI codepage, so it
#    decodes the templates' UTF-8 em dashes and ellipses as cp1252 and writes the
#    mojibake straight back out. That silently corrupted 43 sequences in the global
#    CLAUDE.md and 7 in the skill: the load-bearing files, in the one step whose whole
#    job is to produce them. Same failure class as the 2026-07-29 CLI stdin incident.
Write-Host "[3/6] writing $ClaudeDir\CLAUDE.md and skills\brain\SKILL.md"
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
$globalTemplate = [System.IO.File]::ReadAllText((Join-Path $TemplatesDir 'global-CLAUDE.md'), [System.Text.Encoding]::UTF8)
$globalRendered = $globalTemplate.Replace('__BRAIN_VAULT__', $VaultRoot).Replace('__BRAIN_CMD__', $BrainCmdToken)
[System.IO.File]::WriteAllText((Join-Path $ClaudeDir 'CLAUDE.md'), $globalRendered, $Utf8NoBom)

$skillTemplate = [System.IO.File]::ReadAllText((Join-Path $TemplatesDir 'skills\brain\SKILL.md'), [System.Text.Encoding]::UTF8)
$skillRendered = $skillTemplate.Replace('__BRAIN_CMD__', $BrainCmdToken)
[System.IO.File]::WriteAllText((Join-Path $ClaudeDir 'skills\brain\SKILL.md'), $skillRendered, $Utf8NoBom)

# 6. Generate the per-install brain-launch.cmd wrapper.
#    Unix-style "VAR=val cmd" env prefix does not work on Windows, and inline cmd.exe /c
#    wrappers require nasty JSON quote escaping. A generated .cmd file sidesteps both: each
#    hook command in settings.json is just "<config>\brain-launch.cmd <hook-name>".
Write-Host "[4/6] writing $ClaudeDir\brain-launch.cmd"
$LaunchCmd = Join-Path $ClaudeDir 'brain-launch.cmd'
$launchBody = @"
@echo off
rem Generated by setup-windows.ps1 - do not edit by hand. Re-run setup-windows.ps1 to regenerate.
setlocal
set "BRAIN_VAULT=$VaultRoot"
"$VenvPython" "$HooksDir\%~1.py"
exit /b %ERRORLEVEL%
"@
[System.IO.File]::WriteAllText($LaunchCmd, $launchBody)

# 7. Merge the Brain hook block into settings.json.
#    The merge itself lives in brain_settings_merge.py - the ONE implementation
#    shared by all four installers and all four uninstallers, so this algorithm
#    cannot fork again (it used to be a PowerShell here-string copy of the same
#    Python). It APPENDS our hook groups to whatever third-party hooks already
#    exist for the same events (assigning over the event silently deleted a user's
#    own SessionStart/Stop/PreCompact hook), REFUSES to rewrite an unparseable
#    settings.json instead of replacing it with {}, takes a timestamped backup, and
#    writes atomically. It also creates the file when missing, so nothing here
#    pre-seeds '{}'.
Write-Host "[5/6] merging hooks into $ClaudeDir\settings.json"
$SettingsFile  = Join-Path $ClaudeDir 'settings.json'
$HooksTemplate = Join-Path $TemplatesDir 'settings.hooks.win.json'
$MergeScript   = Join-Path $RepoDir 'brain_settings_merge.py'

# `$ErrorActionPreference = 'Stop'` turns ANY native stderr into a terminating
# NativeCommandError under Windows PowerShell 5.1 -- and the merge script's whole
# refusal path (malformed settings.json) writes its diagnosis to stderr. Without
# this try/catch the installer would die with a PowerShell traceback instead of
# the actionable message, and would skip the partial-install summary below. Same
# trap as the two `claude mcp remove` call sites further down.
$mergeRc = 0
$mergeOutput = ''
try {
  $mergeOutput = (& $VenvPython $MergeScript merge `
      --settings $SettingsFile `
      --template $HooksTemplate `
      --brain-cmd $BrainCmdToken `
      --brain-launch $LaunchCmd 2>&1 | Out-String)
  $mergeRc = $LASTEXITCODE
} catch {
  $mergeOutput = $_.Exception.Message
  $mergeRc = if ($LASTEXITCODE) { $LASTEXITCODE } else { 3 }
}
if ($mergeOutput.Trim()) { Write-Host $mergeOutput.Trim() }
if ($mergeRc -ne 0) {
  Write-Host ""
  Write-Host "[FAIL] PARTIAL INSTALLATION in $ClaudeDir"
  Write-Host "   These steps DID succeed:"
  Write-Host "     - the venv and brain-mcp install"
  Write-Host "     - $ClaudeDir\brain.cmd and $ClaudeDir\brain-launch.cmd"
  Write-Host "     - $ClaudeDir\CLAUDE.md and skills\brain\SKILL.md"
  Write-Host "   These did NOT:"
  Write-Host "     - the hook wiring (no preload, no checkpoints, no stop-gate)"
  Write-Host "     - the Bash(<brain cmd>:*) permission rule (proactive saves will prompt)"
  Write-Host "     - MCP registration / cleanup (not attempted)"
  Write-Host "   Repair $SettingsFile and re-run this script."
  exit $mergeRc
}

# 8. Register the brain MCP server with user scope via the claude CLI.
#    User-scoped MCP servers live in the config dir's .claude.json and must be written
#    via `claude mcp add --scope user`. Dropping a .mcp.json file does not work - that
#    file is only read from the current project dir.
Write-Host "[6/6] MCP registration"
$ClaudeBin = if ($env:CLAUDE_BIN) { $env:CLAUDE_BIN } else { 'claude' }
$McpRegistered = $false
$McpFailReason = ''

# `claude mcp add --scope user` writes to $CLAUDE_CONFIG_DIR/.claude.json when
# the env var is set, but to %USERPROFILE%\.claude.json when it isn't - two
# different files. When $ClaudeDir is the default location, we MUST leave the
# env var unset so the write lands where a plain `claude` invocation later
# reads from. For custom config dirs each has its own sibling .claude.json
# inside it, so the env var is correct and required.
$DefaultClaudeDir = Join-Path $env:USERPROFILE '.claude'
$IsDefaultTarget = ([System.IO.Path]::GetFullPath($ClaudeDir).TrimEnd('\')) -ieq `
                   ([System.IO.Path]::GetFullPath($DefaultClaudeDir).TrimEnd('\'))

if ($WithMcp) {
  if (-not (Get-Command $ClaudeBin -ErrorAction SilentlyContinue)) {
    $McpFailReason = "'$ClaudeBin' not on PATH (check with: Get-Command claude)"
  } else {
    Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
    if (-not $IsDefaultTarget) { $env:CLAUDE_CONFIG_DIR = $ClaudeDir }
    try {
      # Same guard as the CLI-first branch below: `2>$null` does NOT stop
      # $ErrorActionPreference='Stop' turning native stderr into a terminating
      # NativeCommandError (verified against PS 5.1, 2026-08-24), so on a machine
      # with nothing registered this expected "no server named brain" aborted the
      # whole -WithMcp install before it ever reached `mcp add`.
      try {
        & $ClaudeBin mcp remove brain --scope user 2>&1 | Out-Null
      } catch {
        # nothing registered yet; `mcp add` below is the point of this branch
      }
      # Capture stdout+stderr so a silent CLI failure doesn't vanish into Out-Null.
      $addOutput = (& $ClaudeBin mcp add brain --scope user -e "BRAIN_VAULT=$VaultRoot" -- $VenvPython -m brain_mcp 2>&1 | Out-String).Trim()
      $addRc = $LASTEXITCODE
      if ($addRc -ne 0) {
        $McpFailReason = "'claude mcp add' exited ${addRc}: $addOutput"
      } else {
        $listOutput = (& $ClaudeBin mcp list 2>&1 | Out-String)
        if ($listOutput -notmatch '(?m)^brain') {
          $McpFailReason = "'claude mcp add' returned success but 'brain' not in 'claude mcp list'"
        } else {
          $McpRegistered = $true
          Write-Host "       [ok] registered as user-scope MCP server in $ClaudeDir"
        }
      }
    } finally {
      Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
    }
  }
} else {
  # CLI-first default: the skill + global CLAUDE.md drive the `brain` CLI, so
  # the MCP server would only cost ~3k tokens of schemas per session. Remove
  # any stale registration so the token saving actually lands.
  if (Get-Command $ClaudeBin -ErrorAction SilentlyContinue) {
    Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
    if (-not $IsDefaultTarget) { $env:CLAUDE_CONFIG_DIR = $ClaudeDir }
    try {
      # "No MCP server named brain in user scope" is the *expected* result on a
      # CLI-first install with nothing to remove, but claude writes it to stderr and
      # $ErrorActionPreference='Stop' turns any native stderr into a terminating
      # NativeCommandError -- so the idempotent path exited 1 and the whole setup
      # reported failure after all six steps had already succeeded.
      try {
        & $ClaudeBin mcp remove brain --scope user 2>&1 | Out-Null
      } catch {
        # nothing registered; that is the state we wanted anyway
      }
    } finally {
      Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
    }
    Write-Host "       skipped (CLI-first default); removed any stale user-scope 'brain' entry."
  } else {
    Write-Host "       skipped (CLI-first default; '$ClaudeBin' not on PATH, nothing to remove)."
  }
  Write-Host "       pass -WithMcp to register the brain_* MCP tools instead."
}

# 9. Clean up any obsolete .mcp.json from earlier setup runs (it never worked).
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $ClaudeDir '.mcp.json')

Write-Host ""
if ($WithMcp -and -not $McpRegistered) {
  Write-Host "[ok] Brain files installed in $ClaudeDir"
  Write-Host ""
  Write-Host "[FAIL] MCP SERVER NOT REGISTERED - brain_* tools will NOT appear in Claude Code."
  Write-Host "   reason: $McpFailReason"
  Write-Host ""
  Write-Host "   To fix, ensure Claude Code is installed and on PATH, then register manually:"
  if ($IsDefaultTarget) {
    Write-Host "     Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue"
  } else {
    Write-Host "     `$env:CLAUDE_CONFIG_DIR = '$ClaudeDir'"
  }
  Write-Host "     claude mcp add brain --scope user -e `"BRAIN_VAULT=$VaultRoot`" -- `"$VenvPython`" -m brain_mcp"
  Write-Host "   Or re-run this script after pointing `$env:CLAUDE_BIN at the claude binary:"
  Write-Host "     `$env:CLAUDE_BIN = (Get-Command claude).Source"
  Write-Host "     powershell -ExecutionPolicy Bypass -File '$($MyInvocation.MyCommand.Path)' '$ClaudeDir' '$VaultRoot' -WithMcp"
} else {
  Write-Host "[ok] Brain installed in $ClaudeDir"
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open a new Claude Code session in any project."
Write-Host "  2. The SessionStart hook should preload the brain context automatically."
if ($WithMcp) {
  Write-Host "  3. The brain_* MCP tools should appear in your tool list."
} else {
  Write-Host "  3. The model drives the Brain via the CLI wrapper: $BrainCmdToken <recall|save|...>"
  Write-Host "     (try /brain list in a session, or run the command above yourself)."
}
Write-Host "  4. To register with LMStudio or another MCP client, point its MCP settings at:"
Write-Host "       command: $VenvPython"
Write-Host "       args:    -m brain_mcp"
Write-Host "       env:     BRAIN_VAULT=$VaultRoot"
