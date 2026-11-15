<#
.SYNOPSIS
    Registers a Windows scheduled task that runs a Teams archive refresh and a 7-Zip encrypted backup every Sunday at 12:00.

.DESCRIPTION
    This script is run manually by the user.
    Registers the scheduled task "\LocalMessageReaders\local-message-readers-weekly-backup".
    Triggers every Sunday at 12:00 noon, with StartWhenAvailable set.
    Runs only while the user is logged on (Interactive Logon), so the password in Windows Credential Manager can be read.
    The action runs: uv run --directory <RepoRoot> python -m readers.backup
    This script contains no password and never asks for one.

.PARAMETER RepoRoot
    Path of the local-message-readers project root. Defaults to the parent of the directory containing this script ($PSScriptRoot\..).
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param (
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$TaskPath = "\LocalMessageReaders\"
)

$TaskName = "local-message-readers-weekly-backup"
$ErrorActionPreference = "Stop"
$Destination = (Resolve-Path -LiteralPath $Destination).Path

# The task points at RepoRoot long-term; a git worktree gets deleted after use, and a task pointing at it would fail every week afterwards. Only the main checkout is accepted.
$GitDir = (git -C $RepoRoot rev-parse --path-format=absolute --git-dir 2>$null)
$CommonDir = (git -C $RepoRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
if (-not $GitDir -or -not $CommonDir) {
    throw "RepoRoot is not a git checkout: $RepoRoot"
}
if ((Resolve-Path $GitDir).Path -ne (Resolve-Path $CommonDir).Path) {
    throw "RepoRoot is a git worktree ($RepoRoot). Run this script from the main checkout, or pass -RepoRoot <main checkout>."
}

# The PATH of a scheduled task run does not necessarily include uv; resolve it to a full path at registration time
$UvPath = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source

$Action = New-ScheduledTaskAction `
    -Execute $UvPath `
    -Argument "run --directory `"$RepoRoot`" python -m readers.backup --dest `"$Destination`""

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At 12:00

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive

if ($PSCmdlet.ShouldProcess($TaskName, "Register Scheduled Task (Weekly Sunday 12:00, interactive user session)")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Force
    Write-Host "Scheduled task '$TaskPath$TaskName' registered successfully."
}
