# Changelog

## 0.1.0 (2026-10-30)

First release.

- Bar widget with sign-in, storage used, account, sync switch and recent activity.
- Two-way sync between a local folder and a folder in Proton Drive, built on
  Proton's official Drive CLI (tested with 0.9.0).
- One-click install of the official CLI, verified against Proton's published SHA-512.
- Deletions go to the Proton Drive trash or the desktop Trash; conflicts keep both versions.
- Safety stops for unreadable remotes, missing or emptied folders, and large deletions.
