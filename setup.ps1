$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = $env:RTC_PYTHON
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "RTC_PYTHON does not exist: $Python"
    }
} else {
    $PreferredPython = "D:\ProgramFiles\Miniforge3\envs\rtc\python.exe"
    if (Test-Path -LiteralPath $PreferredPython) {
        $Python = $PreferredPython
    } else {
        throw "Python environment not found. Create D:\ProgramFiles\Miniforge3\envs\rtc or set RTC_PYTHON."
    }
}

Write-Host "Using Python: $Python"
& $Python -m pip install -r requirements.txt
Write-Host "Setup complete."
