#!/usr/bin/env pwsh
# setup-windows.ps1 — install the Brain wiring into a Claude Code config dir on Windows.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 <claude-config-dir> <vault-path>
#
# Example:
#     powershell -ExecutionPolicy Bypass -File C:\src\AiBrain\setup-windows.ps1 `
#         "$env:USERPROFILE\.claude-personal" "$env:USERPROFILE\Documents\Vaults\Ai-Brain"
#
# Idempotent: re-running updates the global CLAUDE.md, hook block, MCP registration, and
# generated brain-launch.cmd in place without disturbing other settings.

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true, Position=0)][string]$ClaudeDir,
  [Parameter(Mandatory=$true, Position=1)][string]$VaultPath
)

$ErrorActionPreference = 'Stop'

function Expand-UserPath([string]$p) {
  if ($p.StartsWith('~')) { $p = $p -replace '^~', $HOME }
  return [System.IO.Path]::GetFullPath($p)
}

$ClaudeDir    = Expand-UserPath $ClaudeDir
$VaultRoot    = Expand-UserPath $VaultPath
$RepoDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$HooksDir     = Join-Path $RepoDir 'hooks'
$McpServerDir = Join-Path $RepoDir 'mcp-server'
$TemplatesDir = Join-Path $RepoDir 'templates'
$VenvDir      = Join-Path $McpServerDir '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip      = Join-Path $VenvDir 'Scripts\pip.exe'

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
if (-not (Test-Path $VenvPython)) {
  Write-Host "[1/6] creating Python venv at $VenvDir"

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
  Write-Host "      warming up embedding model (one-time ONNX download, ~130MB)..."
  & $VenvPython -c "from brain_mcp.embed import EmbedIndex; EmbedIndex.warm()"
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "embed warm-up failed; vector recall will fall back to ripgrep until resolved."
  }
} finally {
  Pop-Location
  Remove-Item Env:BRAIN_VAULT -ErrorAction SilentlyContinue
}

# 3. Ensure the Brain/ layout exists in the vault.
foreach ($sub in @('user', 'feedback', 'references', 'projects')) {
  New-Item -ItemType Directory -Force -Path (Join-Path $VaultRoot "Brain\$sub") | Out-Null
}
New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeDir 'skills\brain') | Out-Null

# 4. Write the global CLAUDE.md with __BRAIN_VAULT__ substituted (preserving LF line endings).
Write-Host "[2/6] writing $ClaudeDir\CLAUDE.md"
$globalTemplate = Get-Content (Join-Path $TemplatesDir 'global-CLAUDE.md') -Raw
$globalRendered = $globalTemplate.Replace('__BRAIN_VAULT__', $VaultRoot)
[System.IO.File]::WriteAllText((Join-Path $ClaudeDir 'CLAUDE.md'), $globalRendered)

# 5. Drop the brain skill.
Write-Host "[3/6] writing $ClaudeDir\skills\brain\SKILL.md"
Copy-Item -Force (Join-Path $TemplatesDir 'skills\brain\SKILL.md') (Join-Path $ClaudeDir 'skills\brain\SKILL.md')

# 6. Generate the per-install brain-launch.cmd wrapper.
#    Unix-style "VAR=val cmd" env prefix does not work on Windows, and inline cmd.exe /c
#    wrappers require nasty JSON quote escaping. A generated .cmd file sidesteps both: each
#    hook command in settings.json is just "<config>\brain-launch.cmd <hook-name>".
Write-Host "[4/6] writing $ClaudeDir\brain-launch.cmd"
$LaunchCmd = Join-Path $ClaudeDir 'brain-launch.cmd'
$launchBody = @"
@echo off
rem Generated by setup-windows.ps1 — do not edit by hand. Re-run setup-windows.ps1 to regenerate.
setlocal
set "BRAIN_VAULT=$VaultRoot"
"$VenvPython" "$HooksDir\%~1.py"
exit /b %ERRORLEVEL%
"@
[System.IO.File]::WriteAllText($LaunchCmd, $launchBody)

# 7. Merge hooks block into settings.json (in-place, preserving other keys).
Write-Host "[5/6] merging hooks into $ClaudeDir\settings.json"
$SettingsFile  = Join-Path $ClaudeDir 'settings.json'
if (-not (Test-Path $SettingsFile)) { '{}' | Set-Content $SettingsFile -Encoding UTF8 }

