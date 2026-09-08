[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$FocusHome = Split-Path -Parent $AppDir
$DesktopDir = Join-Path $AppDir "desktop"
$HarnessDir = Join-Path $AppDir "backend\packages\harness"
$RuntimeDir = Join-Path $FocusHome "runtime"
$VenvDir = Join-Path $RuntimeDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$MetadataPath = Join-Path $FocusHome "install.json"
$LocksDir = Join-Path $FocusHome "locks"
$ExpectedRepository = "https://github.com/Leeminjing/Focus.git"
$ExpectedBranch = "main"

function Get-ApplicationPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $application = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $application) {
        throw "Required command '$Name' was not found on PATH."
    }
    return $application.Source
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($ArgumentList -join ' ')"
    }
}

function Invoke-NativeOutput {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    $output = & $FilePath @ArgumentList 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($ArgumentList -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return (($output | Out-String).Trim())
}

function Normalize-RepositoryUrl {
    param([Parameter(Mandatory = $true)][string]$Value)

    $normalized = $Value.Trim().TrimEnd("/").ToLowerInvariant()
    if ($normalized.EndsWith(".git")) {
        $normalized = $normalized.Substring(0, $normalized.Length - 4)
    }
    return $normalized
}

function Assert-ManagedInstall {
    if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
        throw "This is not a managed Focus installation: $MetadataPath is missing. Reinstall Focus with install.ps1."
    }

    try {
        $metadata = Get-Content -Raw -LiteralPath $MetadataPath | ConvertFrom-Json
    } catch {
        throw "Focus installation metadata is invalid: $MetadataPath"
    }

    $managed = $metadata.PSObject.Properties.Name -contains "managed" -and $metadata.managed -eq $true
    $repositoryMatches = $metadata.PSObject.Properties.Name -contains "repository" -and
        (Normalize-RepositoryUrl ([string]$metadata.repository)) -eq (Normalize-RepositoryUrl $ExpectedRepository)
    $branchMatches = $metadata.PSObject.Properties.Name -contains "branch" -and [string]$metadata.branch -eq $ExpectedBranch
    $pathMatches = $metadata.PSObject.Properties.Name -contains "appPath" -and
        [System.IO.Path]::GetFullPath([string]$metadata.appPath) -eq $AppDir

    if (-not ($managed -and $repositoryMatches -and $branchMatches -and $pathMatches)) {
        throw "Focus refused to manage this directory because install.json does not match the expected app, repository, and branch."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir ".git") -PathType Container)) {
        throw "Focus managed app is not a Git checkout: $AppDir"
    }
}

function Open-ExclusiveLock {
    param([Parameter(Mandatory = $true)][string]$Name)

    New-Item -ItemType Directory -Force -Path $LocksDir | Out-Null
    $path = Join-Path $LocksDir $Name
    return [System.IO.File]::Open($path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
}

function Assert-FocusIsStopped {
    $probe = $null
    try {
        $probe = Open-ExclusiveLock "running.lock"
    } catch [System.IO.IOException] {
        throw "Focus is running. Close it before running 'focus update'."
    } finally {
        if ($null -ne $probe) {
            $probe.Dispose()
        }
    }
}

function Resolve-PythonRuntime {
    $candidates = @(
        [PSCustomObject]@{ File = "py.exe"; Prefix = @("-3") },
        [PSCustomObject]@{ File = "python.exe"; Prefix = @() },
        [PSCustomObject]@{ File = "python"; Prefix = @() }
    )

    foreach ($candidate in $candidates) {
        $application = Get-Command -Name $candidate.File -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $application) {
            continue
        }

        $version = & $application.Source @($candidate.Prefix) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) {
            continue
        }

        $parts = ([string]$version).Trim().Split(".")
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)) {
            return [PSCustomObject]@{ File = $application.Source; Prefix = @($candidate.Prefix) }
        }
    }

    throw "Python 3.11 or newer was not found. Install Python and run 'focus update' again."
}

function Ensure-PythonEnvironment {
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return
    }

    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $runtime = Resolve-PythonRuntime
    Write-Host "Creating the managed Python environment..."
    Invoke-Native $runtime.File (@($runtime.Prefix) + @("-m", "venv", $VenvDir))
}

