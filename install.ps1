<#
.SYNOPSIS
    SushiStack one-script installer (Windows).

.DESCRIPTION
    Bootstraps Python and Git — using winget when available, direct downloads
    otherwise (no Microsoft Store required). Clones the SushiStack workspace,
    installs the `ss` CLI, then provisions the shared dependency tree with
    `ss install`. The portable CMake/Ninja and the SYCL toolchains are downloaded
    by `ss install` into <workspace>\dependencies, so only Python and Git are
    bootstrapped here. `ss install` provisions everything (all SYCL toolchains +
    CUDA); to choose a subset, run `ss install --customize` interactively after.

.PARAMETER Add
    Space- or comma-separated module list to clone into the workspace after deps
    are provisioned, e.g. -Add "sushiruntime sushiengine". Default: none.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Add "sushiruntime sushiengine"

.EXAMPLE
    irm https://sushisystems.io/install.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]$Add = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "[INFO] $m"  -ForegroundColor Cyan }
function Warn($m) { Write-Host "[WARN] $m"  -ForegroundColor Yellow }
function Fail($m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }

$RepoUrl = if ($env:SUSHISTACK_REPO_URL) { $env:SUSHISTACK_REPO_URL } else { "https://github.com/sushisystems/sushistack.git" }

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

function GitHub-LatestUrl($repo, $assetGlob) {
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like $assetGlob } | Select-Object -First 1
    if (-not $asset) { Fail "No asset matching '$assetGlob' in $repo latest release." }
    return $asset.browser_download_url
}

function Download($url, $dest) {
    Info "Downloading $(Split-Path $dest -Leaf)..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    (New-Object Net.WebClient).DownloadFile($url, $dest)
}

function Ensure-Python {
    Refresh-Path
    if (Get-Command python -ErrorAction SilentlyContinue) { return }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info "Installing Python via winget..."
        winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent
        Refresh-Path
        if (Get-Command python -ErrorAction SilentlyContinue) { return }
    }

    $url  = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $dest = Join-Path $env:TEMP "python-setup.exe"
    Download $url $dest
    Info "Installing Python (user scope, prepend PATH)..."
    Start-Process -Wait -FilePath $dest -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_pip=1"
    Refresh-Path
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Fail "Python installation failed. Open a new terminal and re-run."
    }
}

function Ensure-Git {
    Refresh-Path
    if (Get-Command git -ErrorAction SilentlyContinue) { return }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info "Installing Git via winget..."
        winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements --silent
        Refresh-Path
        if (Get-Command git -ErrorAction SilentlyContinue) { return }
    }

    $url  = GitHub-LatestUrl "git-for-windows/git" "*64-bit.exe"
    $dest = Join-Path $env:TEMP "git-setup.exe"
    Download $url $dest
    Info "Installing Git..."
    Start-Process -Wait -FilePath $dest -ArgumentList "/VERYSILENT","/NORESTART","/NOCANCEL","/SP-","/CLOSEAPPLICATIONS","/RESTARTAPPLICATIONS","/COMPONENTS=icons,ext\reg\shellhere,assoc,assoc_sh"
    Refresh-Path
}

# Bootstrap only what `ss` itself needs to run and clone: Python and Git. CMake,
# Ninja, and the SYCL toolchains are downloaded portably into the shared
# <workspace>\dependencies by `ss install`.
Ensure-Python
Ensure-Git

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Fail "python not on PATH after install. Open a new terminal and re-run." }

# Locate or clone the workspace. The SushiStack repo is identified by its
# cli\manifests tree (it ships no CMakeLists.txt).
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { $null }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "cli\manifests"))) {
    $WorkspaceDir = $ScriptDir
} else {
    $WorkspaceDir = if ($env:SUSHISTACK_DIR) { $env:SUSHISTACK_DIR } else { Join-Path $HOME "sushistack" }
    if (-not (Test-Path (Join-Path $WorkspaceDir ".git"))) {
        Info "Cloning $RepoUrl -> $WorkspaceDir"
        git clone $RepoUrl $WorkspaceDir
    }
}
Set-Location $WorkspaceDir
Info "Workspace: $WorkspaceDir"

# Install the ss CLI.
Info "Installing the ss CLI..."
python cli/install.py

$PipxBinDir = python -m pipx environment --value PIPX_BIN_DIR
$SsCmd = Join-Path $PipxBinDir "ss.exe"
if (-not (Test-Path $SsCmd)) { $SsCmd = "ss" }

# Mark the workspace, then provision the shared dependency tree (everything).
& $SsCmd init

$flags = @("install")
if ($DryRun) { $flags += "--dry-run" }
Info "Running: ss $($flags -join ' ')"
& $SsCmd @flags
$ssExit = $LASTEXITCODE
if ($ssExit -ne 0) { exit $ssExit }

# Optionally clone the requested modules into the workspace.
$modules = ($Add -replace ',', ' ').Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
if ($modules.Count -gt 0) {
    Info "Adding modules: $($modules -join ' ')"
    & $SsCmd add @modules
}

Info "Done. Workspace ready at $WorkspaceDir"
if ($modules.Count -eq 0) {
    Info "Next: ss add sushiruntime   (then: cd sushiruntime; sr build)"
}
