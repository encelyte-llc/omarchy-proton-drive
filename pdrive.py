#!/usr/bin/env python3
"""Helper for the Proton Drive Omarchy plugin.

Wraps Proton's official `proton-drive` CLI and adds a two-way folder sync on
top of it. Every subcommand prints one JSON object on stdout, so the QML side
only ever parses JSON.

  pdrive.py status                      CLI, sign-in, storage, sync state
  pdrive.py install-cli                 download the official CLI, verify its SHA-512
  pdrive.py login | logout              pass-through to `proton-drive auth`
  pdrive.py set-sync on|off             persist the sync switch
  pdrive.py sync --local DIR --remote PATH [--dry-run] [--allow-mass-delete]
  pdrive.py activity [--limit N]        recent sync activity
  pdrive.py clear-activity              empty the recent activity list

Safety rules of the sync, in order of importance:
  * nothing is ever deleted permanently: deletions go to the local desktop
    Trash or to the Proton Drive trash;
  * a conflict keeps both versions, on both sides;
  * a sync refuses to run when either side looks missing or emptied, or when
    it would delete more than a handful of files (unless explicitly allowed);
  * any failure to read the remote tree aborts before anything changes.
"""

import argparse
import concurrent.futures
import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
CLI_INDEX_URL = "https://proton.me/download/drive/cli/index.html"
STAGING_DIR = ".pdsync-staging"
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".directory", STAGING_DIR}
IGNORED_SUFFIXES = ("~", ".part", ".crdownload", ".tmp", ".swp", ".swx")
IGNORED_PREFIXES = (".~lock.", ".goutputstream-", "~$")
# Folders that are rebuilt by tools and rarely worth syncing; overridable
# with --ignore (the "ignore" setting in the panel).
DEFAULT_IGNORE = "node_modules, __pycache__, .venv, .cache, .fetch-cache"
ACTIVITY_LIMIT = 200
LIST_WORKERS = 4
TRANSFER_WORKERS = 2
# Failures worth retrying: parallel CLI processes contending for the CLI's
# SQLite cache, truncated responses, and network hiccups.
TRANSIENT_ERRORS = re.compile(
  r"database is locked|SQLITE_BUSY|JSON Parse error|timed out|socket connection was closed"
  r"|ECONNRESET|ETIMEDOUT|EAI_AGAIN|fetch failed|Unable to connect|status code 5\d\d",
  re.IGNORECASE,
)
RETRY_DELAYS = (2, 6, 15)


class SyncError(Exception):
  """A sync that must stop before (or without) changing anything further."""


# ---------------------------------------------------------------------------
# Paths and small utilities


def state_dir():
  base = os.environ.get("PDRIVE_STATE_DIR")
  if base:
    path = Path(base)
  else:
    path = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "omarchy-proton-drive"
  path.mkdir(parents=True, exist_ok=True)
  return path


def cli_path():
  override = os.environ.get("PDRIVE_CLI")
  if override:
    return override
  found = shutil.which("proton-drive")
  if found:
    return found
  local = Path.home() / ".local" / "bin" / "proton-drive"
  return str(local) if local.exists() else None


def emit(payload, code=0):
  print(json.dumps(payload))
  sys.exit(code)


def now_iso():
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, fallback):
  try:
    with open(path, "r", encoding="utf-8") as handle:
      return json.load(handle)
  except (OSError, ValueError):
    return fallback


def write_json(path, data):
  path = Path(path)
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
  with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=1, sort_keys=True)
  os.replace(tmp, path)


def expand_local(path):
  return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "").strip())))


def normalize_remote(path):
  value = str(path or "").strip()
  if not value.startswith("/"):
    value = "/my-files/" + value
  value = re.sub(r"/+", "/", value).rstrip("/")
  return value or "/my-files"


def escape_name(name):
  # Remote paths are POSIX paths; a slash inside a node name is escaped.
  return name.replace("/", "\\/")


def remote_join(root, rel):
  if not rel:
    return root
  return root + "/" + "/".join(escape_name(part) for part in rel.split("/"))


def rel_parent(rel):
  return rel.rsplit("/", 1)[0] if "/" in rel else ""


def parse_ignore(text):
  """Comma or newline separated patterns. A bare name ("node_modules") matches
  at any depth; one with a slash ("site/assets") matches that path from the
  sync root. Shell wildcards work in both."""
  return [p.strip().strip("/") for p in re.split(r"[,\n]", text or "") if p.strip().strip("/")]


def ignored(name, rel="", patterns=()):
  return (
    name in IGNORED_NAMES
    or name.endswith(IGNORED_SUFFIXES)
    or name.startswith(IGNORED_PREFIXES)
    or any(fnmatch.fnmatchcase(rel if "/" in p else name, p) for p in patterns)
  )