$HooksTemplate = Join-Path $TemplatesDir 'settings.hooks.win.json'

$mergeScript = @'
import json, sys
settings_path, template_path, brain_launch = sys.argv[1:4]

with open(settings_path, "r", encoding="utf-8") as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        settings = {}

with open(template_path, "r", encoding="utf-8") as f:
    template = f.read()

# Use forward slashes in the path written into settings.json. Claude Code on
# Windows often runs hooks through Git Bash (/usr/bin/bash), which strips
# single backslashes as escape characters — so "C:\Users\...\brain-launch.cmd"
# becomes "C:Usersbrain-launch.cmd" by the time it reaches the OS. Forward
# slashes work in cmd.exe, bash, and python.exe equally well on Windows.
template = template.replace("__BRAIN_LAUNCH__", brain_launch.replace("\\", "/"))
hooks_block = json.loads(template)["hooks"]

settings.setdefault("hooks", {})
for event, definition in hooks_block.items():
    settings["hooks"][event] = definition  # overwrite the brain block; leave other events alone

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
'@

$mergeScriptPath = Join-Path $env:TEMP "brain-merge-$PID.py"
[System.IO.File]::WriteAllText($mergeScriptPath, $mergeScript)
try {
  & $VenvPython $mergeScriptPath $SettingsFile $HooksTemplate $LaunchCmd
  if ($LASTEXITCODE -ne 0) { Write-Error "settings.json merge failed"; exit $LASTEXITCODE }
} finally {
  Remove-Item -Force $mergeScriptPath -ErrorAction SilentlyContinue
}

# 8. Register the brain MCP server with user scope via the claude CLI.
#    User-scoped MCP servers live in the config dir's .claude.json and must be written
#    via `claude mcp add --scope user`. Dropping a .mcp.json file does not work — that
#    file is only read from the current project dir.
Write-Host "[6/6] registering brain MCP server (user scope)"
$ClaudeBin = if ($env:CLAUDE_BIN) { $env:CLAUDE_BIN } else { 'claude' }
$McpRegistered = $false
$McpFailReason = ''
if (-not (Get-Command $ClaudeBin -ErrorAction SilentlyContinue)) {
  $McpFailReason = "'$ClaudeBin' not on PATH (check with: Get-Command claude)"
} else {
  $env:CLAUDE_CONFIG_DIR = $ClaudeDir
  try {
    & $ClaudeBin mcp remove brain --scope user 2>$null | Out-Null
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

# 9. Clean up any obsolete .mcp.json from earlier setup runs (it never worked).
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $ClaudeDir '.mcp.json')

Write-Host ""
if ($McpRegistered) {
  Write-Host "[ok] Brain installed in $ClaudeDir"
} else {
  Write-Host "[ok] Brain files installed in $ClaudeDir"
  Write-Host ""
  Write-Host "[FAIL] MCP SERVER NOT REGISTERED — brain_* tools will NOT appear in Claude Code."
  Write-Host "   reason: $McpFailReason"
  Write-Host ""
  Write-Host "   To fix, ensure Claude Code is installed and on PATH, then register manually:"
  Write-Host "     `$env:CLAUDE_CONFIG_DIR = '$ClaudeDir'"
  Write-Host "     claude mcp add brain --scope user -e `"BRAIN_VAULT=$VaultRoot`" -- `"$VenvPython`" -m brain_mcp"
  Write-Host "   Or re-run this script after pointing `$env:CLAUDE_BIN at the claude binary:"
  Write-Host "     `$env:CLAUDE_BIN = (Get-Command claude).Source"
  Write-Host "     powershell -ExecutionPolicy Bypass -File '$($MyInvocation.MyCommand.Path)' '$ClaudeDir' '$VaultRoot'"
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open a new Claude Code session in any project."
Write-Host "  2. The SessionStart hook should preload the brain context automatically."
Write-Host "  3. The brain_* MCP tools should appear in your tool list."
Write-Host "  4. To register with LMStudio, point its MCP settings at:"
Write-Host "       command: $VenvPython"
Write-Host "       args:    -m brain_mcp"
Write-Host "       env:     BRAIN_VAULT=$VaultRoot"
