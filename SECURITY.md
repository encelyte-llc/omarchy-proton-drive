# Security

- The plugin never handles your Proton password. Sign-in happens in Proton's
  own browser flow, and the session is stored by Proton's CLI in your desktop
  keyring.
- The CLI is downloaded only from `proton.me` over HTTPS and installed only if
  its SHA-512 matches the value on Proton's release page.
- The helper (`pdrive.py`) runs as your user, only calls `proton-drive`, `gio`
  and `notify-send`, and writes only to your sync folder,
  `~/.local/bin/proton-drive` and `~/.local/state/omarchy-proton-drive/`.
- Like every Omarchy plugin, the widget runs unsandboxed inside
  `omarchy-shell`. Review the code before you enable it.

Please report security issues privately via GitHub's "Report a vulnerability"
on this repository rather than in a public issue.