def ignored_rel(rel, patterns):
  """True when the path or any folder above it is ignored."""
  parts = rel.split("/")
  return any(ignored(parts[i], "/".join(parts[: i + 1]), patterns) for i in range(len(parts)))


def sha1_of(path):
  digest = hashlib.sha1()
  with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def parse_time(value):
  if not value:
    return None
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
  except ValueError:
    return None


def conflict_name(name, stamp):
  stem, dot, ext = name.rpartition(".")
  if not dot or not stem:
    return f"{name} (Proton conflict {stamp})"
  return f"{stem} (Proton conflict {stamp}).{ext}"


# ---------------------------------------------------------------------------
# The official CLI


def cli_error_message(output):
  """The CLI frames errors in a banner of "=====" rules, an "Error details:"
  label and sometimes a source excerpt and stack trace. Prefer the
  "SomeError: message" line; otherwise the last line that says something."""
  lines = [
    line.strip() for line in output.splitlines()
    if line.strip().strip("=-*_^ ")
    and line.strip().rstrip(":").lower() != "error details"
    and not re.match(r"^\d+ \|", line.strip())
    and not line.strip().startswith("at ")
  ]
  named = [line for line in lines if re.match(r"^[A-Za-z]*Error: ", line)]
  return (named or lines or [""])[-1]


class Cli:
  def __init__(self, binary):
    if not binary:
      raise SyncError("The Proton Drive CLI is not installed")
    self.binary = binary

  def run(self, args, json_output=True, timeout=None, retries=0):
    """Runs one CLI command. `retries` is only for commands that are safe to
    repeat (reads); they are retried on transient failures."""
    for attempt in range(retries + 1):
      try:
        return self._run_once(args, json_output, timeout)
      except SyncError as error:
        if attempt >= retries or not getattr(error, "transient", False):
          raise
        time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

  def _run_once(self, args, json_output, timeout):
    command = [self.binary] + list(args) + (["-j"] if json_output else [])
    try:
      done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
      error = SyncError(f"proton-drive {' '.join(args[:2])} timed out")
      error.transient = True
      raise error
    except OSError as error:
      raise SyncError(f"Could not run proton-drive: {error}")
    if done.returncode != 0:
      # The CLI may put the banner on stderr and the message on stdout.
      output = "\n".join(part.strip() for part in (done.stdout, done.stderr) if part and part.strip())
      try:
        with open(state_dir() / "cli-errors.log", "a", encoding="utf-8") as handle:
          handle.write(f"[{now_iso()}] proton-drive {' '.join(args)} (exit {done.returncode})\n{output}\n\n")
      except OSError:
        pass
      error = SyncError(cli_error_message(output) or f"proton-drive {' '.join(args[:2])} failed")
      error.transient = bool(TRANSIENT_ERRORS.search(output))
      raise error
    if not json_output:
      return done.stdout
    text = done.stdout.strip()
    if not text:
      return None
    try:
      return json.loads(text)
    except ValueError:
      raise SyncError(f"Unexpected output from proton-drive {' '.join(args[:2])}")

  def list(self, path):
    result = self.run(["filesystem", "list", path], timeout=180, retries=3)
    return result if isinstance(result, list) else []

  def info(self, path):
    return self.run(["filesystem", "info", path], timeout=120, retries=3)

  def size(self, path):
    return self.run(["filesystem", "size", path], timeout=300, retries=2)

  def create_folder(self, parent, name):
    self.run(["filesystem", "create-folder", parent, name], json_output=False, timeout=180)

  def upload(self, local_paths, parent):
    self.run(
      ["filesystem", "upload", "-f", "create-new-revision", "-d", "merge"] + list(local_paths) + [parent],
      json_output=False,
    )

  def download(self, remote_paths, local_folder):
    self.run(
      ["filesystem", "download", "-f", "remove", "-d", "merge"] + list(remote_paths) + [local_folder],
      json_output=False,
    )

  def trash(self, remote_paths):
    self.run(["filesystem", "trash"] + list(remote_paths), json_output=False, timeout=300)


# ---------------------------------------------------------------------------
# Scanning