function Test-DependenciesReady {
    $electronPackage = Join-Path $DesktopDir "node_modules\electron\package.json"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $electronPackage -PathType Leaf)) {
        return $false
    }

    & $VenvPython -c "import alembic, asyncpg, fastapi, psycopg, uvicorn" *> $null
    return $LASTEXITCODE -eq 0
}

function Sync-FocusDependencies {
    $npm = Get-ApplicationPath "npm.cmd"
    Ensure-PythonEnvironment

    Write-Host "Updating desktop dependencies..."
    Invoke-Native $npm @("--prefix", $DesktopDir, "ci")

    Write-Host "Updating Python dependencies..."
    $desktopRequirement = "$HarnessDir[desktop]"
    Invoke-Native $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "-e", $desktopRequirement)
    Invoke-Native $VenvPython @("-c", "import alembic, asyncpg, fastapi, psycopg, uvicorn")
}

function Start-Focus {
    Assert-ManagedInstall
    if (-not (Test-DependenciesReady)) {
        throw "Focus dependencies are missing. Run 'focus update' first."
    }

    $npm = Get-ApplicationPath "npm.cmd"
    $runLock = $null
    try {
        try {
            $runLock = Open-ExclusiveLock "running.lock"
        } catch [System.IO.IOException] {
            throw "Focus is already running."
        }

        $env:FOCUS_GLOBAL_HOME = $FocusHome
        $env:FOCUS_MANAGED_APP = $AppDir
        $env:FOCUS_PYTHON = $VenvPython
        Push-Location $DesktopDir
        try {
            Invoke-Native $npm @("start")
        } finally {
            Pop-Location
        }
    } finally {
        if ($null -ne $runLock) {
            $runLock.Dispose()
        }
    }
}

function Update-Focus {
    Assert-ManagedInstall
    Assert-FocusIsStopped

    $updateLock = $null
    try {
        try {
            $updateLock = Open-ExclusiveLock "update.lock"
        } catch [System.IO.IOException] {
            throw "Another Focus update is already running."
        }

        $git = Get-ApplicationPath "git.exe"
        $origin = Invoke-NativeOutput $git @("-C", $AppDir, "remote", "get-url", "origin")
        if ((Normalize-RepositoryUrl $origin) -ne (Normalize-RepositoryUrl $ExpectedRepository)) {
            throw "Focus refused to update because origin is not $ExpectedRepository."
        }

        $previousCommit = Invoke-NativeOutput $git @("-C", $AppDir, "rev-parse", "HEAD")
        Write-Host "Fetching Focus..."
        Invoke-Native $git @("-C", $AppDir, "fetch", "--prune", "origin", $ExpectedBranch)
        $targetCommit = Invoke-NativeOutput $git @("-C", $AppDir, "rev-parse", "origin/$ExpectedBranch")

        Write-Host "Updating $($previousCommit.Substring(0, 7)) -> $($targetCommit.Substring(0, 7))..."
        Invoke-Native $git @("-C", $AppDir, "reset", "--hard", "origin/$ExpectedBranch")

        try {
            Sync-FocusDependencies
        } catch {
            $updateFailure = $_
            if ($previousCommit -ne $targetCommit) {
                Write-Warning "Dependency update failed. Restoring $($previousCommit.Substring(0, 7))..."
                Invoke-Native $git @("-C", $AppDir, "reset", "--hard", $previousCommit)
                try {
                    Sync-FocusDependencies
                } catch {
                    Write-Warning "The previous dependency set could not be fully restored: $($_.Exception.Message)"
                }
            }
            throw "Focus update failed: $($updateFailure.Exception.Message)"
        }

        if ($previousCommit -eq $targetCommit) {
            Write-Host "Focus is up to date ($($targetCommit.Substring(0, 7)))."
        } else {
            Write-Host "Focus updated successfully: $($previousCommit.Substring(0, 7)) -> $($targetCommit.Substring(0, 7))."
        }
    } finally {
        if ($null -ne $updateLock) {
            $updateLock.Dispose()
        }
    }
}

$Command = ""
if ($null -ne $Arguments -and $Arguments.Count -gt 0) {
    $Command = $Arguments[0]
}
if ($null -ne $Arguments -and $Arguments.Count -gt 1) {
    throw "Usage: focus [update]"
}

switch ($Command) {
    { [string]::IsNullOrWhiteSpace($_) } { Start-Focus; break }
    "update" { Update-Focus; break }
    default { throw "Unknown command '$Command'. Usage: focus [update]" }
}
