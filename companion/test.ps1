param([string]$ToolRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.build-tools'))
$ErrorActionPreference = 'Stop'
$java = Join-Path $ToolRoot 'jdk-17.0.20.1+1\bin'
$android = Join-Path $ToolRoot 'android-9\android.jar'
$json = Join-Path $ToolRoot 'json-20240303.jar'
if (-not (Test-Path -LiteralPath $json)) { throw 'Host tests require org.json:json:20240303. See README.md.' }
$classes = Join-Path $PSScriptRoot 'build\test-classes'
New-Item -ItemType Directory -Force -Path $classes | Out-Null
& "$java\javac.exe" -encoding UTF-8 -cp "$json;$android" -d $classes "$PSScriptRoot\src\org\androidrescue\helper\ExportEngine.java" "$PSScriptRoot\tests\ExportEngineTest.java"
if ($LASTEXITCODE -ne 0) { throw 'Test compilation failed' }
& "$java\java.exe" -cp "$classes;$json;$android" org.androidrescue.helper.ExportEngineTest
if ($LASTEXITCODE -ne 0) { throw 'Exporter tests failed' }
