"""Sync engine tests against the fake CLI. Run: python3 -m unittest discover tests"""

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
    }
    self.saved_env = {k: os.environ.get(k) for k in self.env}
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

  def test_dry_run_changes_nothing(self):
    self.write_local("a.txt", "a")
    self.sync()
    self.write_local("b.txt", "b")
    result = self.sync(dry_run=True)
    self.assertEqual(result["plan"]["upload"], 1)
    self.assertNotIn("b.txt", self.remote_files())

  def test_refuses_whole_drive(self):
    with self.assertRaises(pdrive.SyncError):
      pdrive.run_sync(self.local, "/my-files")


if __name__ == "__main__":
  unittest.main()
