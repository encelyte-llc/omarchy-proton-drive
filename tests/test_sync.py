"""Sync engine tests against the fake CLI. Run: python3 -m unittest discover tests"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pdrive  # noqa: E402

REMOTE = "/my-files/Sync"


class SyncTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp(prefix="pdrive-test-")
    self.drive = os.path.join(self.tmp, "drive")
    self.local = os.path.join(self.tmp, "local")
    self.trash = os.path.join(self.tmp, "desktop-trash")
    os.makedirs(os.path.join(self.drive, "my-files"))
    os.makedirs(self.trash)
    trash_script = os.path.join(self.tmp, "trash.sh")
    with open(trash_script, "w") as handle:
      handle.write(f'#!/bin/sh\nmv "$1" "{self.trash}/$(date +%s%N)-$(basename "$1")"\n')
    os.chmod(trash_script, 0o755)
    self.env = {
      "FAKE_DRIVE_ROOT": self.drive,
      "PDRIVE_CLI": os.path.join(HERE, "fake_proton_drive.py"),
      "PDRIVE_STATE_DIR": os.path.join(self.tmp, "state"),
      "PDRIVE_LOCAL_TRASH": trash_script,
      "PDRIVE_CLI_DATA_DIR": os.path.join(self.tmp, "cli-data"),
    }
    self.saved_env = {k: os.environ.get(k) for k in self.env}
    pdrive.EVENT_SETTLE_SECONDS = 0
    pdrive.PROBE_RETRY_SECONDS = 0
    os.environ.update(self.env)

  def tearDown(self):
    for key, value in self.saved_env.items():
      if value is None:
        os.environ.pop(key, None)
      else:
        os.environ[key] = value
    os.environ.pop("FAKE_DRIVE_FAIL", None)
    shutil.rmtree(self.tmp, ignore_errors=True)

  # helpers -----------------------------------------------------------------

  def sync(self, **kwargs):
    return pdrive.run_sync(self.local, REMOTE, **kwargs)

  def remote_path(self, rel=""):
    return os.path.join(self.drive, "my-files", "Sync", *[p for p in rel.split("/") if p])

  def write_local(self, rel, text, bump=0):
    path = os.path.join(self.local, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
      handle.write(text)
    if bump:
      st = os.stat(path)
      os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump))

  def write_remote(self, rel, text):
    path = self.remote_path(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
      handle.write(text)
    meta_path = os.path.join(self.drive, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {"next": 1, "revs": {}}
    meta["revs"][os.path.relpath(path, self.drive)] = {"rev": meta["next"]}
    meta["next"] += 1
    json.dump(meta, open(meta_path, "w"))

  def read_local(self, rel):
    with open(os.path.join(self.local, *rel.split("/"))) as handle:
      return handle.read()

  def read_remote(self, rel):
    with open(self.remote_path(rel)) as handle:
      return handle.read()

  def local_files(self):
    found = set()
    for root, _, names in os.walk(self.local):
      for name in names:
        found.add(os.path.relpath(os.path.join(root, name), self.local))
    return found

  def remote_files(self):
    found = set()
    for root, _, names in os.walk(self.remote_path()):
      for name in names:
        found.add(os.path.relpath(os.path.join(root, name), self.remote_path()))
    return found

  # tests -------------------------------------------------------------------

  def test_first_sync_creates_remote_folder_and_merges_both_sides(self):
    self.write_local("a.txt", "local a")
    self.write_local("docs/b.txt", "local b")
    os.makedirs(os.path.join(self.drive, "my-files", "Sync"))
    self.write_remote("c.txt", "remote c")
    self.write_remote("photos/d.txt", "remote d")
    result = self.sync()
    self.assertEqual(result["errors"], [])
    expected = {"a.txt", "docs/b.txt", "c.txt", "photos/d.txt"}
    self.assertEqual(self.local_files(), expected)
    self.assertEqual(self.remote_files(), expected)

  def test_first_sync_with_missing_remote_root_creates_it(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.assertEqual(self.read_remote("a.txt"), "a")

  def test_second_sync_is_a_no_op(self):
    self.write_local("a.txt", "a")
    self.sync()
    result = self.sync()
    self.assertEqual(result["done"], {"uploaded": 0, "downloaded": 0, "conflicts": 0, "trashedLocal": 0, "trashedRemote": 0})

  def test_local_edit_uploads(self):
    self.write_local("a.txt", "v1")
    self.sync()
    self.write_local("a.txt", "version 2", bump=10_000_000)
    result = self.sync()
    self.assertEqual(result["done"]["uploaded"], 1)
    self.assertEqual(self.read_remote("a.txt"), "version 2")

  def test_remote_edit_downloads(self):
    self.write_local("a.txt", "v1")
    self.sync()
    self.write_remote("a.txt", "remote v2")
    result = self.sync()
    self.assertEqual(result["done"]["downloaded"], 1)
    self.assertEqual(self.read_local("a.txt"), "remote v2")
    self.assertEqual(self.sync()["done"]["downloaded"], 0)

  def test_both_edited_keeps_both_versions_on_both_sides(self):
    self.write_local("notes.md", "base")
    self.sync()
    self.write_local("notes.md", "local edit", bump=10_000_000)
    self.write_remote("notes.md", "remote edit")
    result = self.sync()
    self.assertEqual(result["done"]["conflicts"], 1)
    self.assertEqual(self.read_local("notes.md"), "local edit")
    self.assertEqual(self.read_remote("notes.md"), "local edit")
    copies = [f for f in self.local_files() if "Proton conflict" in f]
    self.assertEqual(len(copies), 1)
    self.assertTrue(copies[0].endswith(".md"))
    self.assertEqual(self.read_local(copies[0]), "remote edit")
    self.assertIn(copies[0], self.remote_files())
    again = self.sync()
    self.assertEqual(again["done"], {"uploaded": 0, "downloaded": 0, "conflicts": 0, "trashedLocal": 0, "trashedRemote": 0})

  def test_both_sides_same_content_is_not_a_conflict(self):
    self.write_local("a.txt", "same")
    os.makedirs(self.remote_path())
    self.write_remote("a.txt", "same")
    result = self.sync()
    self.assertEqual(result["done"]["conflicts"], 0)
    self.assertFalse(any("conflict" in f for f in self.local_files()))

  def test_local_delete_moves_remote_to_drive_trash(self):
    for i in range(12):
      self.write_local(f"f{i}.txt", str(i))
    self.sync()
    os.remove(os.path.join(self.local, "f0.txt"))
    result = self.sync()
    self.assertEqual(result["done"]["trashedRemote"], 1)
    self.assertNotIn("f0.txt", self.remote_files())
    self.assertTrue(any(n.endswith("f0.txt") for n in os.listdir(os.path.join(self.drive, ".trash"))))

  def test_remote_delete_moves_local_to_desktop_trash(self):
    for i in range(12):
      self.write_local(f"f{i}.txt", str(i))
    self.sync()
    os.remove(self.remote_path("f3.txt"))
    result = self.sync()
    self.assertEqual(result["done"]["trashedLocal"], 1)
    self.assertNotIn("f3.txt", self.local_files())
    self.assertTrue(any(n.endswith("f3.txt") for n in os.listdir(self.trash)))

  def test_remote_delete_of_locally_edited_file_reuploads(self):
    for i in range(12):
      self.write_local(f"f{i}.txt", str(i))
    self.sync()
    os.remove(self.remote_path("f1.txt"))
    self.write_local("f1.txt", "edited", bump=10_000_000)
    result = self.sync()
    self.assertEqual(result["done"]["trashedLocal"], 0)
    self.assertEqual(self.read_remote("f1.txt"), "edited")

  def test_deleted_local_folder_trashes_remote_folder_once(self):
    for i in range(12):
      self.write_local(f"keep/f{i}.txt", str(i))
    self.write_local("old/a.txt", "a")
    self.write_local("old/sub/b.txt", "b")
    self.sync()
    shutil.rmtree(os.path.join(self.local, "old"))
    result = self.sync()
    self.assertEqual(result["errors"], [])
    self.assertEqual(result["done"]["trashedRemote"], 2)
    self.assertFalse(os.path.exists(self.remote_path("old")))
    self.assertFalse(os.path.exists(os.path.join(self.local, "old")))
    self.assertEqual(self.sync()["done"]["downloaded"], 0)

  def test_new_file_in_remotely_deleted_folder_recreates_folder(self):
    for i in range(12):
      self.write_local(f"keep/f{i}.txt", str(i))
    self.write_local("proj/a.txt", "a")
    self.sync()
    shutil.rmtree(self.remote_path("proj"))
    self.write_local("proj/new.txt", "new")
    result = self.sync()
    self.assertEqual(result["errors"], [])
    self.assertEqual(self.read_remote("proj/new.txt"), "new")

  def test_mass_delete_is_refused_then_allowed(self):
    for i in range(40):
      self.write_local(f"f{i}.txt", str(i))
    self.sync()
    for i in range(30):
      os.remove(os.path.join(self.local, f"f{i}.txt"))
    with self.assertRaises(pdrive.SyncError):
      self.sync()
    self.assertEqual(len(self.remote_files()), 40)
    result = self.sync(allow_mass_delete=True)
    self.assertEqual(result["done"]["trashedRemote"], 30)

  def test_emptied_local_folder_is_refused(self):
    self.write_local("a.txt", "a")
    self.sync()
    os.remove(os.path.join(self.local, "a.txt"))
    with self.assertRaises(pdrive.SyncError):
      self.sync()
    self.assertEqual(self.read_remote("a.txt"), "a")
    self.assertEqual(self.sync(allow_mass_delete=True)["done"]["trashedRemote"], 1)
    self.assertEqual(self.remote_files(), set())

  def test_emptied_remote_folder_is_refused(self):
    self.write_local("a.txt", "a")
    self.sync()
    os.remove(self.remote_path("a.txt"))
    with self.assertRaises(pdrive.SyncError):
      self.sync()
    self.assertIn("a.txt", self.local_files())

  def test_remote_folder_delete_trashes_local_folder(self):
    for i in range(12):
      self.write_local(f"keep/f{i}.txt", str(i))
    self.write_local("gone/a.txt", "a")
    self.sync()
    shutil.rmtree(self.remote_path("gone"))
    result = self.sync()
    self.assertEqual(result["done"]["trashedLocal"], 1)
    self.assertFalse(os.path.exists(os.path.join(self.local, "gone")))
    self.assertTrue(any(n.endswith("gone") for n in os.listdir(self.trash)))

  def test_missing_local_folder_is_refused(self):
    self.write_local("a.txt", "a")
    self.sync()
    shutil.rmtree(self.local)
    with self.assertRaises(pdrive.SyncError):
      self.sync()
    self.assertEqual(self.read_remote("a.txt"), "a")

  def test_remote_listing_failure_changes_nothing(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.write_local("b.txt", "b")
    os.environ["FAKE_DRIVE_FAIL"] = "list"
    with self.assertRaises(pdrive.SyncError):
      self.sync()
    os.environ.pop("FAKE_DRIVE_FAIL")
    self.assertNotIn("b.txt", self.remote_files())
    self.assertIn("a.txt", self.local_files())

  def test_upload_failure_is_reported_and_retried(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.write_local("b.txt", "b")
    os.environ["FAKE_DRIVE_FAIL"] = "upload"
    result = self.sync()
    self.assertTrue(result["errors"])
    os.environ.pop("FAKE_DRIVE_FAIL")
    result = self.sync()
    self.assertEqual(result["done"]["uploaded"], 1)
    self.assertEqual(self.read_remote("b.txt"), "b")

  def test_ignored_files_are_not_synced(self):
    self.write_local("a.txt", "a")
    self.write_local(".~lock.a.odt#", "lock")
    self.write_local("download.part", "partial")
    self.sync()
    self.assertEqual(self.remote_files(), {"a.txt"})

  def test_ignore_patterns_skip_folders_on_both_sides(self):
    self.write_local("keep/a.txt", "a")
    self.write_local("site/.fetch-cache/x.bin", "x")
    self.write_local("site/assets/logo.png", "png")
    self.write_local("other/assets/b.txt", "b")
    os.makedirs(self.remote_path("junk/.fetch-cache"))
    with open(os.path.join(self.remote_path("junk/.fetch-cache"), "y.bin"), "w") as handle:
      handle.write("y")
    self.sync(ignore=".fetch-cache, site/assets")
    self.assertEqual(self.remote_files(), {"keep/a.txt", "other/assets/b.txt", "junk/.fetch-cache/y.bin"})
    self.assertFalse(os.path.exists(os.path.join(self.local, "junk", ".fetch-cache", "y.bin")))

  def test_newly_ignored_folder_is_not_trashed(self):
    self.write_local("a.txt", "a")
    self.write_local("cache/b.txt", "b")
    self.sync(ignore="")
    result = self.sync(ignore="cache")
    self.assertEqual(result["done"]["trashedLocal"] + result["done"]["trashedRemote"], 0)
    self.assertIn("cache/b.txt", self.remote_files())
    self.assertTrue(os.path.exists(os.path.join(self.local, "cache", "b.txt")))

  def test_activity_is_logged_and_can_be_cleared(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.assertEqual([r["path"] for r in pdrive.read_activity(12) if r["kind"] == "uploaded"], ["a.txt"])
    with self.assertRaises(SystemExit):
      pdrive.command_clear_activity(None)
    self.assertEqual(pdrive.read_activity(12), [])

  def test_transient_list_failures_are_retried(self):
    self.write_local("a.txt", "a")
    os.environ["FAKE_DRIVE_FLAKY"] = "list:2"
    saved, pdrive.RETRY_DELAYS = pdrive.RETRY_DELAYS, (0,)
    try:
      result = self.sync()
    finally:
      pdrive.RETRY_DELAYS = saved
      os.environ.pop("FAKE_DRIVE_FLAKY")
    self.assertEqual(result["errors"], [])
    self.assertEqual(self.remote_files(), {"a.txt"})

  def test_cli_error_message_skips_banner_and_stack(self):
    output = "7 | export class SQLiteCache {\n         ^\nSQLiteError: database is locked\n   at run (bun:sqlite:318:21)\n" \
      "Error details:\n{ code: 'SQLITE_BUSY_RECOVERY', errno: 261 }\n" + "=" * 47
    self.assertEqual(pdrive.cli_error_message(output), "SQLiteError: database is locked")
    self.assertEqual(pdrive.cli_error_message("Node not found: x"), "Node not found: x")

  def test_cli_error_message_ignores_details_dump(self):
    message = "ENOENT: no such file or directory, rename '/s/proton-drive.log.3' -> '/s/proton-drive.log.4'"
    output = "113 |             renameSync(from, to);\n                  ^\n" + message + "\n" \
      "    path: \"/s/proton-drive.log.3\",\n    code: \"ENOENT\"\n\n      at KEe (src/telemetry/fileHandler.ts:113:13)\n" \
      "Error details:\n{\n  code: 'ENOENT',\n  errno: -2\n}\n" + "=" * 47
    self.assertEqual(pdrive.cli_error_message(output), message)
    self.assertTrue(pdrive.TRANSIENT_ERRORS.search(output))

  def test_progress_is_reported_and_cleared(self):
    self.write_local("up.txt", "u")
    os.makedirs(self.remote_path("dir"))
    with open(self.remote_path("dir/down.txt"), "w") as handle:
      handle.write("d")
    seen = []
    original = pdrive.set_progress
    pdrive.set_progress = lambda text, *rest: (seen.append(text), original(text, *rest))
    try:
      self.sync()
    finally:
      pdrive.set_progress = original
    self.assertTrue(any(t.startswith("Scanning · ") for t in seen))
    self.assertIn("Uploading", seen)
    self.assertIn("Downloading", seen)
    self.assertEqual(pdrive.read_progress(), {"progress": "", "progressFraction": -1, "progressStarted": ""})

  def test_completed_sync_is_logged_to_activity(self):
    self.write_local("a.txt", "a")
    args = argparse.Namespace(local=self.local, remote=REMOTE, dry_run=False, allow_mass_delete=False, ignore="")
    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
      pdrive.command_sync(args)
    last = pdrive.read_activity(5)[0]
    self.assertEqual(last["kind"], "synced")
    self.assertEqual(last["path"], "")
    self.assertEqual(last["done"]["uploaded"], 1)

  def test_deep_tree_is_scanned_completely(self):
    for rel in ("a/b/c/d/deep.txt", "a/side.txt", "x/y.txt", "top.txt"):
      self.write_local(rel, rel)
    self.sync()
    files, dirs, _ = pdrive.scan_remote(pdrive.Cli(pdrive.cli_path()), REMOTE)
    self.assertEqual(set(files), {"a/b/c/d/deep.txt", "a/side.txt", "x/y.txt", "top.txt"})
    self.assertEqual(dirs, {"a", "a/b", "a/b/c", "a/b/c/d", "x"})

  def test_progress_with_total_has_percentage_and_fraction(self):
    pdrive.set_progress("Downloading", 120, 400)
    progress = pdrive.read_progress()
    self.assertEqual(progress["progress"], "Downloading · 30% · 120 of 400")
    self.assertEqual(progress["progressFraction"], 0.3)
    pdrive.set_progress("Scanning")
    self.assertEqual(pdrive.read_progress()["progressFraction"], -1)
    pdrive.set_progress("")

  def test_dry_run_changes_nothing(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.write_local("b.txt", "b")
    result = self.sync(dry_run=True)
    self.assertEqual(result["plan"]["upload"], 1)
    self.assertNotIn("b.txt", self.remote_files())
    self.assertFalse((pdrive.state_dir() / "progress.json").exists())

  def test_idle_syncs_do_not_stack_in_activity(self):
    idle = {"kind": "synced", "path": "", "detail": "", "done": {"uploaded": 0, "downloaded": 0}}
    pdrive.append_activity([dict(idle, time="2026-01-01T00:00:00Z")])
    pdrive.append_activity([{"time": "2026-01-01T00:01:00Z", "kind": "uploaded", "path": "a.txt", "detail": ""}])
    for minute in (2, 3, 4):
      pdrive.append_activity([dict(idle, time=f"2026-01-01T00:0{minute}:00Z")])
    lines = (pdrive.state_dir() / "activity.jsonl").read_text().splitlines()
    self.assertEqual([json.loads(line)["time"][-6:-1] for line in lines], ["00:00", "01:00", "04:00"])

  def test_activity_hides_errors_a_later_sync_got_past(self):
    pdrive.append_activity([
      {"time": "2026-01-01T00:00:00Z", "kind": "error", "path": "", "detail": "offline"},
      {"time": "2026-01-01T00:01:00Z", "kind": "uploaded", "path": "a.txt", "detail": ""},
      {"time": "2026-01-01T00:02:00Z", "kind": "synced", "path": "", "detail": "", "done": {"uploaded": 1}},
    ])
    self.assertEqual([row["kind"] for row in pdrive.read_activity(10)], ["synced", "uploaded"])

  # remote scan skipping ------------------------------------------------------

  def test_unchanged_drive_is_not_listed_again(self):
    self.write_local("a.txt", "a")
    self.write_remote("b.txt", "b")
    self.assertEqual(self.sync()["remoteScan"], "full")
    # The first sync uploaded a.txt, which moves the event ID: one more full scan.
    self.assertEqual(self.sync()["remoteScan"], "full")
    self.assertEqual(self.sync()["remoteScan"], "cached")
    self.assertEqual(self.sync()["remoteScan"], "cached")

  def test_remote_change_forces_a_full_scan(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    self.write_remote("new.txt", "from the web")
    result = self.sync()
    self.assertEqual(result["remoteScan"], "full")
    self.assertEqual(self.read_local("new.txt"), "from the web")

  def test_local_change_uploads_with_cached_tree(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    self.write_local("c.txt", "c")
    result = self.sync()
    self.assertEqual(result["remoteScan"], "cached")
    self.assertEqual(self.read_remote("c.txt"), "c")
    # Our own upload is folded into the saved tree, so no full scan follows.
    result = self.sync()
    self.assertEqual(result["remoteScan"], "cached")
    self.assertEqual(result["done"]["uploaded"] + result["done"]["downloaded"], 0)

  def test_others_change_before_our_writes_is_not_absorbed(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    self.write_local("c.txt", "c")
    original = pdrive.plan_sync

    def plan_then_someone_edits(*args, **kwargs):
      self.write_remote("phone.txt", "from the phone")
      return original(*args, **kwargs)

    pdrive.plan_sync = plan_then_someone_edits
    try:
      self.sync()
    finally:
      pdrive.plan_sync = original
    result = self.sync()
    self.assertEqual(result["remoteScan"], "full")
    self.assertEqual(self.read_local("phone.txt"), "from the phone")

  def test_folded_delete_then_web_change_is_seen(self):
    self.write_local("a.txt", "a")
    self.write_local("b.txt", "b")
    self.sync()
    self.sync()
    os.remove(os.path.join(self.local, "a.txt"))
    self.sync()
    self.assertEqual(self.sync()["remoteScan"], "cached")
    self.write_remote("web.txt", "w")
    self.assertEqual(self.sync()["remoteScan"], "full")
    self.assertEqual(sorted(self.local_files()), ["b.txt", "web.txt"])

  def test_local_delete_trashes_remote_with_cached_tree(self):
    self.write_local("a.txt", "a")
    self.write_local("b.txt", "b")
    self.sync()
    self.sync()
    os.remove(os.path.join(self.local, "a.txt"))
    self.assertEqual(self.sync()["remoteScan"], "cached")
    self.assertNotIn("a.txt", self.remote_files())

  def test_old_cache_forces_a_full_scan(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    cache_path = pdrive.state_dir() / "remote-cache.json"
    cache = json.load(open(cache_path))
    cache["scannedAt"] -= pdrive.FULL_SCAN_SECONDS + 1
    json.dump(cache, open(cache_path, "w"))
    self.assertEqual(self.sync()["remoteScan"], "full")

  def test_stale_events_file_forces_a_full_scan(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    # A CLI call that did not refresh events.json (an older or failing CLI)
    # proves nothing about the Drive.
    events = os.path.join(self.tmp, "cli-data", "events.json")
    aged = os.path.getmtime(events) - 60
    os.utime(events, (aged, aged))
    os.environ["FAKE_NO_EVENTS"] = "1"
    try:
      self.assertEqual(self.sync()["remoteScan"], "full")
    finally:
      os.environ.pop("FAKE_NO_EVENTS")

  def test_changing_the_ignore_list_forces_a_full_scan(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    result = pdrive.run_sync(self.local, REMOTE, ignore="build")
    self.assertEqual(result["remoteScan"], "full")

  def test_delete_during_a_sync_is_not_undone(self):
    self.write_local("a.txt", "a")
    self.write_local("b.txt", "b")
    self.sync()
    self.write_local("c.txt", "c")
    original = pdrive.plan_sync

    def plan_then_user_deletes(*args, **kwargs):
      actions = original(*args, **kwargs)
      os.remove(os.path.join(self.local, "a.txt"))
      return actions

    pdrive.plan_sync = plan_then_user_deletes
    try:
      self.sync()
    finally:
      pdrive.plan_sync = original
    result = self.sync()
    self.assertEqual(result["done"]["downloaded"], 0)
    self.assertNotIn("a.txt", self.local_files())
    self.assertNotIn("a.txt", self.remote_files())

  def test_fold_waits_for_our_own_events(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.sync()
    events = os.path.join(self.tmp, "cli-data", "events.json")
    stale = "VOLUME:" + json.load(open(events))["drive"]["VOLUME"]["lastEventId"]
    # Reads 1-2 come before our writes; read 3, right after them, still shows
    # the old ID, as Proton does for a moment.
    original = pdrive.drive_event_id
    reads = {"n": 0}

    def lagging(cli, root):
      reads["n"] += 1
      value = original(cli, root)
      return stale if reads["n"] <= 3 else value

    self.write_local("c.txt", "c")
    pdrive.drive_event_id = lagging
    try:
      self.sync()
    finally:
      pdrive.drive_event_id = original
    self.assertEqual(self.sync()["remoteScan"], "cached")

  def test_file_gone_before_upload_is_not_an_error(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.write_local("new.txt", "n")
    original = pdrive.plan_sync

    def plan_then_user_deletes(*args, **kwargs):
      actions = original(*args, **kwargs)
      os.remove(os.path.join(self.local, "new.txt"))
      return actions

    pdrive.plan_sync = plan_then_user_deletes
    try:
      result = self.sync()
    finally:
      pdrive.plan_sync = original
    self.assertEqual(result["errors"], [])
    self.assertNotIn("new.txt", self.remote_files())
    self.assertEqual(self.sync()["done"]["downloaded"], 0)

  def test_syncs_whole_of_my_files(self):
    self.write_local("top.txt", "top")
    pdrive.run_sync(self.local, "/my-files")
    with open(os.path.join(self.drive, "my-files", "top.txt")) as handle:
      self.assertEqual(handle.read(), "top")

  def test_refuses_remote_outside_my_files(self):
    with self.assertRaises(pdrive.SyncError):
      pdrive.run_sync(self.local, "/shared")


if __name__ == "__main__":
  unittest.main()
