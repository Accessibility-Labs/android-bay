param(
    [string]$ToolRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.build-tools')
)
$ErrorActionPreference = 'Stop'
$jdkRoot = Join-Path $ToolRoot 'jdk-17.0.20.1+1'
$sdkBuild = Join-Path $ToolRoot 'android-15'
$androidJar = Join-Path $ToolRoot 'android-9\android.jar'
foreach ($required in @("$jdkRoot\bin\javac.exe", "$sdkBuild\aapt.exe", $androidJar)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Missing build tool: $required. See README.md for verified downloads." }
}
$build = Join-Path $PSScriptRoot 'build'
if (Test-Path -LiteralPath $build) {
    $resolvedBuild = [IO.Path]::GetFullPath($build)
    $expectedBuild = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'build'))
    if ($resolvedBuild -ne $expectedBuild -or (Split-Path $resolvedBuild -Leaf) -ne 'build') { throw 'Build folder safety check failed' }
    Remove-Item -LiteralPath $resolvedBuild -Recurse -Force
}
New-Item -ItemType Directory -Force -Path "$build\classes", "$build\dex" | Out-Null
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build step failed ($LASTEXITCODE): $Executable" }
}
$sourceFiles = @(Get-ChildItem -LiteralPath "$PSScriptRoot\src" -Filter '*.java' -Recurse | ForEach-Object FullName)
Invoke-Checked "$jdkRoot\bin\javac.exe" (@('-encoding','UTF-8','-source','8','-target','8','-bootclasspath',$androidJar,'-d',"$build\classes") + $sourceFiles)
Invoke-Checked "$jdkRoot\bin\jar.exe" @('cf',"$build\classes.jar",'-C',"$build\classes",'.')
Invoke-Checked "$jdkRoot\bin\java.exe" @('-cp',"$sdkBuild\lib\d8.jar",'com.android.tools.r8.D8','--release','--min-api','19','--lib',$androidJar,'--output',"$build\dex","$build\classes.jar")
Invoke-Checked "$sdkBuild\aapt.exe" @('package','-f','-M',"$PSScriptRoot\AndroidManifest.xml",'-S',"$PSScriptRoot\res",'-I',$androidJar,'-F',"$build\unsigned.apk")
Push-Location "$build\dex"
try { Invoke-Checked "$sdkBuild\aapt.exe" @('add',"$build\unsigned.apk",'classes.dex') } finally { Pop-Location }
Invoke-Checked "$sdkBuild\zipalign.exe" @('-f','4',"$build\unsigned.apk","$build\aligned.apk")
# Local development signing key stays outside the deliverable. Do not distribute it.
$key = Join-Path $ToolRoot 'android-rescue-development.jks'
if (-not (Test-Path -LiteralPath $key)) {
    Invoke-Checked "$jdkRoot\bin\keytool.exe" @('-genkeypair','-keystore',$key,'-alias','androidrescue','-storepass','androidrescue-development','-keypass','androidrescue-development','-keyalg','RSA','-keysize','3072','-validity','3650','-dname','CN=Android Rescue Local Development, OU=Offline Tools, O=Android Rescue, C=US','-noprompt')
}
$apk = Join-Path $PSScriptRoot 'AndroidRescueHelper.apk'
Invoke-Checked "$jdkRoot\bin\java.exe" @('-jar',"$sdkBuild\lib\apksigner.jar",'sign','--ks',$key,'--ks-key-alias','androidrescue','--ks-pass','pass:androidrescue-development','--key-pass','pass:androidrescue-development','--v1-signing-enabled','true','--v2-signing-enabled','true','--v3-signing-enabled','true','--out',$apk,"$build\aligned.apk")
Invoke-Checked "$jdkRoot\bin\java.exe" @('-jar',"$sdkBuild\lib\apksigner.jar",'verify','--verbose','--print-certs',$apk)
Invoke-Checked "$sdkBuild\aapt.exe" @('dump','badging',$apk)
Invoke-Checked "$sdkBuild\aapt.exe" @('dump','permissions',$apk)
Get-FileHash -LiteralPath $apk -Algorithm SHA256
