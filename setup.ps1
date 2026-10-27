param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$mhCandidates = @($Python, $env:MEDIAHARVEST_PYTHON)
foreach ($mhName in @('python', 'python3')) {
    $mhCommand = Get-Command $mhName -ErrorAction SilentlyContinue
    if ($mhCommand) { $mhCandidates += $mhCommand.Source }
}
$mhCandidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$mhInterpreter = $null
foreach ($mhCandidate in $mhCandidates) {
    if (-not $mhCandidate) { continue }
    try {
        & $mhCandidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $mhInterpreter = $mhCandidate; break }
    } catch { }
}
if (-not $mhInterpreter) { throw 'Install Python 3.10+ or pass -Python with its full path.' }
& $mhInterpreter -m venv .venv --without-pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to create .venv.' }
$mhVenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $mhVenvPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw 'Failed to install pip in .venv.' }
$mhRequirements = 'requirements.txt'
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { $mhRequirements = 'requirements-gpu.txt' }
& $mhVenvPython -m pip install -r $mhRequirements
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $mhVenvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
Write-Host 'Ready. Double-click run_local_platform.bat to start.'
Write-Host 'Douyin browser collection uses installed Chrome/Edge; alternatively run: .venv\Scripts\python -m playwright install chromium'
