param([switch]$Dev, [switch]$Stable)

& {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = "Stop"

    $Repository = "https://github.com/Leeminjing/Focus.git"
    $UserProfile = [Environment]::GetFolderPath("UserProfile")
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        throw "Could not determine the current Windows user profile directory."
    }

    $FocusHome = Join-Path $UserProfile ".focus"
    $AppDir = Join-Path $FocusHome "app"
    $BinDir = Join-Path $FocusHome "bin"
    $MetadataPath = Join-Path $FocusHome "install.json"
    if ($Dev -and $Stable) { throw "Choose -Dev or -Stable, not both." }
    $Channel = $env:FOCUS_CHANNEL
    if ($Dev) { $Channel = "dev" }
    if ($Stable) { $Channel = "stable" }
    if (-not $Channel -and (Test-Path -LiteralPath $MetadataPath)) {
        $previous = Get-Content -Raw -LiteralPath $MetadataPath | ConvertFrom-Json
        if ($previous.PSObject.Properties.Name -contains "channel") { $Channel = [string]$previous.channel }
        elseif ($previous.PSObject.Properties.Name -contains "branch" -and $previous.branch -eq "main") { $Channel = "dev" }
        else { throw "Unknown Focus installation channel." }
    }
    if (-not $Channel) { $Channel = "stable" }
    if ($Channel -cnotin @("dev", "stable")) { throw "FOCUS_CHANNEL must be dev or stable." }
    $TargetRef = "main"
    if ($Channel -eq "stable") {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/Leeminjing/Focus/releases/latest" -Headers @{ "User-Agent" = "Focus-installer" } -TimeoutSec 15
        if ($release.draft -ne $false -or $release.prerelease -ne $false -or -not $release.published_at -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') {
            throw "No published stable Focus release is available."
        }
        $TargetRef = [string]$release.tag_name
    }

    function Get-ApplicationPath {
        param([Parameter(Mandatory = $true)][string]$Name)

        $application = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $application) {
            throw "Required command '$Name' was not found on PATH."
        }
        return $application.Source
    }

    # Electron 43 uses @electron/get 5 (ESM-only), requiring Node >= 22.12.0.
    # Older Node versions can leave node_modules without an Electron binary.
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
                ((($metadata.PSObject.Properties.Name -contains "channel") -and $metadata.channel -in @("dev", "stable")) -or
                    (-not ($metadata.PSObject.Properties.Name -contains "channel") -and ($metadata.PSObject.Properties.Name -contains "branch") -and $metadata.branch -eq "main")) -and
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
    Write-Host "Cloning $Channel Focus $TargetRef..."
    $stagingRoot = Join-Path $FocusHome "staging"
    $stagingApp = Join-Path $stagingRoot "app-$PID"
    $controllerApp = Join-Path $stagingRoot "controller-$PID"
    New-Item -ItemType Directory -Force -Path $stagingRoot | Out-Null
    try {
        Invoke-Native $git @("clone", "--branch", $TargetRef, "--single-branch", $Repository, $stagingApp)
        if (-not $existingInstall) {
            $resolvedApp = [System.IO.Path]::GetFullPath($AppDir)
            if ($resolvedApp -ne [System.IO.Path]::GetFullPath((Join-Path $FocusHome "app"))) { throw "Unexpected app destination." }
            if (-not [System.IO.Path]::GetFullPath($stagingApp).StartsWith([System.IO.Path]::GetFullPath($stagingRoot).TrimEnd("\") + "\", [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected staging source." }
            Move-Item -LiteralPath $stagingApp -Destination $resolvedApp
        }

        if (-not $existingInstall) {
            $metadata = [ordered]@{
                schema = 1
                managed = $true
                repository = $Repository
                channel = $Channel
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

        # Run the staged controller without replacing the installed CLI. An older
        # Release may predate the dual-channel protocol; bootstrap its controller
        # separately from main while the update target remains the selected Release.
        $cliSource = $AppDir
        if ($existingInstall) { $cliSource = $stagingApp }
        if (-not (Test-Path -LiteralPath (Join-Path $cliSource "scripts\cli-protocol.json"))) {
            Invoke-Native $git @("clone", "--branch", "main", "--single-branch", $Repository, $controllerApp)
            $cliSource = $controllerApp
        }
        $cliDir = Join-Path $FocusHome "runtime\cli"
        New-Item -ItemType Directory -Force -Path $cliDir | Out-Null
        $cliScript = Join-Path $cliSource "scripts\focus.ps1"
        if (-not (Test-Path -LiteralPath $cliScript -PathType Leaf)) {
            throw "The installed repository does not contain scripts\focus.ps1."
        }

        $powershell = Join-Path $PSHOME "powershell.exe"
        if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
            $powershell = Get-ApplicationPath "powershell.exe"
        }
        $previousUpdateApp = $env:FOCUS_UPDATE_APP
        try {
            $env:FOCUS_UPDATE_APP = $AppDir
            $wasRecovery = Test-Path -LiteralPath (Join-Path $FocusHome "update-state.json")
            Invoke-Native $powershell @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $cliScript, "update", "--$Channel")
            # Recovery ends an invocation without accepting a new CLI. Leave the
            # old launcher intact and ask for another installer run in that case.
            if ($wasRecovery -or -not (Test-Path -LiteralPath (Join-Path $cliDir "current.json"))) { throw "Recovery completed. Run this installer again to finish the upgrade." }
        } finally {
            $env:FOCUS_UPDATE_APP = $previousUpdateApp
        }

        Copy-Item -LiteralPath (Join-Path $cliSource "scripts\cli-launch.cjs") -Destination (Join-Path $cliDir "cli-launch.cjs.tmp") -Force
        Move-Item -LiteralPath (Join-Path $cliDir "cli-launch.cjs.tmp") -Destination (Join-Path $cliDir "cli-launch.cjs") -Force
        $launcher = '@echo off' + "`r`n" + 'setlocal' + "`r`n" +
            'set "FOCUS_UPDATE_APP=%USERPROFILE%\.focus\app"' + "`r`n" +
            'node "%USERPROFILE%\.focus\runtime\cli\cli-launch.cjs" %*' + "`r`n"
        $launcherPath = Join-Path $BinDir "focus.cmd"
        [System.IO.File]::WriteAllText("$launcherPath.tmp", $launcher, [System.Text.Encoding]::ASCII)
        Move-Item -LiteralPath "$launcherPath.tmp" -Destination $launcherPath -Force
        Add-UserPathEntry $BinDir

        Write-Host ""
        Write-Host "Focus installed successfully."
        Write-Host ""
        Write-Host "On first launch, Model Settings will open. Enter your API key and save to verify chat and curation."
        Write-Host "Optional environment-file configuration: $globalEnv"
        Write-Host ""
        Write-Host "Run:"
        Write-Host "    focus"
        Write-Host ""
        Write-Host "Update later with:"
        Write-Host "    focus update"
    } finally {
        $safeRoot = [System.IO.Path]::GetFullPath($stagingRoot).TrimEnd("\") + "\"
        foreach ($candidate in @($stagingApp, $controllerApp)) {
            $resolved = [System.IO.Path]::GetFullPath($candidate)
            if (-not $resolved.StartsWith($safeRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe staging cleanup path." }
            if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
        }
    }
}
