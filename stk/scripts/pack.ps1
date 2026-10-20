<#
    Builds and packages the VDGET module for the SwissTechKnife host (docs/MODULES.md in the host repo):
      artifacts/vd-get-<ver>.zip     module.json + VdGet.Module.dll at the root, backend/servidor.py + sniffer.py
    The SDK dll is never packaged: the host provides it. The version comes from stk/Directory.Build.props
    and is written into module.json. Prints sha256 and size for the host catalog.
      powershell -File stk/scripts/pack.ps1
#>
[CmdletBinding()]
param([ValidateSet('Debug','Release')][string]$Configuration = 'Release')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

$stk = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $stk
$project = Join-Path $stk 'VdGet.Module'
[xml]$props = Get-Content (Join-Path $stk 'Directory.Build.props')
$version = ($props.Project.PropertyGroup | Where-Object { $_.Version } | Select-Object -First 1).Version

# module.json follows the build version.
$manifestPath = Join-Path $project 'module.json'
$utf8 = New-Object Text.UTF8Encoding $false
$manifest = [IO.File]::ReadAllText($manifestPath, $utf8)
$manifest = [regex]::Replace($manifest, '"version":\s*"[^"]*"', "`"version`": `"$version`"")
[IO.File]::WriteAllText($manifestPath, $manifest, $utf8)

dotnet build $project -c $Configuration -nologo -v q
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }

$output = Join-Path $project "bin/$Configuration/net48"
$artifacts = Join-Path $repo 'artifacts'
New-Item -ItemType Directory -Force $artifacts | Out-Null
$zipPath = Join-Path $artifacts "vd-get-$version.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

$zip = [IO.Compression.ZipFile]::Open($zipPath, 'Create')
try {
    Get-ChildItem $output -Recurse -File |
        Where-Object { $_.Name -notlike 'SwissTechKnife.Sdk.*' -and $_.Extension -notin '.pdb', '.xml' } |
        ForEach-Object {
            $relative = $_.FullName.Substring($output.Length).TrimStart('\', '/').Replace('\', '/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $relative, 'Optimal') | Out-Null
        }
} finally { $zip.Dispose() }

$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$size = (Get-Item $zipPath).Length
Write-Host "Packed $zipPath" -ForegroundColor Green
Write-Host "  version $version"
Write-Host "  sha256  $hash"
Write-Host "  size    $size"