def scan_local(root, patterns=()):
  files = {}
  dirs = set()
  skipped = []
  for current, subdirs, names in os.walk(root):
    rel_dir = os.path.relpath(current, root)
    rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
    keep = []
    for name in subdirs:
      full = os.path.join(current, name)
      if ignored(name, (rel_dir + "/" + name).lstrip("/"), patterns) or os.path.islink(full):
        continue
      if "\\" in name:
        skipped.append((rel_dir + "/" + name).lstrip("/"))
        continue
      keep.append(name)
      dirs.add((rel_dir + "/" + name).lstrip("/"))
    subdirs[:] = keep
    for name in names:
      full = os.path.join(current, name)
      rel = (rel_dir + "/" + name).lstrip("/")
      if ignored(name, rel, patterns) or os.path.islink(full):
        continue
      if "\\" in name:
        skipped.append(rel)
        continue
      try:
        st = os.stat(full)
      except OSError:
        continue
      files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
  return files, dirs, skipped


def remote_entry(node):
  revision = node.get("activeRevision") or {}
  digests = revision.get("claimedDigests") or {}
  return {
    "rev": revision.get("uid") or node.get("uid"),
    "sha1": digests.get("sha1"),
    "size": revision.get("claimedSize", node.get("totalStorageSize")),
    "mtime": revision.get("claimedModificationTime") or node.get("modificationTime"),
  }


def scan_remote(cli, root, patterns=(), progress=None):
  """Recursively lists the remote tree. Any listing failure raises."""
  files = {}
  dirs = set()
  skipped = []

  def list_dir(rel):
    return rel, cli.list(remote_join(root, rel))

  pending = [""]
  with concurrent.futures.ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
    while pending:
      results = list(pool.map(list_dir, pending))
      pending = []
      for rel_dir, nodes in results:
        for node in nodes:
          name_field = node.get("name") or {}
          name = name_field.get("value") if name_field.get("ok") else None
          rel = (rel_dir + "/" + (name or "?")).lstrip("/")
          if not name or "/" in name or "\\" in name or name in (".", ".."):
            skipped.append(rel)
            continue
          if ignored(name, rel, patterns):
            continue
          kind = node.get("type")
          if kind == "folder":
            dirs.add(rel)
            pending.append(rel)
          elif kind == "file":
            media = str(node.get("mediaType") or "")
            if media.startswith("application/vnd.proton"):
              continue  # Proton Docs and Sheets cannot be downloaded as files
            files[rel] = remote_entry(node)
      if progress:
        progress(f"Scanning Drive · {len(dirs):,} folders, {len(files):,} files")
  return files, dirs, skipped


# ---------------------------------------------------------------------------
# Planning


def plan_sync(local, remote, state, local_dirs, remote_dirs, state_dirs, local_root):
  """Returns the list of actions; does not touch anything.

  Each action is a dict with `op` in: upload, download, conflict, record,
  trash-local, trash-remote, forget.
  """
  actions = []
  for rel in sorted(set(local) | set(remote) | set(state)):
    l, r, s = local.get(rel), remote.get(rel), state.get(rel)
    local_changed = l is not None and (s is None or l["size"] != s.get("size") or l["mtime_ns"] != s.get("mtime_ns"))
    remote_changed = r is not None and (s is None or r["rev"] != s.get("rev"))

    if l and r:
      if not local_changed and not remote_changed:
        continue
      if local_changed and not remote_changed:
        actions.append({"op": "upload", "rel": rel})
      elif remote_changed and not local_changed:
        actions.append({"op": "download", "rel": rel})
      else:
        local_sha = sha1_of(os.path.join(local_root, rel))
        if r.get("sha1") and local_sha == r["sha1"]:
          actions.append({"op": "record", "rel": rel})
        else:
          actions.append({"op": "conflict", "rel": rel})
    elif l:
      if s and not local_changed:
        actions.append({"op": "trash-local", "rel": rel})
      else:
        actions.append({"op": "upload", "rel": rel})
    elif r:
      if s and not remote_changed:
        actions.append({"op": "trash-remote", "rel": rel})
      else:
        actions.append({"op": "download", "rel": rel})
    else:
      actions.append({"op": "forget", "rel": rel})

  # A folder deleted on one side becomes one trash of the folder on the other,
  # but only when nothing inside it still needs to be kept.
  def collapse(missing_side_dirs, present_side_dirs, trash_op, keep_ops):
    folders = []
    for rel in sorted(state_dirs):
      if rel in missing_side_dirs or rel not in present_side_dirs:
        continue
      if any(rel == f or rel.startswith(f + "/") for f in folders):
        continue
      prefix = rel + "/"
      inside = [a for a in actions if a["rel"].startswith(prefix)]
      if any(a["op"] in keep_ops for a in inside):
        continue
      folders.append(rel)
    for rel in folders:
      prefix = rel + "/"
      count = sum(1 for a in actions if a["rel"].startswith(prefix) and a["op"] == trash_op)
      actions[:] = [a for a in actions if not (a["rel"].startswith(prefix) and a["op"] == trash_op)]
      actions.append({"op": trash_op, "rel": rel, "folder": True, "files": count})

  collapse(local_dirs, remote_dirs, "trash-remote", {"download", "conflict", "upload", "record"})
  collapse(remote_dirs, local_dirs, "trash-local", {"upload", "conflict", "download", "record"})
  return actions


