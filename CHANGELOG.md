# Changelog

## Unreleased

- Ignore setting: leave folders out of the sync by name (`node_modules`) or by
  path (`site/assets`), with wildcards. Defaults to common build and cache folders.
  Newly ignored folders are never treated as deleted.
- Error messages from the CLI now show the actual message instead of its
  `=====` banner rule; full CLI output goes to `cli-errors.log` in the state folder.
- Recent activity is written as the sync runs and refreshes live in the panel.
- Clear button for recent activity.
- While syncing, the sync row spins and shows what it is doing: scanning Drive
  (folders and files found so far), then uploading or downloading with a
  percentage and a thin progress bar under the sync row.
- Uploads and downloads appear in recent activity as each folder's batch lands.
- Read-only CLI calls (list, info, size) retry on transient failures: a locked
  CLI cache database, truncated responses and network timeouts. One glitch no
  longer aborts a long sync.

## 0.1.0 (2026-10-30)

First release.

- Bar widget with sign-in, storage used, account, sync switch and recent activity.
- Two-way sync between a local folder and a folder in Proton Drive, built on
  Proton's official Drive CLI (tested with 0.9.0).
- One-click install of the official CLI, verified against Proton's published SHA-512.
- Deletions go to the Proton Drive trash or the desktop Trash; conflicts keep both versions.
- Safety stops for unreadable remotes, missing or emptied folders, and large deletions.
