// Pure helpers for the Proton Drive widget. No QML types here, so the
// functions run under `node --test tests/` as well as inside the shell.

var GLYPH_CLOUD = String.fromCodePoint(0xF015F)        // md-cloud
var GLYPH_SYNC = String.fromCodePoint(0xF063F)         // md-cloud-sync
var GLYPH_CHECK = String.fromCodePoint(0xF0160)        // md-cloud-check
var GLYPH_OFF = String.fromCodePoint(0xF0164)          // md-cloud-off-outline
var GLYPH_ALERT = String.fromCodePoint(0xF09E0)        // md-cloud-alert

function defaultStatus() {
  return {
    ok: true,
    installed: false,
    cliPath: "",
    version: "",
    authenticated: false,
    account: "",
    usedBytes: 0,
    fileCount: 0,
    storageKnown: false,
    syncEnabled: false,
    syncRunning: false,
    lastSync: {},
    activity: [],
    progress: "",
    progressFraction: -1,
    error: ""
  }
}

function parseJson(raw) {
  var text = String(raw || "").trim()
  if (text === "") return null
  // The helper prints exactly one JSON object, but be tolerant of a stray
  // warning line before it.
  var start = text.indexOf("{")
  if (start < 0) return null
  try {
    var parsed = JSON.parse(text.substring(start))
    return parsed && typeof parsed === "object" ? parsed : null
  } catch (e) {
    return null
  }
}

function parseStatus(raw) {
  var parsed = parseJson(raw)
  if (!parsed) {
    var failed = defaultStatus()
    failed.ok = false
    failed.error = "Could not read Proton Drive status"
    return failed
  }
  var base = defaultStatus()
  for (var key in base) {
    if (parsed[key] === undefined || parsed[key] === null) parsed[key] = base[key]
  }
  if (!Array.isArray(parsed.activity)) parsed.activity = []
  if (typeof parsed.lastSync !== "object") parsed.lastSync = {}
  return parsed
}

// The state the bar icon and hero describe, most urgent first.
function phase(status, syncing, loginRunning) {
  if (!status.installed) return "missing"
  if (loginRunning) return "signing-in"
  if (!status.authenticated) return "signed-out"
  if (syncing || status.syncRunning) return "syncing"
  if (status.lastSync && status.lastSync.ok === false && status.syncEnabled) return "error"
  if (!status.syncEnabled) return "paused"
  return "idle"
}

function barGlyph(phaseName) {
  if (phaseName === "syncing") return GLYPH_SYNC
  if (phaseName === "error") return GLYPH_ALERT
  if (phaseName === "idle") return GLYPH_CHECK
  if (phaseName === "missing" || phaseName === "signed-out") return GLYPH_OFF
  return GLYPH_CLOUD
}

function formatBytes(bytes) {
  var value = Number(bytes || 0)
  if (!isFinite(value) || value <= 0) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB"]
  var index = 0
  while (value >= 1000 && index < units.length - 1) {
    value = value / 1000
    index++
  }
  var decimals = value >= 100 || index === 0 ? 0 : (value >= 10 ? 1 : 2)
  return value.toFixed(decimals).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1") + " " + units[index]
}

function formatCount(n) {
  var value = Math.max(0, Math.floor(Number(n || 0)))
  return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",")
}

function isoToMs(value) {
  var ms = Date.parse(String(value || ""))
  return isFinite(ms) ? ms : 0
}

