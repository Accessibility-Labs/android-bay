# Development and packaging

## Desktop

Use Windows 10/11 x64 and Python 3.13. The desktop controller has no pip dependencies. The normal release includes an isolated embedded Python runtime, official ADB, scrcpy and ExifTool.

For a source checkout, download the matching portable release and copy its `runtime/` and `tools/` directories into the checkout. Keep those directories out of Git. Alternatively, obtain the official distributions listed in `DEPENDENCIES.json`, verify their checksums and place them at the same paths. A changed upstream “latest” download will not match a pinned checksum; do not substitute it silently.

Run from the project root:

```powershell
.\runtime\python.exe -m unittest discover -s tests -v
.\runtime\python.exe server.py --demo --no-browser
```

The local URL is written into the demo's local state. Without `--demo`, the app uses the real USB backend. Automated tests use synthetic providers and a fake ADB peer; do not point tests at a personal archive.

## Phone helper

See [companion/README.md](companion/README.md) for its scope. Compile with the official Android SDK and a JDK using `companion/build.ps1 -ToolRoot <your-tool-directory>`. The scripts expect this layout under that directory:

| Path | Tool |
| --- | --- |
| `jdk-17.0.20.1+1/` | [Temurin 17.0.20.1+1 Windows x64 JDK](https://github.com/adoptium/temurin17-binaries/releases/tag/jdk-17.0.20.1%2B1) |
| `android-9/android.jar` | [Android API 28 platform, revision 6](https://dl.google.com/android/repository/platform-28_r06.zip) |
| `android-15/` | [Android SDK build tools 35 for Windows](https://dl.google.com/android/repository/build-tools_r35_windows.zip) |
| `json-20240303.jar` | [JSON-java 20240303](https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar), host tests only |

```powershell
.\companion\build.ps1 -ToolRoot 'C:\AndroidBuildTools'
.\companion\test.ps1 -ToolRoot 'C:\AndroidBuildTools'
```

The helper targets API 28 with a minimum of API 19 for legacy phones. Modern Android/OEM policies may restrict installation and provider access. It declares no Internet permission. The build script uses a local development signing key outside the source tree. Its documented development password is not a production secret; the private key must never be committed or shipped. Independently signed builds may not install over an existing helper without removing it first. Preserve needed exports before changing installed apps.

The public name is Android Bay. Existing internal package identifiers (`org.androidrescue.helper`), export paths and archive naming are retained for compatibility with the original local app. Do not rename those identifiers casually.

## Public release boundary

`PUBLIC_FILES.json` is an explicit file-and-SHA-256 manifest for the reviewed release. It lists original source, documentation, synthetic tests, the built APK and verified vendor files. It does not list runtime state, actual acquisitions, generated test outputs or private keys.

```powershell
.\runtime\python.exe make_release.py
```

This verifies every listed file and writes `dist/AndroidBay-Windows-x64.zip` and a SHA-256 text file. Unknown local files are not added. Changed/missing files stop packaging; review changes and deliberately update the manifest for a new release. The manifest itself is included in the ZIP.

Keep vendor notices and licenses. Audit the ZIP's complete entry list and contents, not only `.gitignore`. Use a new clean checkout for publishing; never initialize Git inside a folder containing personal recovery data. Do not upload old QA reports or screenshots from a real recovery session.
