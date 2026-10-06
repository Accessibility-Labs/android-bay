# Third-party licenses and source

Android Bay's MIT license covers its original application code. The portable Windows release also distributes the components below under their own terms. Android Bay invokes the tools as separate programs; it does not incorporate their implementations into its Python controller or Java helper.

| Component | License / notices included in the portable release |
| --- | --- |
| Android SDK Platform Tools / ADB | AOSP and component notices in `tools/platform-tools/NOTICE.txt`; these also accompany the separate ADB copy in the scrcpy distribution. |
| scrcpy 5.0 | Apache-2.0, `tools/scrcpy-win64-v5.0/LICENSE.txt`. [Upstream source and build instructions](https://github.com/Genymobile/scrcpy/tree/v5.0). |
| FFmpeg 9.0.2 | LGPL-2.1-or-later for the shipped DLL build; [license text](FFmpeg-LGPL-2.1.txt), [upstream licensing details](FFmpeg-LICENSE.md), [recorded build configuration](FFmpeg-BUILD.txt). |
| libusb 1.0.30 | LGPL-2.1-or-later, [license text](libusb-LGPL-2.1.txt). [Upstream](https://github.com/libusb/libusb/tree/v1.0.30). |
| SDL 3.4.18 | zlib license, [license text](SDL-LICENSE.txt). [Upstream](https://github.com/libsdl-org/SDL/tree/release-3.4.18). |
| dav1d 1.5.4 | BSD-2-Clause, [copyright and license text](dav1d-COPYING.txt). [Upstream](https://code.videolan.org/videolan/dav1d/-/tree/1.5.4). Included in FFmpeg by the upstream build. |
| zlib | zlib license, [copyright and license text](zlib-LICENSE.txt). Used by the upstream FFmpeg build. |
| ExifTool 13.59 | Phil Harvey; distributed under the same terms as Perl: [Artistic License](Perl-Artistic.txt) or GPL. Its Perl source is included under `tools/exiftool/exiftool_files/`; the vendor's GPL text is preserved in that folder's `LICENSE`. |
| ExifTool Windows launcher | Oliver Betz; CC0, as recorded in `tools/exiftool/exiftool_files/readme_windows.txt`. |
| Strawberry Perl and its libraries | Original component notices in `tools/exiftool/exiftool_files/Licenses_Strawberry_Perl.zip`, including Perl's licenses and GCC runtime notices. |
| CPython 3.13.16 | Python Software Foundation license and incorporated notices in `runtime/LICENSE.txt`. |

## Corresponding source

The portable ZIP includes these unmodified source archives in `third-party/sources/`:

- `ffmpeg-9.0.2.tar.xz`: FFmpeg library source, including its component notices.
- `libusb-1.0.30.tar.gz`: libusb library source.
- `scrcpy-5.0.tar.gz`: scrcpy source, release build scripts and dependency build recipes.
- `dav1d-1.5.4.tar.xz`: the AV1 decoder source used by the FFmpeg build.

FFmpeg and libusb source archives are verified against the hashes in scrcpy's [pinned build recipes](https://github.com/Genymobile/scrcpy/tree/v5.0/app/deps). [SOURCES.json](SOURCES.json) records the official download locations and SHA-256 values. These compressed source archives are carried in the portable ZIP, rather than checked into the application repository. ExifTool's Perl source already accompanies its executable.

The shipped FFmpeg DLL reports LGPL version 2.1 or later, dynamic linking, and neither `--enable-gpl` nor `--enable-nonfree`. See [FFmpeg-BUILD.txt](FFmpeg-BUILD.txt) for the configuration and build recipe. Android Bay does not modify the vendor DLLs. Users may replace them with interface-compatible versions, including modified builds, and debug those modifications.

## Redistribution

Keep the application copyright and MIT license notice. When redistributing the portable bundle, also retain the vendor notices, these license files and the corresponding source archives. If you change or replace a vendor component, review that component's terms and update its source materials and notices to match. The application license does not relicense third-party code.

The Python import-path configuration is adjusted for portable startup. ExifTool's launcher is renamed from `exiftool(-k).exe` to `exiftool.exe` for batch use. Other vendor files are preserved byte-for-byte from the recorded distributions.