def deletion_count(actions):
  total = 0
  for action in actions:
    if action["op"] in ("trash-local", "trash-remote"):
      total += action.get("files", 0) if action.get("folder") else 1
  return total


# ---------------------------------------------------------------------------
# Executing


def local_trash(path):
  override = os.environ.get("PDRIVE_LOCAL_TRASH")
  command = [override, path] if override else ["gio", "trash", path]
  done = subprocess.run(command, capture_output=True, text=True)
  if done.returncode != 0:
    raise SyncError((done.stderr or "Could not move to Trash").strip())


def ensure_remote_folder(cli, root):
  """Creates the remote root (and missing parents) if needed."""
  parts = root.strip("/").split("/")
  current = "/" + parts[0]
  for part in parts[1:]:
    children = cli.list(current)
    names = {((n.get("name") or {}).get("value")) for n in children if n.get("type") == "folder"}
    if part not in names:
      cli.create_folder(current, part)
    current = current + "/" + escape_name(part)


class Syncer:
  def __init__(self, cli, local_root, remote_root, log):
    self.cli = cli
    self.local_root = local_root
    self.remote_root = remote_root
    self.log = log
    self.stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    self.errors = []
    self.done = {"uploaded": 0, "downloaded": 0, "conflicts": 0, "trashedLocal": 0, "trashedRemote": 0}

  def local(self, rel):
    return os.path.join(self.local_root, *rel.split("/"))

  def remote(self, rel):
    return remote_join(self.remote_root, rel)

  def fail(self, rel, error):
    self.errors.append(f"{rel}: {error}")
    self.log("error", rel, str(error))

  def make_remote_dirs(self, rels):
    for rel in sorted(rels, key=lambda r: r.count("/")):
      try:
        self.cli.create_folder(self.remote(rel_parent(rel)), rel.rsplit("/", 1)[-1])
      except SyncError as error:
        self.fail(rel, error)

  def upload(self, rels, on_batch=None):
    """Uploads files grouped by remote parent folder. Returns rels that succeeded.
    `on_batch(items)` runs as each folder's batch lands."""
    groups = {}
    for rel in rels:
      groups.setdefault(rel_parent(rel), []).append(rel)
    succeeded = []

    def push(parent, items):
      self.cli.upload([self.local(r) for r in items], self.remote(parent))
      return items

    with concurrent.futures.ThreadPoolExecutor(max_workers=TRANSFER_WORKERS) as pool:
      futures = {pool.submit(push, parent, items): items for parent, items in groups.items()}
      for future in concurrent.futures.as_completed(futures):
        items = futures[future]
        try:
          future.result()
          succeeded.extend(items)
          if on_batch:
            on_batch(items)
        except SyncError as error:
          for rel in items:
            self.fail(rel, error)
        set_progress("Uploading", len(succeeded), len(rels))
    return succeeded

  def download(self, rels, remote, local_before, on_batch=None):
    """Downloads into a staging folder, then moves each file into place.

    A local file that changed while we were downloading is never overwritten:
    the download is kept beside it as a conflict copy instead.
    Returns {rel: final local rel} for files that landed; `on_batch` gets the
    same mapping for each folder's batch as it lands.
    """
    staging_root = os.path.join(self.local_root, STAGING_DIR)
    os.makedirs(staging_root, exist_ok=True)
    groups = {}
    for rel in rels:
      groups.setdefault(rel_parent(rel), []).append(rel)
    landed = {}

    def pull(parent, items):
      target = tempfile.mkdtemp(dir=staging_root)
      self.cli.download([self.remote(r) for r in items], target)
      return target, items

    with concurrent.futures.ThreadPoolExecutor(max_workers=TRANSFER_WORKERS) as pool:
      futures = {pool.submit(pull, parent, items): items for parent, items in groups.items()}
      for future in concurrent.futures.as_completed(futures):
        items = futures[future]
        try:
          target, _ = future.result()
        except SyncError as error:
          for rel in items:
            self.fail(rel, error)
          continue
        batch = {}
        for rel in items:
          name = rel.rsplit("/", 1)[-1]
          fetched = os.path.join(target, name)
          if not os.path.isfile(fetched):
            self.fail(rel, "download produced no file")
            continue
          destination_rel = rel
          before = local_before.get(rel)
          current = self.local(rel)
          if os.path.exists(current):
            st = os.stat(current)
            if before is None or st.st_size != before["size"] or st.st_mtime_ns != before["mtime_ns"]:
              destination_rel = (rel_parent(rel) + "/" + conflict_name(name, self.stamp)).lstrip("/")
          os.makedirs(os.path.dirname(self.local(destination_rel)), exist_ok=True)
          mtime = parse_time(remote[rel].get("mtime"))
          if mtime:
            os.utime(fetched, (mtime, mtime))
          os.replace(fetched, self.local(destination_rel))
          landed[rel] = destination_rel
          batch[rel] = destination_rel
        shutil.rmtree(target, ignore_errors=True)
        if on_batch and batch:
          on_batch(batch)
        set_progress("Downloading", len(landed), len(rels))
    shutil.rmtree(staging_root, ignore_errors=True)
    return landed


