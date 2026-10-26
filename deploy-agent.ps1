[CmdletBinding()]
param(
  [string]$PythonPath = "",
  [switch]$NoBrowser,
  [switch]$CheckOnly,
  [switch]$SkipVCRuntimeInstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$projectRoot = $PSScriptRoot
$runtimeDirectory = Join-Path $projectRoot "agent\native\bin"
$requiredRuntimeFiles = @(
  "rtc_native_audio.exe",
  "AliRTCSdk.dll",
  "alivcffmpeg.dll",
  "alivcx265.dll",
  "PluginAAC.dll",
  "x264.dll",
  "vc_redist.x64.exe"
)
$requiredVCRuntimeFiles = @(
  "msvcp140.dll",
  "vcruntime140.dll",
  "vcruntime140_1.dll"
)

function Resolve-PythonExecutable {
  param([string]$ExplicitPath)

  $candidates = [System.Collections.Generic.List[string]]::new()
  if ($ExplicitPath) {
    $candidates.Add($ExplicitPath)
  }
  if ($env:CONDA_PREFIX) {
    $candidates.Add((Join-Path $env:CONDA_PREFIX "python.exe"))
  }
  $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
  if ($pythonCommand) {
    $candidates.Add($pythonCommand.Source)
  }

  foreach ($candidate in $candidates) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) {
      continue
    }
    & $candidate -c "import sys" *> $null
    if ($LASTEXITCODE -eq 0) {
      return (Resolve-Path -LiteralPath $candidate).Path
    }
  }
  throw "Python was not found. Activate the rtc environment or pass -PythonPath."
}

function Test-VCRuntimeInstalled {
  foreach ($fileName in $requiredVCRuntimeFiles) {
    $path = Join-Path $env:WINDIR "System32\$fileName"
    if (-not (Test-Path -LiteralPath $path)) {
      return $false
    }
  }
  return $true
}

function Test-WebBrowserInstalled {
  $candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
      return $true
    }
  }
  return $false
}

try {
  $pythonExecutable = Resolve-PythonExecutable -ExplicitPath $PythonPath
  $pythonInfo = & $pythonExecutable -c (
    "import platform,sys;" +
    "print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.architecture()[0]}');" +
    "print(sys.executable)"
  )
  if ($LASTEXITCODE -ne 0 -or $pythonInfo.Count -lt 2) {
    throw "Unable to read Python version information."
  }
  $pythonVersion, $pythonArchitecture = $pythonInfo[0].Split("|")
  if ($pythonVersion -ne "3.10" -or $pythonArchitecture -ne "64bit") {
    throw "Python 3.10 x64 is required; found $pythonVersion $pythonArchitecture."
  }
  Write-Host "Python: $($pythonInfo[1])"

  $missingRuntimeFiles = @(
    $requiredRuntimeFiles |
      Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $runtimeDirectory $_))
      }
  )
  if ($missingRuntimeFiles.Count -gt 0) {
    throw (
      "Windows agent runtime files are missing: " +
      ($missingRuntimeFiles -join ", ") +
      ". Use a release package or ask a developer to build the runtime."
    )
  }
  Write-Host "ARTC helper and runtime DLLs: OK"

  $configPath = Join-Path $projectRoot "config.json"
  if (-not (Test-Path -LiteralPath $configPath)) {
    throw "config.json is missing. Create it from config.example.json."
  }
  $config = Get-Content -LiteralPath $configPath -Encoding UTF8 -Raw |
    ConvertFrom-Json
  $controlUrl = [string]$config.cluster.control_url
  if (-not $controlUrl) {
    throw "config.json is missing cluster.control_url."
  }
  if ($null -eq $config.agent) {
    throw "config.json is missing the agent section."
  }
  Write-Host "Control server: $controlUrl"

  if (-not (Test-VCRuntimeInstalled)) {
    $redistPath = Join-Path $runtimeDirectory "vc_redist.x64.exe"
    if ($SkipVCRuntimeInstall) {
      Write-Warning "VC++ x64 runtime is incomplete; installation was skipped."
    } else {
      Write-Host "Installing Microsoft Visual C++ 2015-2022 x64 runtime..."
      $installer = Start-Process `
        -FilePath $redistPath `
        -ArgumentList "/install", "/passive", "/norestart" `
        -Verb RunAs `
        -Wait `
        -PassThru
      if ($installer.ExitCode -notin @(0, 1638, 3010)) {
        throw "VC++ runtime installation failed with exit code $($installer.ExitCode)."
      }
      if ($installer.ExitCode -eq 3010) {
        Write-Warning "VC++ runtime installation requires a Windows restart."
      }
      if (-not (Test-VCRuntimeInstalled)) {
        throw "VC++ runtime DLLs are still missing. Restart Windows and retry."
      }
    }
  } else {
    Write-Host "Microsoft Visual C++ 2015-2022 x64 runtime: OK"
  }

  if (Test-WebBrowserInstalled) {
    Write-Host "Chrome/Edge: OK"
  } else {
    Write-Warning "Chrome or Edge was not found; the agent page may not open."
  }

  if ($CheckOnly) {
    Write-Host "Windows agent deployment check passed."
    exit 0
  }

  $arguments = @("main.py", "agent")
  if ($NoBrowser) {
    $arguments += "--no-browser"
  }
  Write-Host "Starting Windows agent..."
  Push-Location $projectRoot
  try {
    & $pythonExecutable @arguments
    exit $LASTEXITCODE
  } finally {
    Pop-Location
  }
} catch {
  Write-Host ""
  Write-Host "Windows agent failed to start: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
