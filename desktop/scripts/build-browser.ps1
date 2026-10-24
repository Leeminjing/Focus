param([string]$MsvcRoot, [string]$WindowsSdkRoot)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$checkout = Join-Path $root '.browser-build'
$revision = '8eea976de275a657138c73815e4b2abf174c0f67'
if (-not (Test-Path -LiteralPath $checkout)) {
    & git clone https://github.com/Leeminjing/focus-distinct-browser.git $checkout
    if ($LASTEXITCODE) { throw 'Browser source checkout failed' }
}
& git -C $checkout fetch origin $revision
if ($LASTEXITCODE) { throw 'Browser source fetch failed' }
& git -C $checkout checkout --detach $revision
if ($LASTEXITCODE) { throw 'Browser revision checkout failed' }
if ((& git -C $checkout status --porcelain)) { throw 'Browser build checkout must be clean' }
& (Join-Path $checkout 'scripts/build.ps1') -MsvcRoot $MsvcRoot -WindowsSdkRoot $WindowsSdkRoot
if ($LASTEXITCODE) { throw 'Browser build failed' }
$destination = Join-Path $root 'resources/distinct-browser'
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$binary = Join-Path $destination 'distinct-browser.exe'
Copy-Item -LiteralPath (Join-Path $checkout 'build/distinct-browser.exe') -Destination $binary -Force
$manifest = [ordered]@{ repository = 'https://github.com/Leeminjing/focus-distinct-browser'; revision = $revision;
    protocol_version = 1; platform = 'win32-x64'; sha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant() }
[IO.File]::WriteAllText((Join-Path $destination 'manifest.json'), ($manifest | ConvertTo-Json) + "`n", [Text.UTF8Encoding]::new($false))
Write-Host "Bundled $binary"
