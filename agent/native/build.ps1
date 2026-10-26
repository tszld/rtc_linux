param(
  [string]$SdkRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $SdkRoot) {
  $SdkRoot = Join-Path `
    $PSScriptRoot `
    "vendor\AliVCSDK_ARTC-7.6.0"
}
$sdkRootResolved = (Resolve-Path -LiteralPath $SdkRoot).Path
$sourcePath = Join-Path $PSScriptRoot "rtc_native_audio.cpp"
$includePath = Join-Path $sdkRootResolved "include"
$libraryPath = Join-Path $sdkRootResolved "x64\Release"
$buildPath = Join-Path $PSScriptRoot "build"
$outputPath = Join-Path $PSScriptRoot "bin"
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"

foreach ($requiredPath in @(
  $sourcePath,
  (Join-Path $includePath "rtc\engine_interface.h"),
  (Join-Path $libraryPath "AliRTCSdk.lib")
)) {
  if (-not (Test-Path -LiteralPath $requiredPath)) {
    throw "Missing build input: $requiredPath"
  }
}
if (-not (Test-Path -LiteralPath $vswhere)) {
  throw "Missing Visual Studio Installer vswhere.exe"
}

$installationPath = & $vswhere `
  -latest `
  -products * `
  -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
  -property installationPath
if (-not $installationPath) {
  throw "Visual Studio C++ x64 build tools are not installed"
}
$vsDevCmd = Join-Path $installationPath "Common7\Tools\VsDevCmd.bat"
if (-not (Test-Path -LiteralPath $vsDevCmd)) {
  throw "Missing Visual Studio developer environment: $vsDevCmd"
}

New-Item -ItemType Directory -Force -Path $buildPath, $outputPath | Out-Null

$outputExe = Join-Path $outputPath "rtc_native_audio.exe"
$compileCommand = @(
  "call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul"
  "cd /d `"$buildPath`""
  "cl /nologo /utf-8 /std:c++17 /EHsc /MD /O2 /DNDEBUG /I`"$includePath`" `"$sourcePath`" /link /LIBPATH:`"$libraryPath`" AliRTCSdk.lib ole32.lib user32.lib gdi32.lib advapi32.lib /OUT:`"$outputExe`""
) -join " && "

& cmd.exe /d /s /c $compileCommand
if ($LASTEXITCODE -ne 0) {
  throw "C++ native audio helper build failed with exit code $LASTEXITCODE"
}

Get-ChildItem -LiteralPath $libraryPath -Filter "*.dll" -File | ForEach-Object {
  Copy-Item `
    -LiteralPath $_.FullName `
    -Destination (Join-Path $outputPath $_.Name) `
    -Force
}

$vcRedist = Get-ChildItem `
  -LiteralPath (Join-Path $installationPath "VC\Redist\MSVC") `
  -Filter "vc_redist.x64.exe" `
  -File `
  -Recurse `
  -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($null -eq $vcRedist) {
  Write-Warning "Visual Studio vc_redist.x64.exe was not found"
} else {
  Copy-Item `
    -LiteralPath $vcRedist.FullName `
    -Destination (Join-Path $outputPath "vc_redist.x64.exe") `
    -Force
  Write-Output ("VC++ x64 Redistributable: " + $vcRedist.FullName)
}

Write-Output ("Build completed: " + $outputExe)
Write-Output ("Runtime directory: " + $outputPath)
