import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.encelyte-llc.proton-drive"
  ipcTarget: "io.github.encelyte-llc.proton-drive"
  manageIpc: false

  // Keyboard cursor: "header" is the sync switch, "rows" walks the action
  // rows and then the activity list as one sequence.
  property string focusSection: "rows"
  property int rowIndex: 0
  property bool cursorActive: false
  property double nowMs: Date.now()

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  // Icon glyphs have uneven advance widths; a fixed slot keeps every row's
  // text starting at the same x.
  readonly property int glyphSlot: Math.ceil(Style.font.icon * 1.25)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string phase: drive.phase
  readonly property bool lit: phase === "idle" || phase === "syncing"
  readonly property bool massDeleteBlocked: !!(drive.status.lastSync && drive.status.lastSync.ok === false
    && String(drive.status.lastSync.error || "").indexOf("stopped to be safe") >= 0)

  // The action rows on screen right now, in order. Activity rows follow.
  readonly property var actions: {
    if (!drive.installed) return [{ key: "install", glyph: "󰇚", title: drive.installing ? "Installing…" : "Install Proton Drive CLI", detail: "Proton's official CLI, checksum-verified" }]
    if (drive.loggingIn) return [
      { key: "openLogin", glyph: "󰌷", title: "Open sign-in page", detail: drive.loginUrl !== "" ? "If your browser did not open" : "Waiting for the CLI…" },
      { key: "cancelLogin", glyph: "󰜺", title: "Cancel sign-in", detail: "" }
    ]
    if (!drive.authenticated) return [{ key: "login", glyph: "󰌆", title: "Sign in to Proton Drive", detail: "Opens Proton's sign-in page in your browser" }]
    var list = [
      { key: "sync", glyph: "󰓦", title: root.phase === "syncing" ? "Syncing…" : "Sync now",
        detail: root.phase === "syncing" ? Model.syncingText(drive.syncStartedAt, root.nowMs, drive.status.progress) : Model.lastSyncText(drive.status.lastSync, root.nowMs) }
    ]
    if (massDeleteBlocked) list.push({ key: "syncDeletes", glyph: "󰩹", title: "Sync and allow deletions", detail: "Moves the deleted files to the Trash" })
    list.push({ key: "folder", glyph: "󰉋", title: "Open sync folder", detail: drive.localFolder.replace(drive.home, "~") + "  ⇄  " + Model.remoteLabel(drive.remoteFolder) })
    list.push({ key: "web", glyph: "󰖟", title: "Open Proton Drive", detail: "drive.proton.me" })
    list.push({ key: "logout", glyph: "󰍃", title: "Sign out", detail: drive.status.account || "" })
    return list
  }
  readonly property var activityRows: drive.authenticated ? drive.activity : []
  readonly property int rowCount: actions.length + activityRows.length

  function runAction(key) {
    switch (key) {
      case "install": drive.installCli(); break
      case "login": drive.login(); break
      case "openLogin": drive.openLoginUrl(); break
      case "cancelLogin": drive.cancelLogin(); break
      case "sync": drive.syncNow(false); break
      case "syncDeletes": drive.syncNow(true); break
      case "folder": drive.openLocalFolder(); break
      case "web": drive.openDriveWeb(); break
      case "logout": drive.logout(); break
    }
  }

  function activateRow(index) {
    if (index < actions.length) runAction(actions[index].key)
    else drive.openActivity(activityRows[index - actions.length])
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    if (dy === 0) return
    if (focusSection === "header") {
      if (dy > 0 && rowCount > 0) { focusSection = "rows"; rowIndex = 0 }
      return
    }
    if (dy < 0 && rowIndex === 0 && drive.authenticated) {
      focusSection = "header"
      panelFlick.contentY = 0
      return
    }
    rowIndex = Math.max(0, Math.min(rowCount - 1, rowIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    if (focusSection === "header") drive.toggleSync()
    else if (rowCount > 0) activateRow(Math.min(rowIndex, rowCount - 1))
  }

  function setRowCursor(index) {
    cursorActive = true
    focusSection = "rows"
    rowIndex = index
  }

  function rowItem(index) {
    if (index < actions.length) return actionColumn.children[index]
    return activityColumn.children[index - actions.length]
  }

  function scrollCursorIntoView() {
    var item = rowItem(rowIndex)
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      var margin = Style.space(6)
      var top = item.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    focusSection = "rows"
    rowIndex = 0
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    drive.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onRowCountChanged: if (rowIndex >= rowCount) rowIndex = Math.max(0, rowCount - 1)

  Service {
    id: drive
    settings: root.settings
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { drive.refresh(); return "ok" }
    function sync(): string { drive.syncNow(false); return "ok" }
    function status(): string { return drive.phase }
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: root.nowMs = Date.now()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: Model.barGlyph(root.phase)
    dimmed: !root.lit && root.phase !== "error"
    active: root.phase === "error"
    tooltipText: "Proton Drive · " + Model.heroMeta(root.phase, drive.status, root.nowMs)
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) drive.syncNow(false)
      else if (buttonCode === Qt.MiddleButton) drive.openLocalFolder()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(600))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var key = String(t).toLowerCase()
        if (key === "s") drive.syncNow(false)
        else if (key === "r") drive.refresh()
        else if (key === "o") drive.openLocalFolder()
        else if (key === "w") drive.openDriveWeb()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.cursorActive && root.focusSection === "header"
            function focusHero() { root.cursorActive = true; root.focusSection = "header" }

            PanelHero {
              id: hero
              width: parent.width
              title: "Proton Drive"
              meta: Model.heroMeta(root.phase, drive.status, root.nowMs)
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: root.lit ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  text: Model.barGlyph(root.phase)
                  color: root.phase === "error" ? root.urgent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  id: syncSwitch
                  visible: drive.authenticated
                  checked: drive.syncEnabled
                  busy: drive.syncing
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: drive.toggleSync()

                  PanelToolTip {
                    visible: syncSwitch.containsMouse
                    text: drive.syncEnabled ? "Turn automatic sync off" : "Sync every " + drive.syncIntervalMin + " minutes"
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: drive.actionStatus !== "" || drive.lastError !== ""
            width: parent.width
            text: drive.actionStatus !== "" ? drive.actionStatus : drive.lastError
            color: drive.lastError !== "" && drive.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            visible: drive.authenticated
            width: parent.width
            spacing: Style.spacing.labelGap

            InfoPair { label: "Account"; value: drive.status.account || "—" }
            InfoPair {
              label: "Stored"
              value: drive.status.storageKnown
                ? Model.formatBytes(drive.status.usedBytes) + " · " + Model.formatCount(drive.status.fileCount) + " items"
                : "—"
            }
          }

          PanelSeparator {
            visible: drive.authenticated
            foreground: root.foreground
          }

          Column {
            id: actionColumn
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: root.actions
              ActionRow {
                required property var modelData
                required property int index
                width: actionColumn.width
                action: modelData
                rowIndex: index
              }
            }
          }

          PanelSeparator {
            visible: root.activityRows.length > 0
            foreground: root.foreground
          }

          Column {
            visible: root.activityRows.length > 0
            width: parent.width
            spacing: Style.space(10)

            RowLayout {
              width: parent.width

              PanelSectionHeader {
                text: "RECENT ACTIVITY"
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.fillWidth: true
              }

              Text {
                id: clearActivity
                textFormat: Text.PlainText
                text: "Clear"
                color: clearArea.containsMouse ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                Layout.alignment: Qt.AlignVCenter

                MouseArea {
                  id: clearArea
                  anchors.fill: parent
                  anchors.margins: -Style.space(4)
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: drive.clearActivity()
                }
              }
            }

            Column {
              id: activityColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.activityRows
                ActivityRow {
                  required property var modelData
                  required property int index
                  width: activityColumn.width
                  entry: modelData
                  rowIndex: root.actions.length + index
                }
              }
            }
          }
        }
      }
    }
  }

  component ActionRow: CursorSurface {
    id: actionRow
    property var action: ({})
    property int rowIndex: 0

    hasCursor: root.cursorActive && root.focusSection === "rows" && root.rowIndex === rowIndex
    foreground: root.foreground
    implicitHeight: actionContent.implicitHeight + Style.spacing.rowPaddingX
    opacity: drive.busy && (action.key === "sync" || action.key === "install") ? 0.6 : 1.0

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setRowCursor(actionRow.rowIndex)
      onClicked: root.runAction(actionRow.action.key)
    }

    // Transfer progress along the bottom of the sync row; hidden while the
    // sync is still scanning and has no total to measure against.
    Rectangle {
      id: progressTrack
      readonly property real fraction: Number(drive.status.progressFraction)
      visible: actionRow.action.key === "sync" && root.phase === "syncing" && fraction >= 0
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      anchors.bottomMargin: Style.space(3)
      height: 2
      radius: 1
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.15)

      Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: parent.width * Math.max(0, Math.min(1, progressTrack.fraction))
        radius: 1
        color: root.foreground

        Behavior on width {
          NumberAnimation { duration: 400; easing.type: Easing.OutCubic }
        }
      }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        id: actionGlyph
        readonly property bool spinning: actionRow.action.key === "sync" && root.phase === "syncing"
        textFormat: Text.PlainText
        text: actionRow.action.glyph || ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        horizontalAlignment: Text.AlignHCenter
        Layout.preferredWidth: root.glyphSlot
        Layout.alignment: Qt.AlignVCenter
        onSpinningChanged: if (!spinning) rotation = 0

        RotationAnimator on rotation {
          running: actionGlyph.spinning && root.opened
          from: 0
          to: 360
          duration: 1400
          loops: Animation.Infinite
        }
      }

      ColumnLayout {
        id: actionContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: actionRow.action.title || ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: text !== ""
          Layout.fillWidth: true
          text: actionRow.action.detail || ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideMiddle
        }
      }
    }
  }

  component ActivityRow: CursorSurface {
    id: activityRow
    property var entry: ({})
    property int rowIndex: 0

    hasCursor: root.cursorActive && root.focusSection === "rows" && root.rowIndex === rowIndex
    foreground: root.foreground
    implicitHeight: activityContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setRowCursor(activityRow.rowIndex)
      onClicked: drive.openActivity(activityRow.entry)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: Model.activityGlyph(activityRow.entry.kind)
        color: activityRow.entry.kind === "error" ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        horizontalAlignment: Text.AlignHCenter
        Layout.preferredWidth: root.glyphSlot
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: activityContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.activityTitle(activityRow.entry)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.activityMeta(activityRow.entry, root.nowMs)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""

    width: parent.width
    spacing: Style.space(8)

    Text {
      id: infoLabel
      textFormat: Text.PlainText
      text: parent.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Text {
      textFormat: Text.PlainText
      width: parent.width - infoLabel.implicitWidth - parent.spacing
      horizontalAlignment: Text.AlignRight
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideLeft
    }
  }
}
