# Android Bay Helper

This native Android helper exports local system-provider records for the desktop app. No Google account, Play Store or internet connection is needed. Android 4.4/API 19 is the minimum; target API 28 preserves legacy-storage behavior where Android permits it.

## Use it

1. In the desktop app, select your authorized phone and click **Install phone helper → Open helper on phone**.
2. Tap **Grant permissions** on the phone and approve only the categories you want. Use **Open app permission settings** if previously denied permissions need review.
3. Tap **Create export**. Keep the helper open, the screen awake and the phone connected until **Export finished** appears.
4. Return to the desktop, select **Phone data exports** and start the transfer. Check its coverage report.

Each run writes a new folder under `/sdcard/AndroidRescue/exports/`. This internal path is retained for compatibility. The helper reads original records and creates export files; it does not delete originals or previous exports. Export files contain personal information and are not encrypted.

## Exported records

- Contacts, raw contacts, data fields, groups, vCards and available contact photos.
- SMS, MMS, per-message addresses, inline MMS text and exact binary attachment bytes.
- Message threads and canonical addresses where exposed.
- Call history and visible calendars, events, reminders and attendees.
- Owner-profile and SIM-contact records where the device permits them.

JSONL output preserves returned fields, SQL nulls, numeric values and relationship IDs. Binary attachment references carry byte counts and SHA-256 hashes. A final `report.json` records attempted categories, permissions, errors and hashes. An interrupted run without the final report is not a completed export. `complete` means planned attempts had no recorded errors; it does not prove all data on the phone was accessible.

The helper cannot read arbitrary private app databases, passwords, account tokens, RCS chats, cloud-only records or other locked profiles. Device/OEM policies may deny providers even after permission is granted. It does not ask to become the default SMS app.

## Build and verification

See [DEVELOPMENT.md](../DEVELOPMENT.md) for the exact tool layout and commands. Source lives in `src/`; deterministic host fixtures live in `tests/`. Android's native `org.json` is used on the phone; JSON-java is used only by PC tests.

The APK is signed, but is not Play Store distributed. Public APK signatures, permission structure and host tests are checked as described in [VALIDATION.md](../VALIDATION.md). Those checks do not guarantee compatibility or completeness on every phone. Always review the device's actual export report.
