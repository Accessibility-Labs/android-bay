# Public build validation

Android Bay 1.2.0, prepared 2026-10-06.

- Python suite: **199 tests run; 196 passed, 3 skipped**. The skipped cases require creating Windows symbolic links, which this test environment does not permit.
- Tests cover the ADB/sync transport with a fake phone, transfer/resume behavior, unsafe paths, archive readers, local HTTP request protections, provider hashes, retained-trash/location checks, launcher locking and bounded device-context captures.
- The actual bundled ExifTool was exercised on generated media fixtures, including normal batches and malformed-metadata warnings.
- Phone-helper host tests: **97 assertions passed** against synthetic provider records.
- Android Bay Helper 1.0.1: rebuilt from the included source; APK v1/v2/v3 signatures verified; min API 19, target API 28; no Internet permission.
- Vendor archives were SHA-256 checked against the pinned acquisition records before their contents were assembled into this release. Bundled files are enumerated in `PUBLIC_FILES.json`.
- The portable ZIP was extracted into a clean folder and its actual `Launch.cmd` entrypoint started the demo service. The loopback health endpoint reported version 1.2.0 and demo mode; the rebranded interface was inspected in a browser. No real USB backend was started during this check.
- The complete packaged file list and file hashes were audited. No included file matched nonempty acquired-phone file hashes from the local recovery manifests. Runtime state, real-device reports and private archives are outside the release allowlist. The README screenshot was captured from the synthetic demo.

The rebranded helper has not been installed on a physical phone as part of this public-release preparation. Automated fixtures and signature checks do not establish complete recovery or compatibility on every device. No private acquisition is included as a test fixture or public validation artifact.

The application cannot guarantee an absence of bugs, a complete phone backup, or recovery of overwritten/encrypted/inaccessible data. Review the coverage report for each actual transfer.
