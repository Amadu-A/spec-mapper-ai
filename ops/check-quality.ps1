# ops/check-quality.ps1
# Одна Windows-команда Ruff, форматирования, offline/architecture/API tests и frontend tests.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
function Invoke-Check {
    param([string]$Executable, [string[]]$Arguments)
    Get-Command $Executable -ErrorAction Stop | Out-Null
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5 считает обычный stderr native CLI ошибкой PowerShell.
        # Статус проверки определяется exit code; весь вывод остаётся видимым.
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-Output ($_.ToString()) }
        $checkExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($checkExitCode -ne 0) { throw "Проверка $Executable завершилась с кодом $checkExitCode" }
}
Invoke-Check -Executable uv -Arguments @('sync', '--locked')
Invoke-Check -Executable uv -Arguments @('run', '--no-sync', 'ruff', 'check', 'src', 'tests', 'alembic')
Invoke-Check -Executable uv -Arguments @('run', '--no-sync', 'ruff', 'format', '--check', 'src', 'tests', 'alembic')
Invoke-Check -Executable uv -Arguments @('run', '--no-sync', 'playwright', 'install', 'chromium')
Invoke-Check -Executable uv -Arguments @('run', '--no-sync', 'pytest', '-m', 'not live', '--cov=src', '--cov-report=term-missing', '-q')
Invoke-Check -Executable node -Arguments @('--test', 'tests/frontend/*.test.js')
Invoke-Check -Executable git -Arguments @('diff', '--check')
