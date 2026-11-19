<#
.SYNOPSIS
    Releases a version: the global teams-reader / wechat-reader snapshot is reinstalled from the released commit.

.DESCRIPTION
    Run from a clean main that matches the remote. In order, and stopping at the first failure:
      1. Check: branch is main, no uncommitted or untracked files, HEAD equals <Remote>/main, tag v<Version> is new.
      2. Set the version with `uv version` (updates pyproject.toml and uv.lock), commit it and push main.
         Skipped when the project already has this version.
      3. Wait for the CI workflow (.github/workflows/ci.yml) on that commit to pass.
      4. Reinstall the global tool from the working tree (non-editable, ADR-0011) and run both readers' --help.
      5. Create the annotated tag v<Version> and push it. The Release workflow then tests the tag and publishes
         the GitHub Release with generated notes.
    Nothing is tagged unless steps 1-4 pass. Rerunning with the same version after a failure is safe.

.PARAMETER Version
    Semantic version without the leading v, for example 0.2.0.

.PARAMETER Remote
    Git remote that holds main. Defaults to origin.

.PARAMETER CiTimeoutMinutes
    How long to wait for CI on the release commit. Defaults to 30.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$Remote = 'origin',
    [int]$CiTimeoutMinutes = 30
)

$ErrorActionPreference = 'Stop'
# Decode native command output as UTF-8 whoever the caller is (Git Bash hands down a non-UTF-8 console).
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$tag = "v$Version"

function Invoke-Native {
    param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
    $output = & $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE):`n$($output | Out-String)"
    }
    return $output
}

function Write-Step([string]$Text) { Write-Host "[release $tag] $Text" }

$root = (Invoke-Native 'git rev-parse' { git rev-parse --show-toplevel }).Trim()
Set-Location $root

# 1. Preconditions
$branch = (Invoke-Native 'git branch' { git branch --show-current }).Trim()
if ($branch -ne 'main') { throw "Release from main; the current branch is '$branch'." }
$dirty = Invoke-Native 'git status' { git status --porcelain --untracked-files=all }
if ($dirty) { throw "The working tree is not clean:`n$($dirty | Out-String)" }
Invoke-Native "git fetch $Remote" { git fetch --tags $Remote } | Out-Null
$head = (Invoke-Native 'git rev-parse HEAD' { git rev-parse HEAD }).Trim()
$remoteHead = (Invoke-Native "git rev-parse $Remote/main" { git rev-parse "$Remote/main" }).Trim()
if ($head -ne $remoteHead) { throw "HEAD ($head) differs from $Remote/main ($remoteHead); pull or push first." }
if (git tag --list $tag) { throw "Tag $tag already exists." }
Write-Step 'preconditions ok'

# 2. Version commit
$current = (Invoke-Native 'uv version --short' { uv version --short }).Trim()
if ($current -ne $Version) {
    Invoke-Native "uv version $Version" { uv version $Version } | Out-Null
    Invoke-Native 'git add' { git add pyproject.toml uv.lock } | Out-Null
    Invoke-Native 'git commit' { git commit -m "chore(release): $tag" } | Out-Null
    Invoke-Native "git push $Remote main" { git push $Remote main } | Out-Null
    $head = (Invoke-Native 'git rev-parse HEAD' { git rev-parse HEAD }).Trim()
    Write-Step "version set to $Version and pushed ($($head.Substring(0, 7)))"
} else {
    Write-Step "version is already $Version"
}

# 3. Wait for CI on the release commit
$deadline = (Get-Date).AddMinutes($CiTimeoutMinutes)
while ($true) {
    $runs = Invoke-Native 'gh run list' {
        gh run list --workflow ci.yml --commit $head --json databaseId,status,conclusion,url
    } | Out-String | ConvertFrom-Json
    $run = $runs | Select-Object -First 1
    if ($run -and $run.status -eq 'completed') {
        if ($run.conclusion -ne 'success') { throw "CI on $($head.Substring(0, 7)) ended with '$($run.conclusion)': $($run.url)" }
        Write-Step "CI passed: $($run.url)"
        break
    }
    if ((Get-Date) -gt $deadline) { throw "CI on $($head.Substring(0, 7)) did not finish within $CiTimeoutMinutes minutes." }
    Start-Sleep -Seconds 20
}

# 4. Reinstall the global snapshot from this commit and check that it starts
Invoke-Native 'uv tool install --reinstall' { uv tool install --reinstall $root } | Out-Null
$bin = (Invoke-Native 'uv tool dir --bin' { uv tool dir --bin }).Trim()
foreach ($name in 'teams-reader', 'wechat-reader') {
    Invoke-Native "$name --help" { & (Join-Path $bin "$name.exe") --help } | Out-Null
}
Write-Step 'global tool reinstalled from the release commit'

# 5. Tag and push; the Release workflow publishes the GitHub Release
Invoke-Native 'git tag' { git tag -a $tag -m $tag } | Out-Null
Invoke-Native "git push $Remote $tag" { git push $Remote $tag } | Out-Null
$repo = (Invoke-Native 'gh repo view' { gh repo view --json nameWithOwner --jq .nameWithOwner }).Trim()
Write-Step "tag pushed; the Release workflow publishes https://github.com/$repo/releases/tag/$tag"
