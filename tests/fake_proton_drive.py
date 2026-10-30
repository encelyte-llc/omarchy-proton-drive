#!/usr/bin/env python3
"""A stand-in for Proton's `proton-drive` CLI, backed by a local folder.

FAKE_DRIVE_ROOT holds the "remote": FAKE_DRIVE_ROOT/my-files/... plus a
meta.json that tracks a revision number per file and a trash folder. Only the
commands and JSON fields the plugin uses are implemented.
"""

import fcntl
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.environ["FAKE_DRIVE_ROOT"]
META = os.path.join(ROOT, "meta.json")


def load_meta():
  try:
    with open(META) as handle:
      return json.load(handle)
  except OSError:
    return {"next": 1, "revs": {}}


def save_meta(meta):
  with open(META, "w") as handle:
    json.dump(meta, handle)


def disk(path):
  parts = [p.replace("\\/", "/") for p in path.strip("/").split("/") if p]
  return os.path.join(ROOT, *parts)


def sha1(path):
  with open(path, "rb") as handle:
    return hashlib.sha1(handle.read()).hexdigest()


def iso(ts):
  return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fail(message):
  print(message, file=sys.stderr)
  sys.exit(1)


def node(path, meta):
  name = os.path.basename(path)
  st = os.stat(path)
  if os.path.isdir(path):
    return {"uid": path, "name": {"ok": True, "value": name}, "type": "folder", "modificationTime": iso(st.st_mtime)}
  key = os.path.relpath(path, ROOT)
  rev = meta["revs"].get(key, {}).get("rev", 0)
  return {
    "uid": path,
    "name": {"ok": True, "value": name},
    "type": "file",
    "mediaType": "application/octet-stream",
    "totalStorageSize": st.st_size,
    "modificationTime": iso(st.st_mtime),
    "activeRevision": {
      "uid": f"{key}~{rev}",
      "claimedSize": st.st_size,
      "claimedModificationTime": iso(st.st_mtime),
      "claimedDigests": {"sha1": sha1(path)},
    },
  }


def bump(meta, path):
  key = os.path.relpath(path, ROOT)
  meta["revs"][key] = {"rev": meta["next"]}
  meta["next"] += 1


def main():
  args = [a for a in sys.argv[1:] if a not in ("-j", "--json")]
  if args[:1] == ["--version"]:
    print("Proton Drive CLI cli-drive@0.0.0-fake+test")
    return
  if os.environ.get("FAKE_DRIVE_FAIL") and args[1:2] == [os.environ["FAKE_DRIVE_FAIL"]]:
    fail("simulated failure")
  group, command, rest = args[0], args[1], args[2:]
  assert group == "filesystem", args
  meta = load_meta()

  if command == "list":
    path = disk(rest[0])
    if not os.path.isdir(path):
      fail(f"Node not found: {rest[0]}")
    print(json.dumps([node(os.path.join(path, n), meta) for n in sorted(os.listdir(path)) if n not in (".trash", ".lock", "meta.json")]))
  elif command == "info":
    print(json.dumps({"type": "folder", "ownedBy": {"email": "test@example.com"}}))
  elif command == "size":
    print(json.dumps({"size": 0, "numberOfDescendants": 0}))
  elif command == "create-folder":
    parent, name = disk(rest[0]), rest[1]
    if not os.path.isdir(parent):
      fail("Parent not found")
    os.makedirs(os.path.join(parent, name), exist_ok=True)
  elif command == "upload":
    while rest and rest[0] in ("-f", "-d"):
      rest = rest[2:]
    *sources, parent = rest
    parent = disk(parent)
    if not os.path.isdir(parent):
      fail("Parent not found")
    for source in sources:
      target = os.path.join(parent, os.path.basename(source))
      if os.path.exists(target) and sha1(target) == sha1(source):
        continue
      shutil.copy2(source, target)
      bump(meta, target)
  elif command == "download":
    while rest and rest[0] in ("-f", "-d"):
      rest = rest[2:]
    *sources, folder = rest
    for source in sources:
      path = disk(source)
      if not os.path.isfile(path):
        fail(f"Node not found: {source}")
      shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
  elif command == "trash":
    trash = os.path.join(ROOT, ".trash")
    os.makedirs(trash, exist_ok=True)
    for source in rest:
      path = disk(source)
      if not os.path.exists(path):
        fail(f"Node not found: {source}")
      shutil.move(path, os.path.join(trash, f"{meta['next']}-{os.path.basename(path)}"))
      meta["next"] += 1
  else:
    fail(f"unsupported command {command}")
  save_meta(meta)


if __name__ == "__main__":
  # The plugin runs several CLI processes at once; serialize them here.
  with open(os.path.join(ROOT, ".lock"), "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    main()