def run_sync(local_root, remote_root, dry_run=False, allow_mass_delete=False, ignore=DEFAULT_IGNORE):
  with open(state_dir() / "sync.lock", "w") as lock_handle:
    try:
      fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
      raise SyncError("A sync is already running")
    try:
      return _run_sync(local_root, remote_root, dry_run, allow_mass_delete, parse_ignore(ignore))
    finally:
      if not dry_run:
        set_progress("")


def _run_sync(local_root, remote_root, dry_run, allow_mass_delete, patterns):
  sdir = state_dir()
  activity_lock = threading.Lock()

  # Written as it happens (not at the end) so the panel can show progress
  # during a long sync. Transfers log from worker threads, hence the lock.
  def log(kind, rel, detail=""):
    with activity_lock:
      append_activity([{"time": now_iso(), "kind": kind, "path": rel, "detail": detail}])

  local_root = expand_local(local_root)
  remote_root = normalize_remote(remote_root)
  if not remote_root.startswith("/my-files/") and remote_root != "/my-files":
    raise SyncError("The remote folder must be inside /my-files")
  if remote_root == "/my-files":
    raise SyncError("Pick a folder inside My files; syncing the whole Drive is not supported")

  state_file = sdir / "state.json"
  saved = read_json(state_file, {})
  if (
    saved.get("version") != STATE_VERSION
    or saved.get("local") != local_root
    or saved.get("remote") != remote_root
  ):
    saved = {"version": STATE_VERSION, "local": local_root, "remote": remote_root, "files": {}, "dirs": []}
  # Ignored paths drop out of the state too, so newly ignoring a folder never
  # reads as it having been deleted on both sides.
  state = {rel: v for rel, v in (saved.get("files") or {}).items() if not ignored_rel(rel, patterns)}
  state_dirs = {rel for rel in saved.get("dirs") or [] if not ignored_rel(rel, patterns)}

  first_run = not state
  if not os.path.isdir(local_root):
    if first_run:
      os.makedirs(local_root, exist_ok=True)
    else:
      raise SyncError(f"The local folder {local_root} is missing; refusing to treat that as deleting everything")

  cli = Cli(cli_path())
  if first_run and not dry_run:
    ensure_remote_folder(cli, remote_root)

  local, local_dirs, local_skipped = scan_local(local_root, patterns)
  if not dry_run:
    set_progress("Scanning Drive")
  remote, remote_dirs, remote_skipped = scan_remote(cli, remote_root, patterns, None if dry_run else set_progress)
  for rel in local_skipped + remote_skipped:
    log("skipped", rel, "name cannot be represented on the other side")

  if state and not allow_mass_delete:
    if not local and not local_dirs:
      raise SyncError("The local folder was emptied; stopped to be safe instead of emptying the Drive folder too")
    if not remote and not remote_dirs:
      raise SyncError("The Drive folder was emptied; stopped to be safe instead of emptying the local folder too")

  actions = plan_sync(local, remote, state, local_dirs, remote_dirs, state_dirs, local_root)
  deletions = deletion_count(actions)
  limit = max(10, len(state) // 4)
  if deletions > limit and not allow_mass_delete:
    raise SyncError(
      f"This sync would move {deletions} files to the Trash; stopped to be safe. "
      "Run Sync with deletions allowed if that is what you want."
    )

  summary = {op: 0 for op in ("upload", "download", "conflict", "record", "trash-local", "trash-remote", "forget")}
  for action in actions:
    summary[action["op"]] += 1
  if dry_run:
    return {"dryRun": True, "plan": summary, "deletions": deletions, "actions": actions[:200]}

  syncer = Syncer(cli, local_root, remote_root, log)
  by_op = {}
  for action in actions:
    by_op.setdefault(action["op"], []).append(action)
  new_state = dict(state)

  # Folders first, so files have somewhere to land. A folder missing on one
  # side is created there unless this sync is trashing it (or a parent).
  trashed_local = [a["rel"] for a in by_op.get("trash-local", []) if a.get("folder")]
  trashed_remote = [a["rel"] for a in by_op.get("trash-remote", []) if a.get("folder")]

  def under(rel, folders):
    return any(rel == f or rel.startswith(f + "/") for f in folders)

  needs_remote = sorted(d for d in local_dirs - remote_dirs if not under(d, trashed_local))
  needs_local = sorted(d for d in remote_dirs - local_dirs if not under(d, trashed_remote))
  syncer.make_remote_dirs(needs_remote)
  for rel in needs_local:
    os.makedirs(syncer.local(rel), exist_ok=True)

  # Conflicts: keep the local file under its name, bring the remote version
  # down as a conflict copy, then push both so each side has both.
  conflict_rels = [a["rel"] for a in by_op.get("conflict", [])]
  conflict_copies = []
  if conflict_rels:
    staged = syncer.download(conflict_rels, remote, {})
    for rel in conflict_rels:
      if rel not in staged:
        continue
      name = rel.rsplit("/", 1)[-1]
      copy_rel = (rel_parent(rel) + "/" + conflict_name(name, syncer.stamp)).lstrip("/")
      landed = syncer.local(staged[rel])
      if staged[rel] != copy_rel:
        os.replace(landed, syncer.local(copy_rel))
      conflict_copies.append(copy_rel)
      syncer.done["conflicts"] += 1
      log("conflict", rel, f"kept both; Drive version saved as {copy_rel.rsplit('/', 1)[-1]}")

  upload_rels = [a["rel"] for a in by_op.get("upload", [])] + conflict_rels + conflict_copies
  def uploaded_batch(items):
    for rel in items:
      if rel in conflict_rels or rel in conflict_copies:
        continue
      syncer.done["uploaded"] += 1
      log("uploaded", rel)

  uploaded = syncer.upload(upload_rels, uploaded_batch) if upload_rels else []

  download_rels = [a["rel"] for a in by_op.get("download", [])]
  def downloaded_batch(batch):
    for rel, final_rel in batch.items():
      syncer.done["downloaded"] += 1
      if final_rel != rel:
        log("conflict", rel, f"changed locally during download; Drive version saved as {final_rel.rsplit('/', 1)[-1]}")
      else:
        log("downloaded", rel)
        st = os.stat(syncer.local(rel))
        new_state[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "rev": remote[rel]["rev"], "sha1": remote[rel].get("sha1")}

  if download_rels:
    syncer.download(download_rels, remote, local, downloaded_batch)

  for action in by_op.get("record", []):
    rel = action["rel"]
    new_state[rel] = {**local[rel], "rev": remote[rel]["rev"], "sha1": remote[rel].get("sha1")}

  remote_trash = by_op.get("trash-remote", [])
  if remote_trash:
    try:
      syncer.cli.trash([syncer.remote(a["rel"]) for a in remote_trash])
      for action in remote_trash:
        prefix = action["rel"] + "/"
        for rel in [r for r in new_state if r == action["rel"] or r.startswith(prefix)]:
          new_state.pop(rel, None)
        syncer.done["trashedRemote"] += action.get("files", 1) if action.get("folder") else 1
        log("trashed-remote", action["rel"], "moved to the Proton Drive trash")
    except SyncError as error:
      for action in remote_trash:
        syncer.fail(action["rel"], error)

  for action in by_op.get("trash-local", []):
    rel = action["rel"]
    try:
      local_trash(syncer.local(rel))
      prefix = rel + "/"
      for key in [r for r in new_state if r == rel or r.startswith(prefix)]:
        new_state.pop(key, None)
      syncer.done["trashedLocal"] += action.get("files", 1) if action.get("folder") else 1
      log("trashed-local", rel, "moved to the desktop Trash")
    except SyncError as error:
      syncer.fail(rel, error)

  for action in by_op.get("forget", []):
    new_state.pop(action["rel"], None)

  # Record the new remote revisions of everything we uploaded.
  if uploaded:
    parents = sorted({rel_parent(rel) for rel in uploaded})
    fresh = {}

    def relist(parent):
      return parent, syncer.cli.list(syncer.remote(parent))

    try:
      with concurrent.futures.ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
        for parent, nodes in pool.map(relist, parents):
          for node in nodes:
            name = (node.get("name") or {}).get("value")
            if name and node.get("type") == "file":
              fresh[(parent + "/" + name).lstrip("/")] = remote_entry(node)
    except SyncError as error:
      syncer.errors.append(f"Could not confirm uploads: {error}")
    for rel in uploaded:
      if rel in fresh and os.path.exists(syncer.local(rel)):
        st = os.stat(syncer.local(rel))
        new_state[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "rev": fresh[rel]["rev"], "sha1": fresh[rel].get("sha1")}

  final_local, final_local_dirs, _ = scan_local(local_root, patterns)
  failed_dirs = {e.split(": ", 1)[0] for e in syncer.errors}
  both_sides = {d for d in remote_dirs if not under(d, trashed_remote)} | (set(needs_remote) - failed_dirs)
  saved_dirs = sorted(final_local_dirs & both_sides)
  write_json(state_file, {
    "version": STATE_VERSION,
    "local": local_root,
    "remote": remote_root,
    "files": {rel: entry for rel, entry in new_state.items() if rel in final_local},
    "dirs": saved_dirs,
  })
  return {"dryRun": False, "done": syncer.done, "errors": syncer.errors, "files": len(final_local)}


def set_progress(text, done=None, total=None):
  """What the running sync is doing right now, for the panel's sync row.
  With `done` and `total` the text gets a percentage and the panel a bar."""
  path = state_dir() / "progress.json"
  if not text:
    if path.exists():
      path.unlink()
    return
  fraction = -1
  if total:
    fraction = min(1.0, done / total)
    text = f"{text} · {int(fraction * 100)}% · {done:,} of {total:,}"
  write_json(path, {"text": text, "fraction": fraction, "time": now_iso()})


def read_progress():
  data = read_json(state_dir() / "progress.json", {})
  fraction = data.get("fraction")
  return {
    "progress": str(data.get("text") or ""),
    "progressFraction": fraction if isinstance(fraction, (int, float)) else -1,
  }


def append_activity(entries):
  if not entries:
    return
  path = state_dir() / "activity.jsonl"
  lines = []
  if path.exists():
    lines = path.read_text(encoding="utf-8").splitlines()
  lines.extend(json.dumps(entry) for entry in entries)
  path.write_text("\n".join(lines[-ACTIVITY_LIMIT:]) + "\n", encoding="utf-8")


def read_activity(limit):
  path = state_dir() / "activity.jsonl"
  if not path.exists():
    return []
  rows = []
  for line in path.read_text(encoding="utf-8").splitlines()[-limit * 3:]:
    try:
      rows.append(json.loads(line))
    except ValueError:
      continue
  rows = [r for r in rows if r.get("kind") != "skipped"] + [r for r in rows if r.get("kind") == "skipped"][-3:]
  rows.sort(key=lambda r: r.get("time", ""), reverse=True)
  return rows[:limit]


# ---------------------------------------------------------------------------
# Status, install, auth


def read_config():
  return read_json(state_dir() / "config.json", {})


def command_status(args):
  binary = cli_path()
  config = read_config()
  last = read_json(state_dir() / "last-sync.json", {})
  sync_running = False
  try:
    with open(state_dir() / "sync.lock", "w") as handle:
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      fcntl.flock(handle, fcntl.LOCK_UN)
  except OSError:
    sync_running = True

  payload = {
    "ok": True,
    "installed": binary is not None,
    "cliPath": binary or "",
    "version": "",
    "authenticated": False,
    "account": config.get("account", ""),
    "usedBytes": 0,
    "fileCount": 0,
    "storageKnown": False,
    "syncEnabled": config.get("syncEnabled") is True,
    "syncRunning": sync_running,
    "lastSync": last,
    "activity": read_activity(args.limit),
    **(read_progress() if sync_running else {"progress": "", "progressFraction": -1}),
    "error": "",
  }
  if not binary:
    emit(payload)

  cli = Cli(binary)
  try:
    version = cli.run(["--version"], json_output=False, timeout=20)
    payload["version"] = (version or "").splitlines()[0].split("@")[-1].split("+")[0] if version else ""
  except SyncError:
    pass

  def fetch_size():
    return cli.size("/my-files")

  def fetch_info():
    return cli.info("/my-files")

  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    size_future = pool.submit(fetch_size)
    info_future = pool.submit(fetch_info) if not payload["account"] else None
    try:
      size = size_future.result() or {}
      payload["authenticated"] = True
      payload["usedBytes"] = int(size.get("size") or 0)
      payload["fileCount"] = int(size.get("numberOfDescendants") or 0)
      payload["storageKnown"] = True
    except SyncError as error:
      message = str(error)
      if not re.search(r"auth|log ?in|sign ?in|session|credential|keyring|unauthori", message, re.I):
        payload["error"] = message
    if info_future is not None:
      try:
        info = info_future.result() or {}
        email = (info.get("ownedBy") or {}).get("email") or ""
        if email:
          payload["account"] = email
          config["account"] = email
          write_json(state_dir() / "config.json", config)
      except SyncError:
        pass
  emit(payload)


def command_set_sync(args):
  config = read_config()
  config["syncEnabled"] = args.value == "on"
  write_json(state_dir() / "config.json", config)
  emit({"ok": True, "syncEnabled": config["syncEnabled"]})


def command_sync(args):
  started = now_iso()
  try:
    result = run_sync(args.local, args.remote, dry_run=args.dry_run, allow_mass_delete=args.allow_mass_delete, ignore=args.ignore)
  except SyncError as error:
    record = {"ok": False, "started": started, "finished": now_iso(), "error": str(error)}
    if str(error) != "A sync is already running":
      write_json(state_dir() / "last-sync.json", record)
      append_activity([{"time": now_iso(), "kind": "error", "path": "", "detail": str(error)}])
    emit(record, 1)
  record = {"ok": not result.get("errors"), "started": started, "finished": now_iso(), **result}
  if not args.dry_run:
    record["errors"] = record.get("errors", [])[:20]
    record["error"] = record["errors"][0] if record["errors"] else ""
    write_json(state_dir() / "last-sync.json", record)
  emit(record, 0 if record["ok"] else 1)


def cli_platform():
  machine = platform.machine().lower()
  arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
  if not arch:
    raise SyncError(f"No Proton Drive CLI build for {machine}")
  return f"linux/{arch}"


def command_install_cli(args):
  try:
    target_platform = cli_platform()
    with urllib.request.urlopen(CLI_INDEX_URL, timeout=30) as response:
      page = response.read().decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", " ", page)
    match = re.search(
      re.escape(target_platform) + r"\s+(https://proton\.me/download/drive/cli/[^\s]+/proton-drive)\s+([0-9a-f]{128})",
      text,
    )
    if not match:
      raise SyncError("Could not find the CLI download on Proton's release page")
    url, expected = match.group(1), match.group(2)
    version = url.split("/cli/")[1].split("/")[0]
    destination = Path.home() / ".local" / "bin" / "proton-drive"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix=".proton-drive.")
    digest = hashlib.sha512()
    with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as response:
      for chunk in iter(lambda: response.read(1024 * 1024), b""):
        digest.update(chunk)
        out.write(chunk)
    if digest.hexdigest() != expected:
      os.unlink(tmp)
      raise SyncError("Checksum mismatch: the download was discarded")
    os.chmod(tmp, 0o755)
    os.replace(tmp, destination)
  except (SyncError, OSError, ValueError) as error:
    emit({"ok": False, "error": str(error)}, 1)
  emit({"ok": True, "version": version, "path": str(destination)})


