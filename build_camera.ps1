$ErrorActionPreference = 'Stop'
$frameworkDir = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$metadataDir = Join-Path $env:WINDIR 'System32\WinMetadata'
$compiler = Join-Path $frameworkDir 'csc.exe'
$cameraRefs = @('System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll')
$cameraRefs += @('System.Runtime.dll', 'System.Threading.Tasks.dll', 'System.Runtime.InteropServices.WindowsRuntime.dll') | ForEach-Object { Join-Path $frameworkDir $_ }
$cameraRefs += @('Windows.Foundation.winmd', 'Windows.Devices.winmd', 'Windows.Media.winmd', 'Windows.Storage.winmd') | ForEach-Object { Join-Path $metadataDir $_ }
$cameraArgs = @('/nologo', '/target:exe', '/platform:x64', ('/out:' + (Join-Path $PSScriptRoot 'CameraHost.exe')))
$cameraArgs += $cameraRefs | ForEach-Object { '/r:' + $_ }
$cameraArgs += Join-Path $PSScriptRoot 'CameraHost.cs'
& $compiler @cameraArgs
if ($LASTEXITCODE -ne 0) { throw 'CameraHost compilation failed.' }
Write-Output 'CameraHost.exe built. No camera was opened.'
