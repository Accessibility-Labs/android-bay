# Contributing

Small, focused fixes and device-compatibility reports are welcome. Describe the behavior before and after a change and how it was verified.

- Use synthetic test records. Never contribute a real acquisition, contact, conversation, device identifier, location trail or private screenshot.
- Preserve originals and partial transfers. Do not silently turn a permission denial or parsing error into a successful empty result.
- Keep source paths as metadata; do not trust them as Windows filesystem paths.
- Keep the local-only workflow and document any change to permissions, network behavior or dependencies.
- Run the Python suite and relevant helper tests described in [DEVELOPMENT.md](DEVELOPMENT.md).
- Review every staged file before committing. Recovery folders, generated state, secrets and signing keys do not belong in Git.
