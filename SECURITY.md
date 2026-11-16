# Security Policy

The readers decrypt local chat data and, for Teams attachments, hold a Microsoft Graph
credential. Security reports are taken seriously.

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/sean3808/local-message-readers/security/advisories/new).
Do not open a public issue, and do not include real chat content, account IDs or keys in the report.

## Scope

In scope: decrypted plaintext left on disk, credentials stored or logged insecurely, any path
that sends data off the machine or writes back to WeChat or Teams, and the commit blocklist
letting private strings through.

Only the latest commit on `main` is supported.
