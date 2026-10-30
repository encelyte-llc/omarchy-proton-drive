# Installing and setting up Proton Drive for Omarchy

This guide walks through everything from a fresh Omarchy install to a folder
that syncs with Proton Drive, plus how to change, troubleshoot and remove it.

## Requirements

- Omarchy 4 ("Quattro") or newer, which has the Quickshell bar and the
  `omarchy plugin` commands. Check with `omarchy version`.
- A Proton account (any plan, including Free).
- `python3`, `gio` and `notify-send`, all present on a standard Omarchy install.
- A desktop keyring (gnome-keyring on Omarchy), where the Proton CLI keeps
  your session. Check it is running: `busctl --user list | grep secrets`.

## 1. Add the plugin

```bash
omarchy plugin add https://github.com/encelyte-llc/omarchy-proton-drive --enable
```

Omarchy shows a warning that plugins run unsandboxed code and asks you to
confirm. The plugin lands in
`~/.config/omarchy/plugins/io.github.encelyte-llc.proton-drive/` and a cloud
icon appears on the right of the bar.

If the icon does not appear, run `omarchy-shell shell rescanPlugins`, or
enable it from the menu: **Omarchy menu → Plugins**.

## 2. Install Proton's CLI

Click the cloud icon and choose **Install Proton Drive CLI**. The plugin:

1. reads Proton's release page, `https://proton.me/download/drive/cli/index.html`;
2. downloads the official build for your CPU (x86-64 or ARM64);
3. checks it against the SHA-512 listed on that page and refuses it if
   they differ;
4. installs it to `~/.local/bin/proton-drive`.

Prefer to do it by hand? Download the Linux build from the same page, check
`sha512sum proton-drive` against the listed value, then
`install -m 755 proton-drive ~/.local/bin/`. The plugin uses any
`proton-drive` on your `PATH`.

## 3. Sign in

Choose **Sign in to Proton Drive**. Your browser opens Proton's sign-in page.
If it does not, use **Open sign-in page** in the panel, or open the link on
another device. Finish signing in there, including 2FA. The panel switches to
your account once Proton confirms.

The plugin never sees your password. The CLI stores the session in your
keyring.

## 4. Choose what to sync

The defaults are:

- **Local folder:** `~/ProtonDrive`
- **Proton Drive folder:** `My files/Omarchy` (`/my-files/Omarchy`)

Both are created if they do not exist. To change them, open the Omarchy bar
settings (**Omarchy menu → Bar**, or right-click the bar), pick the
**Proton Drive** widget and set **Local folder** and **Proton Drive folder**.

The Drive folder must be inside `/my-files`. For example, use
`/my-files/Documents` to sync your existing Documents folder. Syncing the
whole Drive at once is not supported, because a large Drive takes a long
time to list through the CLI.

Changing either folder starts a fresh sync record. The first sync after a
change only merges: files missing on one side are copied over, files that
differ are kept in both versions, and nothing is deleted.

## 5. Turn sync on

Flip the switch at the top of the panel. The first sync starts within about
15 seconds, then repeats every **Sync every (minutes)**, 10 by default. You can
always sync immediately:

- **Sync now** in the panel, or `s` with the panel open;
- right-click the bar icon;
- from a terminal: `omarchy-shell io.github.encelyte-llc.proton-drive sync`.

The bar icon shows the state at a glance:

| Icon | Meaning |
|---|---|
| cloud with check | synced, switch on |
| cloud with arrows | syncing now |
| plain cloud, dimmed | signed in, switch off |
| cloud crossed out | CLI missing or signed out |
| cloud with "!" (highlighted) | the last sync failed (see the panel) |

## Conflicts and deletions

- **Edited on both sides:** you keep both versions. Yours keeps its name,
  and Drive's version is saved beside it as `name (Proton conflict DATE).ext`
  on both sides. You get a desktop notification.
- **Deleted on one side:** the file goes to the Trash on the other side.
  That is the Proton Drive trash (restore it at drive.proton.me) or your
  desktop Trash (restore it from Files). Nothing is deleted permanently.
- **Big deletions:** if a sync would trash more than 10 files (or more than
  a quarter of the folder), or one side was emptied, it stops and changes
  nothing. If that was intended, choose **Sync and allow deletions**.

## Troubleshooting

**"Sync failed" with a network or sign-in message.** Open the panel. If it
says *Signed out*, sign in again. Proton sessions can expire. Nothing is
changed while the remote folder cannot be read.

**"The local folder ... is missing".** The sync folder was moved, renamed or
is on a disk that is not mounted. Put it back, or point **Local folder**
at the new place (that starts a fresh merge).

**The sign-in page never finishes.** Choose **Cancel sign-in** and try again.
Make sure the keyring is running and unlocked (it unlocks when you log in).

**A sync takes a long time.** Each CLI call talks to Proton's servers, and
one folder takes a few seconds to list. Folders with hundreds of subfolders
take minutes. Pick a smaller folder or a longer interval.

**See exactly what a sync would do, without doing it:**

```bash
python3 ~/.config/omarchy/plugins/io.github.encelyte-llc.proton-drive/pdrive.py \
  sync --local ~/ProtonDrive --remote /my-files/Omarchy --dry-run
```

**Logs and state** are in `~/.local/state/omarchy-proton-drive/`:

| File | Contents |
|---|---|
| `last-sync.json` | result of the most recent sync |
| `activity.jsonl` | the last 200 sync events shown in the panel |
| `state.json` | what both sides looked like after the last sync |
| `config.json` | the sync switch and cached account name |

Deleting `state.json` makes the next sync a fresh merge, which never deletes
anything.

**Shell errors** from the widget show in the user journal:
`journalctl --user -b | grep -i proton-drive`.

## Updating

```bash
omarchy plugin update io.github.encelyte-llc.proton-drive
```

Omarchy shows the diff before applying it. To update Proton's CLI, delete
`~/.local/bin/proton-drive` and choose **Install Proton Drive CLI** again.

## Removing

```bash
omarchy plugin remove io.github.encelyte-llc.proton-drive
```

Your files stay where they are, locally and in Drive. To also sign out and
remove the CLI and the plugin's state:

```bash
proton-drive auth logout
rm ~/.local/bin/proton-drive
rm -r ~/.local/state/omarchy-proton-drive
```
