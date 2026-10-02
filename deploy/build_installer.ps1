<#
Build the single-click Windows installer (ChartReviewAssistant-Setup.exe).

Stages the payload (a bundled Python 3.12 with the built wheel and the uv.lock-pinned dependencies
installed into it, launchers, config templates, third-party licenses, icon) into
deploy\build\payload, then compiles
deploy\installer.iss with ISCC. Reuses the machine's uv, the per-user Inno Setup install and the
.NET Framework csc by default; override with -Uv / -Iscc / -Csc.
#>
param(
    [string]$Uv = "$env:USERPROFILE\.local\bin\uv.exe",
    [string]$Iscc = "$env:USERPROFILE\.local\InnoSetup6\ISCC.exe",
    [string]$Csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)

$ErrorActionPreference = 'Stop'
$deploy = $PSScriptRoot
$repo = Split-Path -Parent $deploy
$pkg = Join-Path $repo 'chart_review_assistant'

if (-not (Test-Path $Uv)) { throw "uv not found at $Uv" }
if (-not (Test-Path $Iscc)) { throw "ISCC not found at $Iscc" }
if (-not (Test-Path $Csc)) { throw "csc not found at $Csc" }

# Version is the single source of truth in the package __init__.
$initText = Get-Content (Join-Path $pkg '__init__.py') -Raw
$match = [regex]::Match($initText, "__version__\s*=\s*'([^']+)'")
if (-not $match.Success) { throw 'could not read __version__ from __init__.py' }
$version = $match.Groups[1].Value
Write-Host "Building installer for version $version"

# Fresh wheel
& $Uv build --wheel --directory $repo
if ($LASTEXITCODE -ne 0) { throw 'uv build failed' }
$wheel = Get-ChildItem (Join-Path $repo 'dist') -Filter '*.whl' |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $wheel) { throw 'no wheel produced' }

# Stage a clean payload.
$payload = Join-Path $deploy 'build\payload'
if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
New-Item -ItemType Directory -Path $payload | Out-Null
New-Item -ItemType Directory -Path (Join-Path $payload 'data') | Out-Null

Copy-Item (Join-Path $deploy 'files\THIRD_PARTY_LICENSES.txt') $payload
Copy-Item (Join-Path $deploy 'files\ChartReviewAssistant-debug.cmd') $payload
Copy-Item (Join-Path $pkg 'assets\icons\icon_blue_black_1024.ico') (Join-Path $payload 'icon.ico')

# Compile the native launcher exe (icon embedded) so the shortcuts point at a normal .exe.
$launcherExe = Join-Path $payload 'ChartReviewAssistant.exe'
$launcherSrc = Join-Path $deploy 'files\launcher.cs'
& $Csc /nologo /target:winexe "/win32icon:$(Join-Path $payload 'icon.ico')" "/out:$launcherExe" $launcherSrc
if ($LASTEXITCODE -ne 0) { throw 'csc failed to build the launcher exe' }

# Same launcher under a -Demo name; launcher.cs switches to demo mode from its exe name.
$demoExe = Join-Path $payload 'ChartReviewAssistant-Demo.exe'
& $Csc /nologo /target:winexe "/win32icon:$(Join-Path $payload 'icon.ico')" "/out:$demoExe" $launcherSrc
if ($LASTEXITCODE -ne 0) { throw 'csc failed to build the demo launcher exe' }

# Bundle the runtime: a relocatable uv-managed Python 3.12 copied into payload\python, with the
# app and its pinned dependencies installed straight into it. The installer is then a plain file
# copy; nothing is downloaded on the user's machine.
$pyDownload = Join-Path $deploy 'build\python-dl'
$env:UV_PYTHON_INSTALL_DIR = $pyDownload
& $Uv python install 3.12 --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw 'uv python install failed' }
$pyDir = Get-ChildItem $pyDownload -Directory | Where-Object Name -match '^cpython-3\.12\.\d+' |
    Select-Object -Last 1
if (-not $pyDir) { throw 'uv python install produced no cpython-3.12 dir' }
$runtime = Join-Path $payload 'python'
Copy-Item $pyDir.FullName $runtime -Recurse

# The copy is no longer uv-managed; drop uv's marker so packages can be installed into it.
Remove-Item (Join-Path $runtime 'Lib\EXTERNALLY-MANAGED')

# Pin every dependency, transitive ones included, to the versions in uv.lock: export the lock as a
# flat requirements.txt and install from it, so each install matches the dev machine.
$requirements = Join-Path $deploy 'build\requirements.txt'
& $Uv export --frozen --no-dev --no-emit-project --format requirements-txt --directory $repo `
    -o $requirements
if ($LASTEXITCODE -ne 0) { throw 'uv export failed' }
& $Uv pip install --python (Join-Path $runtime 'python.exe') -r $requirements $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw 'uv pip install into the bundled runtime failed' }
Copy-Item (Join-Path $pkg 'clinic_config.example.toml') (Join-Path $payload 'data')
Copy-Item (Join-Path $pkg 'db_config.example.toml') (Join-Path $payload 'data')

# Compile.
$dist = Join-Path $repo 'dist'
& $Iscc "/DAppVersion=$version" "/DPayloadDir=$payload" "/DOutputDir=$dist" (Join-Path $deploy 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'ISCC failed' }

Write-Host "Built $dist\ChartReviewAssistant-Setup.exe"
