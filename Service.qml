import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Owns every process the widget runs. All work goes through pdrive.py, which
// wraps Proton's official `proton-drive` CLI and prints one JSON object.
Item {
  id: root

  property var settings: ({})

  readonly property string helperPath: Qt.resolvedUrl("pdrive.py").toString().replace(/^file:\/\//, "")
  readonly property string home: Quickshell.env("HOME") || ""

  property var status: Model.defaultStatus()
  property bool loaded: false
  property bool refreshing: false
  property string actionStatus: ""
  property string lastError: ""
  property string loginUrl: ""
  // Optimistic switch: -1 follows the saved state, 0/1 while a change lands.
  property int _desired: -1

  readonly property bool installed: status.installed === true
  readonly property bool authenticated: status.authenticated === true
  readonly property bool syncEnabled: _desired === -1 ? status.syncEnabled === true : _desired === 1
  readonly property bool syncing: syncProcess.running
  property string syncStartedAt: ""
  readonly property bool loggingIn: loginProcess.running
  readonly property bool installing: installProcess.running
  readonly property bool busy: syncProcess.running || loginProcess.running || installProcess.running || actionProcess.running
  readonly property string phase: Model.phase(status, syncing, loggingIn)
  readonly property var activity: status.activity || []

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 300, 60, 3600)
  readonly property int syncIntervalMin: intSetting("syncIntervalMin", 10, 2, 1440)
  readonly property string localFolder: Model.expandHome(String(setting("localFolder", "~/ProtonDrive") || "~/ProtonDrive"), home)
  readonly property string remoteFolder: Model.normalizeRemote(setting("remoteFolder", "/my-files/Omarchy") || "/my-files/Omarchy")
  readonly property string ignore: String(setting("ignore", "node_modules, __pycache__, .venv, .cache, .fetch-cache"))

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null || value === "" ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function elide(text) {
    var value = String(text || "").replace(/\s+/g, " ").trim()
    return value.length > 160 ? value.substring(0, 157) + "…" : value
  }

  function flash(text) {
    actionStatus = text
    actionStatusTimer.restart()
  }

  function refresh() {
    if (statusProcess.running) return
    refreshing = true
    statusProcess.command = ["python3", helperPath, "status", "--limit", "12"]
    statusProcess.running = true
  }

  function syncNow(allowMassDelete) {
    if (!installed || !authenticated || syncProcess.running) return
    lastError = ""
    var command = ["python3", helperPath, "sync", "--local", localFolder, "--remote", remoteFolder, "--ignore", ignore]
    if (allowMassDelete === true) command.push("--allow-mass-delete")
    syncProcess.command = command
    syncProcess.running = true
  }

  function setSyncEnabled(on) {
    if (!authenticated || actionProcess.running) return
    _desired = on ? 1 : 0
    runAction(["python3", helperPath, "set-sync", on ? "on" : "off"], on ? "" : "Sync turned off")
    if (on) firstSync.restart()
  }

  function toggleSync() {
    setSyncEnabled(!syncEnabled)
  }

  function login() {
    if (!installed || loginProcess.running) return
    loginUrl = ""
    lastError = ""
    actionStatus = "Opening Proton sign-in in your browser…"
    loginProcess.command = ["python3", helperPath, "login"]
    loginProcess.running = true
  }

  function cancelLogin() {
    if (loginProcess.running) loginProcess.signal(15)
  }

  function openLoginUrl() {
    if (loginUrl !== "") Qt.openUrlExternally(loginUrl)
  }

  function logout() {
    if (!installed || actionProcess.running) return
    runAction(["python3", helperPath, "logout"], "Signed out of Proton Drive")
  }

  function installCli() {
    if (installProcess.running) return
    lastError = ""
    actionStatus = "Downloading Proton's official CLI…"
    installProcess.running = true
  }

  function clearActivity() {
    if (actionProcess.running) return
    var next = Object.assign({}, status)
    next.activity = []
    status = next
    runAction(["python3", helperPath, "clear-activity"], "")
  }

  function runAction(command, successText) {
    actionProcess.successText = successText
    actionProcess.command = command
    actionProcess.running = true
  }

  function openLocalFolder() {
    Quickshell.execDetached(["mkdir", "-p", localFolder])
    Quickshell.execDetached(["uwsm-app", "--", "nautilus", localFolder])
  }

  function openDriveWeb() {
    Qt.openUrlExternally("https://drive.proton.me/")
  }

  function openActivity(entry) {
    if (!entry || !entry.path || entry.kind === "trashed-local" || entry.kind === "trashed-remote" || entry.kind === "error") {
      openLocalFolder()
      return
    }
    var parts = (localFolder + "/" + String(entry.path)).split("/")
    for (var i = 0; i < parts.length; i++) parts[i] = encodeURIComponent(parts[i])
    Quickshell.execDetached(["uwsm-app", "--", "nautilus", "--select", "file://" + parts.join("/")])
  }

  function notify(summary, body, urgency) {
    Quickshell.execDetached(["notify-send", "-a", "Proton Drive", "-u", urgency || "normal", summary, body])
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    if (!parsed.ok) {
      lastError = parsed.error
      return
    }
    status = parsed
    loaded = true
    if (_desired !== -1 && parsed.syncEnabled === (_desired === 1)) _desired = -1
    lastError = parsed.error ? elide(parsed.error) : ""
  }

  function applySync(raw, exitCode) {
    var result = Model.parseJson(raw) || {}
    var done = result.done || {}
    if (exitCode !== 0 || result.ok === false) {
      var message = result.error || (result.errors && result.errors[0]) || "Sync failed"
      if (message === "A sync is already running") return
      lastError = elide(message)
      notify("Sync problem", lastError, "critical")
    } else if (Number(done.conflicts || 0) > 0) {
      notify("Sync conflict", done.conflicts + " file(s) changed in both places. Both versions were kept.", "normal")
    }
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: syncTimer
    interval: root.syncIntervalMin * 60 * 1000
    repeat: true
    running: root.syncEnabled && root.authenticated
    onTriggered: root.syncNow(false)
  }

  Timer {
    // Shortly after the shell starts (or sync is switched on), sync once
    // instead of waiting a full interval.
    id: firstSync
    interval: 15000
    repeat: false
    running: root.loaded && root.syncEnabled && root.authenticated
    onTriggered: root.syncNow(false)
  }

  Timer {
    // While a sync runs, pick up its activity as it is written. Only the
    // activity log is read; the full status call talks to Proton.
    interval: 4000
    repeat: true
    running: root.syncing
    onTriggered: if (!activityProcess.running) activityProcess.running = true
  }

  Timer {
    id: actionStatusTimer
    interval: 2600
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  Process {
    id: activityProcess
    command: ["python3", root.helperPath, "activity", "--limit", "12"]
    stdout: StdioCollector { id: activityOut; waitForEnd: true }
    onExited: function(exitCode) {
      var result = Model.parseJson(activityOut.text)
      if (exitCode !== 0 || !result || !result.activity) return
      var next = Object.assign({}, root.status)
      next.activity = result.activity
      next.progress = result.progress || ""
      root.status = next
    }
  }

  Process {
    id: statusProcess
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      root.applyStatus(statusOut.text)
    }
  }

  Process {
    id: syncProcess
    command: []
    stdout: StdioCollector { id: syncOut; waitForEnd: true }
    onRunningChanged: if (running) root.syncStartedAt = new Date().toISOString()
    onExited: function(exitCode) {
      root.applySync(syncOut.text, exitCode)
      root.refresh()
    }
  }

  Process {
    id: loginProcess
    command: []
    stdout: SplitParser {
      onRead: function(line) {
        var url = Model.extractUrl(line)
        if (url !== "") root.loginUrl = url
        if (/successful/i.test(line)) root.flash("Signed in to Proton Drive")
      }
    }
    stderr: SplitParser {
      onRead: function(line) {
        var url = Model.extractUrl(line)
        if (url !== "") root.loginUrl = url
      }
    }
    onExited: function(exitCode) {
      root.loginUrl = ""
      if (exitCode !== 0 && root.actionStatus.indexOf("Signed in") !== 0) {
        root.actionStatus = ""
        root.lastError = "Sign-in did not complete"
      }
      root.refresh()
    }
  }

  Process {
    id: installProcess
    command: ["python3", root.helperPath, "install-cli"]
    stdout: StdioCollector { id: installOut; waitForEnd: true }
    onExited: function(exitCode) {
      var result = Model.parseJson(installOut.text) || {}
      if (exitCode === 0 && result.ok) root.flash("Installed Proton Drive CLI " + (result.version || ""))
      else {
        root.actionStatus = ""
        root.lastError = root.elide(result.error || "Install failed")
      }
      root.refresh()
    }
  }

  Process {
    id: actionProcess
    property string successText: ""
    command: []
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    onExited: function(exitCode) {
      var result = Model.parseJson(actionOut.text) || {}
      if (exitCode === 0 && result.ok !== false) {
        if (actionProcess.successText !== "") root.flash(actionProcess.successText)
      } else {
        root._desired = -1
        root.lastError = root.elide(result.error || "Command failed")
      }
      root.refresh()
    }
  }
}
