# Security and private data

Use Android Bay only with devices you own or are authorized to access. Unlock the phone and approve debugging on the phone itself. No lock bypass, exploit, rooting or bootloader unlock is included.

The desktop service binds to loopback, validates local requests and uses an action token. The helper has no Internet permission. These measures do not protect archives from other people or programs with access to your Windows account or destination drive. The archive is not encrypted by this app.

## Reporting a problem

Never include phone archives, messages, contacts, photos, location history, device serials, account details, authentication tokens, signing keys or unredacted logs in a public issue. Reproduce problems with synthetic records and include only a sanitized error plus software versions.

For vulnerabilities, use GitHub's private vulnerability reporting if it is available under this repository's Security tab. Otherwise open a minimal issue asking for a private reporting channel; do not publish sensitive details or attach data while arranging one.

## Release boundary

Public builds are assembled from an explicit application-file manifest and verified vendor packages. `.gitignore` is an additional guard, not proof that an arbitrary folder is safe to publish. Never run `git add .` in a working recovery installation. Prepare a separate clean source checkout, inspect its complete staged file list, and audit the final ZIP before uploading it.

Tests and security checks reduce risk but do not guarantee the absence of vulnerabilities or complete device coverage.
