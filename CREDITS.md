# Credits and acknowledgments

Android Bay combines an independently written desktop controller, local archive reader and Android helper with mature tools built by others. We are grateful to the people who made that possible.

## Tools included in the Windows release

| Project | Credit | How Android Bay uses it | License / source |
| --- | --- | --- | --- |
| [Android Debug Bridge / Platform Tools](https://developer.android.com/tools/releases/platform-tools) | Android Open Source Project, Google and contributors | Authorized USB communication and accessible data transfer | [ADB source](https://android.googlesource.com/platform/packages/modules/adb/); bundled `tools/platform-tools/NOTICE.txt` |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Genymobile, Romain Vimont and contributors | The optional Mirror phone window | Apache-2.0 for scrcpy; [release and dependency build information](https://github.com/Genymobile/scrcpy/tree/v5.0/release); bundled components retain their own terms |
| [ExifTool](https://github.com/exiftool/exiftool) | Phil Harvey and contributors; Windows packaging contributors | Read-only metadata extraction from saved media | [Official site](https://exiftool.org/); same terms as Perl (Artistic License or GPL), plus bundled dependency notices |
| [CPython](https://github.com/python/cpython) | Python Software Foundation and contributors | Portable Python runtime and standard library | [Python license](https://docs.python.org/3/license.html); bundled `runtime/LICENSE.txt` |

The release keeps vendor copyright notices and license files. ExifTool's executable is renamed from `exiftool(-k).exe` to `exiftool.exe` for noninteractive use. Python's isolated import path is configured to load the adjacent application. Versions and archive checksums are recorded in [DEPENDENCIES.json](DEPENDENCIES.json).

## Projects that informed the design

These projects were researched during development. Their source was not incorporated into the custom controller or helper, and Android Bay is not a fork of them.

- **[Open Android Backup — mrrfv and contributors](https://github.com/mrrfv/open-android-backup):** the practical ADB-plus-companion approach to no-account Android backups. GPL-3.0.
- **[Android-Archiver — mirbyte and contributors](https://github.com/mirbyte/Android-Archiver):** Windows backup handling, preserving incomplete copies and shell-stream fallback for files ordinary ADB sync cannot copy. MIT.
- **[SMS Import / Export — tmo1 and contributors](https://github.com/tmo1/sms-ie):** SMS/MMS export, line-delimited records and separately preserved binary attachments. GPL-3.0.
- **[Amaze File Manager — TeamAmaze and contributors](https://github.com/TeamAmaze/AmazeFileManager):** Android file-management, APK-backup and root-browsing capabilities that helped define the project's scope. GPL-3.0.
- **[Timeline GPX Exporter — Makeshit and contributors](https://github.com/Makeshit/Timeline-GPX-Exporter):** examples of Timeline export structures.
- **[Google Takeout location parser — DovarFalcone and contributors](https://github.com/DovarFalcone/google-takeout-location-parser):** examples of location coordinates and export formats.

## Build and test tools

- [Eclipse Temurin / Adoptium](https://github.com/adoptium/temurin17-binaries): Java development kit for compiling and signing the helper.
- [Android SDK](https://developer.android.com/tools): platform API, D8, AAPT, zipalign and APK signature verification.
- [JSON-java](https://github.com/stleary/JSON-java): host-side exporter tests only. The APK uses Android's native `org.json`; it does not bundle this test library.

## Documentation and platform research

Implementation decisions were checked against official [ADB documentation](https://developer.android.com/tools/adb), [Android's app sandbox documentation](https://source.android.com/docs/security/app-sandbox), [MediaStore](https://developer.android.com/reference/android/provider/MediaStore), [Telephony](https://developer.android.com/reference/android/provider/Telephony), [ContactsContract](https://developer.android.com/reference/android/provider/ContactsContract) and [CalendarContract](https://developer.android.com/reference/android/provider/CalendarContract), along with relevant AOSP source.

Credit does not imply endorsement or affiliation. All upstream names and marks belong to their respective owners.