function relativeTime(iso, nowMs) {
  var ms = isoToMs(iso)
  if (!ms) return "never"
  var now = nowMs === undefined ? Date.now() : Number(nowMs)
  var diff = Math.max(0, Math.floor((now - ms) / 1000))
  if (diff < 60) return "just now"
  var minutes = Math.floor(diff / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  if (days < 30) return days + "d ago"
  return Math.floor(days / 30) + "mo ago"
}

function heroMeta(phaseName, status, nowMs) {
  switch (phaseName) {
    case "missing": return "CLI not installed"
    case "signing-in": return "Waiting for sign-in"
    case "signed-out": return "Signed out"
    case "syncing": return "Syncing"
    case "error": return "Sync failed"
    case "paused": return "Sync off"
  }
  var last = status.lastSync || {}
  return last.finished ? "Synced " + relativeTime(last.finished, nowMs) : "Ready to sync"
}

function syncingText(startedAt, nowMs, progress) {
  var started = ""
  if (startedAt) {
    var when = relativeTime(startedAt, nowMs)
    started = when === "just now" ? "started just now" : "started " + when
  }
  if (progress) return started ? progress + " · " + started : progress
  if (!started) return "In progress"
  return started.charAt(0).toUpperCase() + started.substring(1)
}

function lastSyncText(last, nowMs) {
  if (!last || !last.finished) return "Never"
  var when = relativeTime(last.finished, nowMs)
  if (last.ok === false) return "Failed " + when
  var done = last.done || {}
  var moved = Number(done.uploaded || 0) + Number(done.downloaded || 0)
  var parts = [when]
  if (moved > 0) parts.push(moved + (moved === 1 ? " file" : " files"))
  if (Number(done.conflicts || 0) > 0) parts.push(done.conflicts + " conflict" + (done.conflicts === 1 ? "" : "s"))
  return parts.join(" · ")
}

var ACTIVITY = {
  "uploaded": { glyph: String.fromCodePoint(0xF0552), verb: "Uploaded" },       // md-upload
  "downloaded": { glyph: String.fromCodePoint(0xF01DA), verb: "Downloaded" },   // md-download
  "conflict": { glyph: String.fromCodePoint(0xF0A10), verb: "Conflict" },       // md-file-compare
  "trashed-local": { glyph: String.fromCodePoint(0xF0A7A), verb: "Trashed here" }, // md-trash-can-outline
  "trashed-remote": { glyph: String.fromCodePoint(0xF0A7A), verb: "Trashed in Drive" },
  "error": { glyph: String.fromCodePoint(0xF0026), verb: "Error" },             // md-alert
  "skipped": { glyph: String.fromCodePoint(0xF0156), verb: "Skipped" }          // md-close-circle-outline
}

function activityGlyph(kind) {
  return (ACTIVITY[kind] || ACTIVITY.error).glyph
}

function activityTitle(entry) {
  var path = String(entry && entry.path || "")
  if (path === "") return String(entry && entry.detail || "Sync")
  var name = path.split("/").pop()
  return name || path
}

function activityMeta(entry, nowMs) {
  var info = ACTIVITY[entry && entry.kind] || ACTIVITY.error
  var parts = [info.verb, relativeTime(entry && entry.time, nowMs)]
  var path = String(entry && entry.path || "")
  var folder = path.indexOf("/") >= 0 ? path.substring(0, path.lastIndexOf("/")) : ""
  if (folder !== "") parts.push(folder)
  return parts.join(" · ")
}

function expandHome(path, home) {
  var value = String(path || "").trim()
  if (value === "~") return home
  if (value.indexOf("~/") === 0) return home + value.substring(1)
  return value
}

function normalizeRemote(path) {
  var value = String(path || "").trim()
  if (value.indexOf("/") !== 0) value = "/my-files/" + value
  value = value.replace(/\/+/g, "/").replace(/\/$/, "")
  return value || "/my-files"
}

function remoteLabel(path) {
  var value = normalizeRemote(path)
  return value.indexOf("/my-files") === 0 ? "My files" + value.substring("/my-files".length) : value
}

function extractUrl(text) {
  var match = String(text || "").match(/https:\/\/account\.proton\.me\/\S+/)
  return match ? match[0] : ""
}

if (typeof module !== "undefined") {
  module.exports = {
    defaultStatus: defaultStatus,
    parseJson: parseJson,
    parseStatus: parseStatus,
    phase: phase,
    barGlyph: barGlyph,
    formatBytes: formatBytes,
    formatCount: formatCount,
    relativeTime: relativeTime,
    heroMeta: heroMeta,
    lastSyncText: lastSyncText,
    syncingText: syncingText,
    activityGlyph: activityGlyph,
    activityTitle: activityTitle,
    activityMeta: activityMeta,
    expandHome: expandHome,
    normalizeRemote: normalizeRemote,
    remoteLabel: remoteLabel,
    extractUrl: extractUrl
  }
}