def command_auth(args):
  binary = cli_path()
  if not binary:
    emit({"ok": False, "error": "The Proton Drive CLI is not installed"}, 1)
  if args.command == "logout":
    done = subprocess.run([binary, "auth", "logout"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    config = read_config()
    config.pop("account", None)
    config["syncEnabled"] = False
    write_json(state_dir() / "config.json", config)
    emit({"ok": done.returncode == 0, "error": (done.stderr or "").strip()}, done.returncode)
  # Login streams the CLI's own output so the panel can show the sign-in URL.
  sys.stdout.flush()
  os.execv(binary, [binary, "auth", "login"])


def command_activity(args):
  emit({"ok": True, "activity": read_activity(args.limit), **read_progress()})


def command_clear_activity(args):
  (state_dir() / "activity.jsonl").write_text("", encoding="utf-8")
  emit({"ok": True})


def main():
  parser = argparse.ArgumentParser(prog="pdrive.py")
  sub = parser.add_subparsers(dest="command", required=True)
  status = sub.add_parser("status")
  status.add_argument("--limit", type=int, default=12)
  status.set_defaults(func=command_status)
  sub.add_parser("install-cli").set_defaults(func=command_install_cli)
  sub.add_parser("login").set_defaults(func=command_auth)
  sub.add_parser("logout").set_defaults(func=command_auth)
  toggle = sub.add_parser("set-sync")
  toggle.add_argument("value", choices=["on", "off"])
  toggle.set_defaults(func=command_set_sync)
  sync = sub.add_parser("sync")
  sync.add_argument("--local", required=True)
  sync.add_argument("--remote", required=True)
  sync.add_argument("--dry-run", action="store_true")
  sync.add_argument("--allow-mass-delete", action="store_true")
  sync.add_argument("--ignore", default=DEFAULT_IGNORE)
  sync.set_defaults(func=command_sync)
  activity = sub.add_parser("activity")
  activity.add_argument("--limit", type=int, default=12)
  activity.set_defaults(func=command_activity)
  clear = sub.add_parser("clear-activity")
  clear.set_defaults(func=command_clear_activity)
  args = parser.parse_args()
  args.func(args)


if __name__ == "__main__":
  main()
