param(
    [ValidateSet("all", "server", "agent", "caller")]
    [string]$Component = "all"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = $env:RTC_PYTHON
$PythonArguments = @()
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    $ResolvedPython = Resolve-Path -LiteralPath $Python -ErrorAction SilentlyContinue
    if (-not $ResolvedPython) {
        throw "RTC_PYTHON does not exist: $Python"
    }
    $Python = $ResolvedPython.Path
} else {
    $PathPython = Get-Command python -ErrorAction SilentlyContinue
    if ($PathPython) {
        $Python = $PathPython.Source
    } else {
        $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PyLauncher) {
            $Python = $PyLauncher.Source
            $PythonArguments = @("-3.10")
        } else {
            throw "Python not found. Activate the rtc environment or set RTC_PYTHON."
        }
    }
}

$RequirementsFile = switch ($Component) {
    "server" { "requirements-server.txt" }
    "agent" { "requirements-agent.txt" }
    "caller" { "requirements-caller.txt" }
    default { "requirements.txt" }
}

Write-Host "Using Python: $Python"
Write-Host "Installing component: $Component"
& $Python @PythonArguments -m pip install -r $RequirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed: $RequirementsFile"
}
Write-Host "Setup complete: $Component"
