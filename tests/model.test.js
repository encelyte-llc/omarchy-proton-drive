// node --test tests/
const test = require("node:test")
const assert = require("node:assert")
const fs = require("node:fs")
const path = require("node:path")
const Model = require("../Model.js")

const NOW = Date.parse("2026-10-30T12:00:00Z")

test("parseStatus fills defaults and tolerates a stray line", () => {
  const s = Model.parseStatus('warning\n{"installed":true,"authenticated":true}')
  assert.strictEqual(s.installed, true)
  assert.deepStrictEqual(s.activity, [])
  assert.strictEqual(Model.parseStatus("garbage").ok, false)
})

test("phase picks the most urgent state", () => {
  const base = Model.defaultStatus()
  assert.strictEqual(Model.phase(base, false, false), "missing")
  assert.strictEqual(Model.phase({ ...base, installed: true }, false, true), "signing-in")
  assert.strictEqual(Model.phase({ ...base, installed: true }, false, false), "signed-out")
  const signedIn = { ...base, installed: true, authenticated: true }
  assert.strictEqual(Model.phase(signedIn, false, false), "paused")
  assert.strictEqual(Model.phase({ ...signedIn, syncEnabled: true }, true, false), "syncing")
  assert.strictEqual(Model.phase({ ...signedIn, syncEnabled: true, lastSync: { ok: false } }, false, false), "error")
  assert.strictEqual(Model.phase({ ...signedIn, syncEnabled: true, lastSync: { ok: true } }, false, false), "idle")
})

test("formatting", () => {
  assert.strictEqual(Model.formatBytes(12629581646), "12.6 GB")
  assert.strictEqual(Model.formatCount(46202), "46,202")
  assert.strictEqual(Model.relativeTime("2026-10-30T11:58:00Z", NOW), "2m ago")
  assert.strictEqual(Model.relativeTime("", NOW), "never")
})

test("syncingText shows how long the current run has taken", () => {
  assert.strictEqual(Model.syncingText("2026-10-30T11:53:00Z", NOW), "Started · 7m")
  assert.strictEqual(Model.syncingText("2026-10-30T11:59:50Z", NOW), "Started · just started")
  assert.strictEqual(Model.syncingText("", NOW), "In progress")
  assert.strictEqual(Model.syncingText("2026-10-30T11:53:00Z", NOW, "Downloading · 12 of 300"), "Downloading · 12 of 300 · 7m")
  assert.strictEqual(Model.syncingText("2026-10-30T10:40:00Z", NOW, "Scanning"), "Scanning · 1h 20m")
  assert.strictEqual(Model.syncingText("", NOW, "Scanning"), "Scanning")
})

test("lastSyncText summarizes the run", () => {
  const last = { finished: "2026-10-30T11:55:00Z", ok: true, done: { uploaded: 2, downloaded: 1, conflicts: 1 } }
  assert.strictEqual(Model.lastSyncText(last, NOW), "5m ago · 3 files · 1 conflict")
  assert.strictEqual(Model.lastSyncText({ finished: "2026-10-30T11:55:00Z", ok: false }, NOW), "Failed 5m ago")
  assert.strictEqual(Model.lastSyncText({}, NOW), "Never")
})

test("paths", () => {
  assert.strictEqual(Model.expandHome("~/ProtonDrive", "/home/a"), "/home/a/ProtonDrive")
  assert.strictEqual(Model.normalizeRemote("Documents/"), "/my-files/Documents")
  assert.strictEqual(Model.normalizeRemote("/my-files//Omarchy"), "/my-files/Omarchy")
  assert.strictEqual(Model.remoteLabel("/my-files/Omarchy"), "My files/Omarchy")
})

test("activity rows", () => {
  const entry = { kind: "uploaded", path: "docs/report.pdf", time: "2026-10-30T11:00:00Z" }
  assert.strictEqual(Model.activityTitle(entry), "report.pdf")
  assert.strictEqual(Model.activityMeta(entry, NOW), "Uploaded · 1h ago · docs")
  const failed = { kind: "error", path: "", detail: "boom", time: "2026-10-30T11:00:00Z" }
  assert.strictEqual(Model.activityTitle(failed), "Sync failed")
  assert.strictEqual(Model.activityMeta(failed, NOW), "1h ago · boom")
  const synced = { kind: "synced", path: "", time: "2026-10-30T11:00:00Z", files: 9939, done: {} }
  assert.strictEqual(Model.activityTitle(synced), "Sync complete")
  assert.strictEqual(Model.activityMeta(synced, NOW), "1h ago · 9,939 files, all up to date")
  synced.done = { uploaded: 3, downloaded: 1 }
  assert.strictEqual(Model.activityMeta(synced, NOW), "1h ago · 3 up, 1 down")
})

test("extractUrl finds the sign-in link", () => {
  const line = "https://account.proton.me/desktop/login?app=drive&pv=3#payload=abc"
  assert.strictEqual(Model.extractUrl("Open: " + line), line)
  assert.strictEqual(Model.extractUrl("nothing here"), "")
})

test("manifest matches what omarchy plugin validate enforces", () => {
  const root = path.join(__dirname, "..")
  const m = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"))
  assert.strictEqual(m.schemaVersion, 1)
  for (const field of ["id", "name", "version", "author", "description", "kinds", "entryPoints"]) assert.ok(m[field], field)
  assert.match(m.id, /^[A-Za-z0-9][A-Za-z0-9._-]*$/)
  assert.ok(!m.id.startsWith("omarchy."))
  assert.ok(m.kinds.includes("bar-widget"))
  assert.ok(fs.existsSync(path.join(root, m.entryPoints.barWidget)))
  for (const field of m.barWidget.schema) assert.strictEqual(m.barWidget.defaults[field.key], field.defaultValue, field.key)
})
