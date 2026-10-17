& {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = "Stop"

    $Repository = "https://github.com/Leeminjing/Focus.git"
    # Only published stable releases are eligible for public installation.
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/Leeminjing/Focus/releases/latest" -Headers @{ "User-Agent" = "Focus-installer" } -TimeoutSec 15
    if ($release.draft -ne $false -or $release.prerelease -ne $false -or -not $release.published_at -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') {
        throw "No published stable Focus release is available."
    }
    $ReleaseTag = [string]$release.tag_name
    $UserProfile = [Environment]::GetFolderPath("UserProfile")
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        throw "Could not determine the current Windows user profile directory."
    }

    $FocusHome = Join-Path $UserProfile ".focus"
    $AppDir = Join-Path $FocusHome "app"
    $BinDir = Join-Path $FocusHome "bin"
    $MetadataPath = Join-Path $FocusHome "install.json"

    function Get-ApplicationPath {
        param([Parameter(Mandatory = $true)][string]$Name)

        $application = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $application) {
            throw "Required command '$Name' was not found on PATH."
        }
        return $application.Source
    }

    # electron 43 的安装器依赖 @electron/get 5（ESM-only），要求 Node >= 22.12.0；
    # 更低版本无法执行安装器，会留下没有二进制的残缺 node_modules。
    function Assert-NodeRuntime {
        $node = Get-ApplicationPath "node.exe"

        $version = & $node -e "const [major, minor] = process.versions.node.split('.').map(Number); console.log(major + '.' + minor)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) {
            $parts = ([string]$version).Trim().Split(".")
            if ([int]$parts[0] -gt 22 -or ([int]$parts[0] -eq 22 -and [int]$parts[1] -ge 12)) {
                return $node
            }
        }

        throw "Node.js 22.12.0 or newer is required (found $( & $node --version ))."
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

    function Normalize-RepositoryUrl {
        param([Parameter(Mandatory = $true)][string]$Value)

        $normalized = $Value.Trim().TrimEnd("/").ToLowerInvariant()
        if ($normalized.EndsWith(".git")) {
            $normalized = $normalized.Substring(0, $normalized.Length - 4)
        }
        return $normalized
    }

    function Test-ExistingManagedInstall {
        if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
            return $false
        }
        try {
            $metadata = Get-Content -Raw -LiteralPath $MetadataPath | ConvertFrom-Json
            return $metadata.managed -eq $true -and
                (Normalize-RepositoryUrl ([string]$metadata.repository)) -eq (Normalize-RepositoryUrl $Repository) -and
                ((($metadata.PSObject.Properties.Name -contains "channel") -and $metadata.channel -eq "stable") -or
                    (($metadata.PSObject.Properties.Name -contains "branch") -and $metadata.branch -eq "main")) -and
                [System.IO.Path]::GetFullPath([string]$metadata.appPath) -eq [System.IO.Path]::GetFullPath($AppDir)
        } catch {
            return $false
        }
    }

    function Add-UserPathEntry {
        param([Parameter(Mandatory = $true)][string]$Path)

        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $entries = @()
        if (-not [string]::IsNullOrWhiteSpace($userPath)) {
            $entries = @($userPath.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries))
        }
        $alreadyPresent = $entries | Where-Object { $_.TrimEnd("\") -ieq $Path.TrimEnd("\") }
        if (-not $alreadyPresent) {
            $newUserPath = (@($entries) + @($Path)) -join ";"
            [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        }
        if (-not (($env:Path.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)) | Where-Object { $_.TrimEnd("\") -ieq $Path.TrimEnd("\") })) {
            $env:Path = "$Path;$env:Path"
        }
    }

    if ($env:OS -ne "Windows_NT") {
        throw "This installer currently supports Windows only."
    }

    $git = Get-ApplicationPath "git.exe"
    Assert-NodeRuntime | Out-Null
    Get-ApplicationPath "npm.cmd" | Out-Null
    Get-ApplicationPath "docker.exe" | Out-Null

    New-Item -ItemType Directory -Force -Path $FocusHome, $BinDir, (Join-Path $FocusHome "plugins"), (Join-Path $FocusHome "users") | Out-Null

    $existingInstall = Test-Path -LiteralPath $AppDir
    if ($existingInstall) {
        if (-not (Test-ExistingManagedInstall)) {
            throw "$AppDir already exists but is not a Focus managed installation. Move it elsewhere, then run the installer again."
        }
        Write-Host "Existing Focus installation found. Updating it..."
    }
    Write-Host "Cloning stable Focus $ReleaseTag..."
    $stagingRoot = Join-Path $FocusHome "staging"
    $stagingApp = Join-Path $stagingRoot "app-$PID"
    New-Item -ItemType Directory -Force -Path $stagingRoot | Out-Null
    try {
        Invoke-Native $git @("clone", "--branch", $ReleaseTag, "--single-branch", $Repository, $stagingApp)
        if (-not $existingInstall) {
            Move-Item -LiteralPath $stagingApp -Destination $AppDir
        }
    } catch {
        $resolvedStagingRoot = [System.IO.Path]::GetFullPath($stagingRoot).TrimEnd("\") + "\"
        $resolvedStagingApp = [System.IO.Path]::GetFullPath($stagingApp)
        if ($resolvedStagingApp.StartsWith($resolvedStagingRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $resolvedStagingApp)) {
            Remove-Item -LiteralPath $resolvedStagingApp -Recurse -Force
        }
        throw
    }

    if (-not $existingInstall) {
        $metadata = [ordered]@{
            schema = 1
            managed = $true
            repository = $Repository
            channel = "stable"
            release = $ReleaseTag
            appPath = $AppDir
            installedAt = [DateTime]::UtcNow.ToString("o")
        }
        $metadata | ConvertTo-Json | Set-Content -LiteralPath $MetadataPath -Encoding UTF8
    }

    $globalConfig = Join-Path $FocusHome "config.yaml"
    if (-not (Test-Path -LiteralPath $globalConfig)) {
        "# Focus global configuration fallback layer.`n" | Set-Content -LiteralPath $globalConfig -Encoding UTF8
    }
    $globalExtensions = Join-Path $FocusHome "extensions_config.json"
    if (-not (Test-Path -LiteralPath $globalExtensions)) {
        '{"mcpServers": {}}' | Set-Content -LiteralPath $globalExtensions -Encoding UTF8
    }
    $globalEnv = Join-Path $FocusHome ".env"
    if (-not (Test-Path -LiteralPath $globalEnv)) {
        Copy-Item -LiteralPath (Join-Path $AppDir ".env.example") -Destination $globalEnv
    }

    # Keep the validated CLI outside the checkout. It remains available if a
    # failed migration requires restoring an older main-based checkout.
    $cliSource = $AppDir
    if ($existingInstall) { $cliSource = $stagingApp }
    $cliDir = Join-Path $FocusHome "runtime\cli"
    New-Item -ItemType Directory -Force -Path $cliDir | Out-Null
    foreach ($file in @("focus.ps1", "managed-update.cjs")) {
        Copy-Item -LiteralPath (Join-Path $cliSource "scripts\$file") -Destination (Join-Path $cliDir $file) -Force
    }
    $cliScript = Join-Path $cliDir "focus.ps1"
    if (-not (Test-Path -LiteralPath $cliScript -PathType Leaf)) {
        throw "The installed repository does not contain scripts\focus.ps1."
    }

    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        $powershell = Get-ApplicationPath "powershell.exe"
    }
    $launcher = '@echo off' + "`r`n" + 'setlocal' + "`r`n" +
        'set "FOCUS_UPDATE_APP=%USERPROFILE%\.focus\app"' + "`r`n" +
        'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.focus\runtime\cli\focus.ps1" %*' + "`r`n"
    $launcherPath = Join-Path $BinDir "focus.cmd"
    [System.IO.File]::WriteAllText($launcherPath, $launcher, [System.Text.Encoding]::ASCII)
    Add-UserPathEntry $BinDir

    $previousUpdateApp = $env:FOCUS_UPDATE_APP
    try {
        $env:FOCUS_UPDATE_APP = $AppDir
        Invoke-Native $powershell @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $cliScript, "update")
    } finally {
        $env:FOCUS_UPDATE_APP = $previousUpdateApp
    }

    Write-Host ""
    Write-Host "Focus installed successfully."
    Write-Host ""
    Write-Host "Before the first run, add your API key to:"
    Write-Host "    $globalEnv"
    Write-Host ""
    Write-Host "Run:"
    Write-Host "    focus"
    Write-Host ""
    Write-Host "Update later with:"
    Write-Host "    focus update"
}
