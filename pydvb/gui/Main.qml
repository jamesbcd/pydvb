pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: window
    required property var controller
    required property bool playerAvailable
    visible: true
    width: 1180
    height: 780
    minimumWidth: 860
    minimumHeight: 600
    title: "PyDVB"
    color: "#f2f4f1"

    property int sectionIndex: 0
    property int editingTuningIndex: -1
    property string selectedChannelId: ""
    property var logWindowObject: null
    property bool scanTablesRefreshAttempted: false
    property bool playerOnlyMode: false
    property bool playerSidebarRevealed: false
    property int previousWindowWidth: width
    property int previousWindowHeight: height
    property int previousWindowVisibility: Window.Windowed
    property real deviceNameColumnWidth: 226
    property real deviceStateColumnWidth: 120
    property real adapterNameColumnWidth: 210
    property real adapterStateColumnWidth: 140
    property real tuningIndexColumnWidth: 48
    property real tuningFrequencyColumnWidth: 180
    property real tuningBandwidthColumnWidth: 110
    readonly property var sectionNames: ["Devices", "Tuning", "Channels", "Server"]

    component ResizableHeaderCell: Item {
        id: headerCell
        required property string title
        required property real columnWidth
        required property real minimumColumnWidth
        signal resized(real newWidth)

        implicitHeight: 22
        Layout.fillWidth: false
        Layout.preferredWidth: columnWidth
        Layout.minimumWidth: minimumColumnWidth
        Layout.maximumWidth: columnWidth

        Label {
            anchors.fill: parent
            text: headerCell.title
            color: "#77847b"
            font.pixelSize: 11
            font.bold: true
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        Item {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 12

            HoverHandler {
                cursorShape: Qt.SplitHCursor
            }

            DragHandler {
                target: null
                property real widthAtStart: 0
                onActiveChanged: {
                    if (active)
                        widthAtStart = headerCell.columnWidth
                }
                onTranslationChanged: {
                    if (active)
                        headerCell.resized(Math.max(
                            headerCell.minimumColumnWidth,
                            widthAtStart + translation.x
                        ))
                }
            }
        }
    }

    function enterPlayerFullScreen() {
        if (visibility !== Window.FullScreen) {
            previousWindowWidth = width
            previousWindowHeight = height
            previousWindowVisibility = visibility
        }
        playerOnlyMode = true
        visibility = Window.FullScreen
    }

    function exitFullScreen() {
        if (visibility !== Window.FullScreen)
            return

        visibility = Window.Windowed
        width = previousWindowWidth
        height = previousWindowHeight
        if (previousWindowVisibility !== Window.Windowed)
            visibility = previousWindowVisibility
    }

    onPlayerOnlyModeChanged: {
        if (!playerOnlyMode) {
            playerSidebarRevealed = false
            playerSidebarHideTimer.stop()
        }
    }

    onSectionIndexChanged: {
        if (sectionIndex === 1 && !scanTablesRefreshAttempted) {
            scanTablesRefreshAttempted = true
            window.controller.tuningController.refreshKnownTables()
        }
    }

    function toggleLogWindow() {
        if (logWindowObject === null) {
            logWindowObject = logWindowComponent.createObject(window, {
                appController: window.controller
            })
            if (logWindowObject === null) {
                console.error("Could not create the log window")
                return
            }
        }
        logWindowObject.visible = !logWindowObject.visible
        if (logWindowObject.visible)
            logWindowObject.requestActivate()
    }

    function sectionStatus(index) {
        switch (index) {
        case 0:
            return window.controller.devicesController.deviceSummary
        case 1:
            return window.controller.tuningController.tunings.length > 0
                ? window.controller.tuningController.tunings.length + " tuning(s) loaded"
                : "No tunings loaded"
        case 2:
            return window.controller.channelController.hasChannelMap ? "A channel map is active" : "No channel map loaded"
        case 3:
            return window.controller.serverController.serverState === "Running" ? "Server is running" : "Server is not running"
        case 4:
            return playerLoader.playbackAvailable
                ? "Play a channel from the local HTTP server"
                : "Install pydvb[player] to enable playback"
        default:
            return ""
        }
    }

    function sectionReady(index) {
        switch (index) {
        case 0:
            return window.controller.devicesController.devices.some(function(device) { return device.adapterCount > 0 })
        case 1:
            return window.controller.tuningController.tunings.length > 0
        case 2:
            return window.controller.channelController.hasChannelMap
        case 3:
            return window.controller.serverController.serverState === "Running"
        case 4:
            return playerLoader.playbackAvailable && window.selectedChannelId.length > 0
        default:
            return false
        }
    }

    function channelExists(channelId) {
        return window.controller.channelController.channels.some(function(channel) {
            return channel.id === channelId
        })
    }

    function focusedTextEditor() {
        const item = window.activeFocusItem
        return item && typeof item.copy === "function" ? item : null
    }

    function canCut() {
        const editor = window.focusedTextEditor()
        return editor && !editor.readOnly && editor.selectedText.length > 0
    }

    function canCopy() {
        const editor = window.focusedTextEditor()
        return editor && editor.selectedText.length > 0
    }

    function canPaste() {
        const editor = window.focusedTextEditor()
        return editor && !editor.readOnly
    }

    function canUndo() {
        const editor = window.focusedTextEditor()
        return editor && editor.canUndo
    }

    function canRedo() {
        const editor = window.focusedTextEditor()
        return editor && editor.canRedo
    }

    onSelectedChannelIdChanged: {
        if (playerLoader.item)
            playerLoader.item.selectedChannelId = selectedChannelId
    }

    onClosing: function(closeEvent) {
        if (!window.controller.shuttingDown) {
            closeEvent.accepted = false
            window.controller.shutdown()
        }
    }

    Action {
        id: openProfileAction
        text: "&Open..."
        shortcut: StandardKey.Open
        onTriggered: openProfileDialog.open()
    }

    Action {
        id: saveProfileAction
        text: "&Save..."
        shortcut: StandardKey.Save
        onTriggered: saveProfileDialog.open()
    }

    Action {
        id: quitAction
        text: "&Quit"
        shortcut: StandardKey.Quit
        onTriggered: window.close()
    }

    Action {
        id: cutAction
        text: "Cu&t"
        shortcut: StandardKey.Cut
        enabled: window.canCut()
        onTriggered: window.focusedTextEditor().cut()
    }

    Action {
        id: undoAction
        text: "&Undo"
        shortcut: StandardKey.Undo
        enabled: window.canUndo()
        onTriggered: window.focusedTextEditor().undo()
    }

    Action {
        id: redoAction
        text: "&Redo"
        shortcut: StandardKey.Redo
        enabled: window.canRedo()
        onTriggered: window.focusedTextEditor().redo()
    }

    Action {
        id: copyAction
        text: "&Copy"
        shortcut: StandardKey.Copy
        enabled: window.canCopy()
        onTriggered: window.focusedTextEditor().copy()
    }

    Action {
        id: pasteAction
        text: "&Paste"
        shortcut: StandardKey.Paste
        enabled: window.canPaste()
        onTriggered: window.focusedTextEditor().paste()
    }

    Action {
        id: selectAllAction
        text: "Select &All"
        shortcut: StandardKey.SelectAll
        enabled: window.focusedTextEditor() !== null
        onTriggered: window.focusedTextEditor().selectAll()
    }

    Action {
        id: toggleLogWindowAction
        text: window.logWindowObject && window.logWindowObject.visible
            ? "Hide logs"
            : "Show logs"
        shortcut: Qt.platform.os === "osx" ? "Meta+Shift+L" : "Ctrl+Shift+L"
        onTriggered: window.toggleLogWindow()
    }

    Action {
        id: openScanTableAction
        text: "Open Scan Table..."
        onTriggered: scanTableDialog.open()
    }

    Action {
        id: scanTuningsAction
        text: "Scan Tuning for Channels"
        enabled: !window.controller.userDeviceBusy
            && window.controller.tuningController.tunings.length > 0
        onTriggered: window.controller.tuningController.scan()
    }

    Action {
        id: toggleServerAction
        text: window.controller.serverController.serverState === "Running" ? "Stop Server" : "Start Server"
        shortcut: Qt.platform.os === "osx" ? "Meta+Alt+R" : "Ctrl+Alt+R"
        enabled: (window.controller.serverController.serverState === "Stopped"
                  || window.controller.serverController.serverState === "Running")
                 && !window.controller.userDeviceBusy
        onTriggered: {
            if (window.controller.serverController.serverState === "Running")
                window.controller.serverController.stopServer()
            else
                window.controller.serverController.startServer()
        }
    }

    Shortcut {
        sequence: "Escape"
        enabled: window.visibility === Window.FullScreen
        onActivated: window.exitFullScreen()
    }

    menuBar: MenuBar {
        Menu {
            title: "&File"
            MenuItem {
                action: openProfileAction
            }
            MenuSeparator {}
            MenuItem {
                action: saveProfileAction
            }
            MenuItem {
                action: quitAction
            }
        }
        Menu {
            title: "&Edit"
            MenuItem {
                action: undoAction
            }
            MenuItem {
                action: redoAction
            }
            MenuSeparator {}
            MenuItem {
                action: cutAction
            }
            MenuItem {
                action: copyAction
            }
            MenuItem {
                action: pasteAction
            }
            MenuSeparator {}
            MenuItem {
                action: selectAllAction
            }
        }
        Menu {
            title: "&Configure"
            MenuItem {
                text: "Auto-discover and initialize devices"
                checkable: true
                checked: window.controller.devicesController.autoDiscoverAndInitializeDevices
                onTriggered: window.controller.devicesController.autoDiscoverAndInitializeDevices = checked
            }
            MenuItem {
                text: "Show unsupported USB devices"
                checkable: true
                checked: window.controller.devicesController.showUnsupportedDevices
                onTriggered: window.controller.devicesController.showUnsupportedDevices = checked
            }
            MenuSeparator {}
            MenuItem {
                action: openScanTableAction
            }
            MenuSeparator {}
            MenuItem {
                action: scanTuningsAction
            }
            MenuSeparator {}
            MenuItem {
                action: toggleServerAction
            }
        }
        Menu {
            title: "&Window"
            MenuItem {
                action: toggleLogWindowAction
            }
        }
    }

    Connections {
        target: window.controller
        function onShutdownFinished() {
            Qt.quit()
        }
        function onChannelsChanged() {
            if (window.selectedChannelId && !window.channelExists(window.selectedChannelId))
                window.selectedChannelId = ""
        }
    }

    header: ToolBar {
        visible: !window.playerOnlyMode
        contentHeight: 56
        background: Rectangle { color: "#fbfcfa" }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 22
            anchors.rightMargin: 18
            spacing: 14

            Label {
                text: "PyDVB"
                font.pixelSize: 20
                font.bold: true
                color: "#17251e"
            }
            Rectangle { width: 1; height: 22; color: "#d5ddd7" }
            Label {
                text: "Digital TV receiver and streaming server"
                font.pixelSize: 14
                color: "#526158"
            }
            Item { Layout.fillWidth: true }
            BusyIndicator {
                running: window.controller.busy
                visible: running
                implicitWidth: 24
                implicitHeight: 24
            }
            Label {
                text: window.controller.statusMessage
                color: "#526158"
                elide: Text.ElideRight
                // Layout.maximumWidth: 330
            }
        }
    }

    RowLayout {
        id: mainLayout
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.preferredWidth: window.playerOnlyMode ? 0 : 218
            Layout.fillHeight: true
            z: window.playerOnlyMode && window.playerSidebarRevealed ? 1 : 0

            Rectangle {
                id: playerSidebar
                visible: !window.playerOnlyMode || window.playerSidebarRevealed
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 218
                color: "#e7ece7"

                HoverHandler {
                    id: playerSidebarHoverHandler
                    enabled: window.playerOnlyMode && window.playerSidebarRevealed
                    onHoveredChanged: {
                        if (hovered)
                            playerSidebarHideTimer.stop()
                        else
                            playerSidebarHideTimer.restart()
                    }
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.topMargin: 24
                    anchors.bottomMargin: 18
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 0

                    Label {
                        text: "CONFIGURE"
                        color: "#78857c"
                        font.pixelSize: 11
                        font.bold: true
                        Layout.leftMargin: 10
                        Layout.bottomMargin: 9
                    }

                    Repeater {
                        model: window.sectionNames
                        delegate: Button {
                            id: sectionButton
                            required property int index
                            required property string modelData
                            Layout.fillWidth: true
                            Layout.minimumHeight: 32
                            Layout.preferredHeight: 32
                            Layout.maximumHeight: 32
                            text: modelData
                            checkable: true
                            checked: window.sectionIndex === index
                            highlighted: checked
                            onClicked: {
                                window.sectionIndex = index
                                window.playerOnlyMode = false
                            }
                            horizontalPadding: 13
                            Accessible.description: window.sectionStatus(index)
                            ToolTip.visible: hovered
                            ToolTip.text: window.sectionStatus(index)
                            background: Rectangle {
                                radius: 5
                                color: sectionButton.checked ? "#d8e5da" : (sectionButton.hovered ? "#f0f3f0" : "transparent")
                                border.color: sectionButton.checked ? "#b8cdbd" : "transparent"
                            }
                            contentItem: RowLayout {
                                spacing: 10
                                Rectangle {
                                    Layout.preferredWidth: 10
                                    Layout.preferredHeight: 10
                                    radius: 5
                                    color: window.sectionReady(sectionButton.index) ? "#338457" : "#d7a63b"
                                }
                                Label {
                                    text: sectionButton.modelData
                                    Layout.fillWidth: true
                                    horizontalAlignment: Text.AlignLeft
                                    font.pixelSize: 14
                                    font.weight: sectionButton.checked ? Font.DemiBold : Font.Normal
                                    color: "#26362c"
                                }
                                ToolButton {
                                    id: sectionServerButton
                                    visible: sectionButton.index === 3
                                    Layout.preferredWidth: visible ? 28 : 0
                                    Layout.preferredHeight: visible ? 28 : 0
                                    enabled: toggleServerAction.enabled
                                    Accessible.name: toggleServerAction.text
                                    ToolTip.visible: hovered
                                    ToolTip.text: toggleServerAction.text
                                    onClicked: toggleServerAction.trigger()
                                    background: Rectangle {
                                        radius: 4
                                        color: sectionServerButton.hovered ? "#c8d8cb" : "transparent"
                                    }
                                    contentItem: Item {
                                        Image {
                                            width: 12
                                            height: 12
                                            anchors.centerIn: parent
                                            source: window.controller.serverController.serverState === "Running"
                                                ? "icons/square.svg"
                                                : "icons/play.svg"
                                            sourceSize.width: 12
                                            sourceSize.height: 12
                                            fillMode: Image.PreserveAspectFit
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Button {
                        id: playerSectionButton
                        Layout.fillWidth: true
                        Layout.minimumHeight: 32
                        Layout.preferredHeight: 32
                        Layout.maximumHeight: 32
                        text: "Player"
                        checkable: true
                        checked: window.sectionIndex === 4
                        highlighted: checked
                        onClicked: window.sectionIndex = 4
                        horizontalPadding: 13
                        Accessible.description: window.sectionStatus(4)
                        ToolTip.visible: hovered
                        ToolTip.text: window.sectionStatus(4)
                        background: Rectangle {
                            radius: 5
                            color: playerSectionButton.checked
                                ? "#d8e5da"
                                : (playerSectionButton.hovered ? "#f0f3f0" : "transparent")
                            border.color: playerSectionButton.checked ? "#b8cdbd" : "transparent"
                        }
                        contentItem: RowLayout {
                            spacing: 1
                            Rectangle {
                                Layout.preferredWidth: 10
                                Layout.preferredHeight: 10
                                Layout.rightMargin: 9
                                radius: 5
                                color: window.sectionReady(4) ? "#338457" : "#d7a63b"
                            }
                            Label {
                                text: "Player"
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignLeft
                                font.pixelSize: 14
                                font.weight: playerSectionButton.checked ? Font.DemiBold : Font.Normal
                                color: "#26362c"
                            }
                            ToolButton {
                                id: playerToggleButton
                                Layout.preferredWidth: 28
                                Layout.preferredHeight: 28
                                Layout.leftMargin: 9
                                enabled: playerLoader.item
                                    && playerLoader.item.canTogglePlayback
                                Accessible.name: playerLoader.item && playerLoader.item.isPlaying
                                    ? "Pause playback"
                                    : "Play playback"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onClicked: playerLoader.item.togglePlayback()
                                background: Rectangle {
                                    radius: 4
                                    color: playerToggleButton.hovered ? "#c8d8cb" : "transparent"
                                }
                                contentItem: Item {
                                    Image {
                                        width: 12
                                        height: 12
                                        anchors.centerIn: parent
                                        source: playerLoader.item && playerLoader.item.isPlaying
                                            ? "icons/pause.svg"
                                            : "icons/play.svg"
                                        sourceSize.width: 12
                                        sourceSize.height: 12
                                        fillMode: Image.PreserveAspectFit
                                    }
                                }
                            }
                            ToolButton {
                                id: playerStopButton
                                Layout.preferredWidth: 28
                                Layout.preferredHeight: 28
                                enabled: playerLoader.item
                                    && playerLoader.item.canStopPlayback
                                Accessible.name: "Stop playback"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onClicked: playerLoader.item.stopPlayback()
                                background: Rectangle {
                                    radius: 4
                                    color: playerStopButton.hovered ? "#c8d8cb" : "transparent"
                                }
                                contentItem: Item {
                                    Image {
                                        width: 12
                                        height: 12
                                        anchors.centerIn: parent
                                        source: "icons/square.svg"
                                        sourceSize.width: 12
                                        sourceSize.height: 12
                                        fillMode: Image.PreserveAspectFit
                                    }
                                }
                            }
                        }
                    }

                    Label {
                        text: "WATCH"
                        color: "#78857c"
                        font.pixelSize: 11
                        font.bold: true
                        Layout.leftMargin: 10
                        Layout.topMargin: 15
                    }
                    ListView {
                        id: playerChannelList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: window.controller.channelController.channels
                        delegate: ItemDelegate {
                            id: playerChannelDelegate
                            required property var modelData
                            width: playerChannelList.width
                            height: 28
                            enabled: modelData.id !== "Unknown"
                            onClicked: {
                                window.selectedChannelId = modelData.id
                                window.sectionIndex = 4
                            }
                            background: Rectangle {
                                radius: 5
                                color: window.selectedChannelId === playerChannelDelegate.modelData.id
                                    ? "#d8e5da"
                                    : (playerChannelDelegate.hovered ? "#f0f3f0" : "transparent")
                            }
                            contentItem: RowLayout {
                                spacing: 10
                                Label {
                                    text: playerChannelDelegate.modelData.lcn === null ? "-" : playerChannelDelegate.modelData.lcn
                                    Layout.preferredWidth: 27
                                    color: "#758178"
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignRight
                                }
                                Label {
                                    text: playerChannelDelegate.modelData.name
                                    Layout.fillWidth: true
                                    color: "#26362c"
                                    elide: Text.ElideRight
                                }
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: playerChannelList.count === 0
                            text: "No channels loaded"
                            color: "#849087"
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }

        Rectangle {
            visible: !window.playerOnlyMode
            Layout.fillHeight: true
            width: 1
            color: "#d9dfda"
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: currentIndex === 4 ? 0 : 26
            currentIndex: window.playerOnlyMode ? 4 : window.sectionIndex

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 3
                            Label { text: "Devices"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: "USB receivers and configured adapters"; color: "#68766d" }
                        }
                        Item { Layout.fillWidth: true }
                        ToolButton {
                            id: discoverButton
                            width: 38
                            height: 38
                            enabled: !window.controller.userDeviceBusy
                            text: "↻"
                            Accessible.name: "Refresh"
                            ToolTip.visible: hovered
                            ToolTip.text: "Refresh"
                            onClicked: window.controller.devicesController.discover()
                            background: Rectangle {
                                radius: 5
                                color: discoverButton.hovered ? "#e4eee6" : "transparent"
                            }
                            contentItem: Label {
                                text: discoverButton.text
                                color: "#26362c"
                                font.pixelSize: 24
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 8
                        spacing: 0
                        ResizableHeaderCell {
                            title: "DEVICE"
                            columnWidth: window.deviceNameColumnWidth
                            minimumColumnWidth: 130
                            onResized: window.deviceNameColumnWidth = newWidth
                        }
                        ResizableHeaderCell {
                            title: "STATE"
                            columnWidth: window.deviceStateColumnWidth
                            minimumColumnWidth: 70
                            onResized: window.deviceStateColumnWidth = newWidth
                        }
                        Label {
                            text: "SIGNAL"
                            Layout.fillWidth: true
                            color: "#77847b"
                            font.pixelSize: 11
                            font.bold: true
                            verticalAlignment: Text.AlignVCenter
                        }
                        Item { Layout.preferredWidth: 36 }
                    }
                    ListView {
                        id: deviceList
                        Layout.fillWidth: true
                        Layout.preferredHeight: 270
                        Layout.minimumHeight: 160
                        clip: true
                        model: window.controller.devicesController.devices
                        delegate: ItemDelegate {
                            id: deviceDelegate
                            required property var modelData
                            width: deviceList.width
                            height: 76
                            leftPadding: 8
                            rightPadding: 8
                            topPadding: 0
                            bottomPadding: 0
                            onClicked: window.controller.devicesController.selectDevice(modelData.deviceIndex)
                            background: Rectangle {
                                color: deviceDelegate.modelData.selected ? "#e4eee6" : (deviceDelegate.hovered ? "#f8faf8" : "transparent")
                                border.color: deviceDelegate.modelData.selected ? "#b8cdbd" : "transparent"
                                radius: 5
                            }
                            contentItem: RowLayout {
                                spacing: 0
                                ColumnLayout {
                                    Layout.fillWidth: false
                                    Layout.preferredWidth: window.deviceNameColumnWidth
                                    Layout.minimumWidth: 130
                                    Label { text: deviceDelegate.modelData.name; font.bold: true; color: "#26362c"; elide: Text.ElideRight }
                                    Label { text: deviceDelegate.modelData.deviceId; color: "#758178"; font.pixelSize: 12 }
                                }
                                Label {
                                    text: deviceDelegate.modelData.state
                                    Layout.fillWidth: false
                                    Layout.preferredWidth: window.deviceStateColumnWidth
                                    Layout.minimumWidth: 70
                                    color: "#4e5e53"
                                    elide: Text.ElideRight
                                }
                                Label { text: deviceDelegate.modelData.signal; Layout.fillWidth: true; color: "#4e5e53"; elide: Text.ElideRight }
                                ToolButton {
                                    id: initializeDeviceButton
                                    visible: deviceDelegate.modelData.deviceIndex >= 0
                                    Layout.preferredWidth: visible ? 36 : 0
                                    Layout.preferredHeight: 36
                                    enabled: visible
                                        && !deviceDelegate.modelData.initializing
                                        && !window.controller.userDeviceBusy
                                    text: "⏻"
                                    Accessible.name: "Initialize"
                                    ToolTip.visible: hovered
                                    ToolTip.text: "Initialize"
                                    onClicked: window.controller.devicesController.initializeDevice(
                                        deviceDelegate.modelData.deviceIndex
                                    )
                                    background: Rectangle {
                                        radius: 5
                                        color: initializeDeviceButton.hovered ? "#e4eee6" : "transparent"
                                    }
                                    contentItem: Label {
                                        text: initializeDeviceButton.text
                                        color: "#26362c"
                                        font.pixelSize: 20
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                }
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: deviceList.count === 0
                            text: "No devices discovered"
                            color: "#849087"
                        }
                    }
                    // Label { text: "DEVICE ADAPTERS"; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 8
                        spacing: 0
                        ResizableHeaderCell {
                            title: "ADAPTER"
                            columnWidth: window.adapterNameColumnWidth
                            minimumColumnWidth: 130
                            onResized: window.adapterNameColumnWidth = newWidth
                        }
                        ResizableHeaderCell {
                            title: "STATE"
                            columnWidth: window.adapterStateColumnWidth
                            minimumColumnWidth: 90
                            onResized: window.adapterStateColumnWidth = newWidth
                        }
                        Label {
                            text: "SIGNAL"
                            Layout.fillWidth: true
                            color: "#77847b"
                            font.pixelSize: 11
                            font.bold: true
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                    ListView {
                        id: adapterList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 90
                        clip: true
                        model: window.controller.devicesController.adapters
                        delegate: ItemDelegate {
                            id: adapterDelegate
                            required property var modelData
                            width: adapterList.width
                            height: 54
                            leftPadding: 8
                            rightPadding: 8
                            topPadding: 0
                            bottomPadding: 0
                            onClicked: window.controller.devicesController.selectAdapter(modelData.adapterIndex)
                            highlighted: modelData.selected
                            contentItem: RowLayout {
                                spacing: 0
                                Label {
                                    text: adapterDelegate.modelData.name
                                    Layout.fillWidth: false
                                    Layout.preferredWidth: window.adapterNameColumnWidth
                                    Layout.minimumWidth: 130
                                    color: "#26362c"
                                    elide: Text.ElideRight
                                }
                                Label {
                                    text: adapterDelegate.modelData.state
                                    Layout.fillWidth: false
                                    Layout.preferredWidth: window.adapterStateColumnWidth
                                    Layout.minimumWidth: 90
                                    color: "#26362c"
                                    elide: Text.ElideRight
                                }
                                Label {
                                    text: adapterDelegate.modelData.signal
                                    Layout.fillWidth: true
                                    color: "#26362c"
                                    elide: Text.ElideRight
                                }
                            }
                            background: Rectangle {
                                color: adapterDelegate.modelData.selected ? "#e4eee6" : (adapterDelegate.hovered ? "#f8faf8" : "transparent")
                                border.color: adapterDelegate.modelData.selected ? "#b8cdbd" : "transparent"
                                radius: 5
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: adapterList.count === 0
                            text: "Initialize a device to configure its adapters"
                            color: "#849087"
                        }
                    }

                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 3
                            Label { text: "Tuning"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: "Choose a scan table or edit individual frequencies"; color: "#68766d" }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    Label { text: "Known Tuning Lists (Scan Tables)"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: tableBox
                            Layout.fillWidth: true
                            model: window.controller.tuningController.knownTables
                            editable: true
                            displayText: currentText || "Select a known scan table"
                            onActivated: window.controller.tuningController.selectScanTable(currentText)
                            onAccepted: window.controller.tuningController.selectScanTable(editText)
                        }
                        ToolButton {
                            icon.source: "icons/refresh-cw.svg"
                            icon.width: 18
                            icon.height: 18
                            icon.color: "#26362c"
                            Accessible.name: "Refresh list"
                            ToolTip.visible: hovered
                            ToolTip.text: Accessible.name
                            Layout.preferredWidth: 36
                            Layout.preferredHeight: 36
                            Layout.alignment: Qt.AlignVCenter
                            enabled: !window.controller.generalBusy
                            onClicked: window.controller.tuningController.refreshKnownTables()
                        }
                        ToolButton {
                            icon.source: "icons/folder-open.svg"
                            icon.width: 18
                            icon.height: 18
                            icon.color: "#26362c"
                            Accessible.name: "Open Scan Table..."
                            ToolTip.visible: hovered
                            ToolTip.text: Accessible.name
                            Layout.preferredWidth: 36
                            Layout.preferredHeight: 36
                            Layout.alignment: Qt.AlignVCenter
                            onClicked: scanTableDialog.open()
                        }
                    }
                    Label { text: "Manual Tuning List"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        ColumnLayout {
                            spacing: 2
                            Label {
                                id: frequencyLabel
                                text: "Frequency (MHz)"
                                color: "#68766d"
                                font.pixelSize: 12
                            }
                            TextField {
                                id: frequencyField
                                Layout.preferredWidth: 180
                                placeholderText: "Frequency"
                                validator: DoubleValidator { bottom: 1; decimals: 6 }
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                            }
                        }
                        ColumnLayout {
                            spacing: 2
                            Label {
                                id: bandwidthLabel
                                text: "Bandwidth (MHz)"
                                color: "#68766d"
                                font.pixelSize: 12
                            }
                            ComboBox {
                                id: bandwidthBox
                                model: ["6", "7", "8"]
                                currentIndex: 1
                                Layout.preferredWidth: 120
                            }
                        }
                        ColumnLayout {
                            spacing: 2
                            Layout.alignment: Qt.AlignVCenter
                            Item { Layout.preferredHeight: frequencyLabel.implicitHeight }
                            ToolButton {
                                icon.source: window.editingTuningIndex < 0
                                    ? "icons/plus.svg"
                                    : "icons/check.svg"
                                icon.width: 18
                                icon.height: 18
                                icon.color: "#26362c"
                                Accessible.name: window.editingTuningIndex < 0 ? "Add tuning" : "Save tuning"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                Layout.preferredWidth: 36
                                Layout.preferredHeight: 36
                                enabled: !window.controller.generalBusy
                                onClicked: {
                                    if (window.editingTuningIndex < 0)
                                        window.controller.tuningController.addTuning(frequencyField.text, bandwidthBox.currentText)
                                    else
                                        window.controller.tuningController.updateTuning(window.editingTuningIndex, frequencyField.text, bandwidthBox.currentText)
                                    frequencyField.clear()
                                    window.editingTuningIndex = -1
                                }
                            }
                        }
                        ColumnLayout {
                            spacing: 2
                            Layout.alignment: Qt.AlignVCenter
                            visible: window.editingTuningIndex >= 0
                            Item { Layout.preferredHeight: bandwidthLabel.implicitHeight }
                            ToolButton {
                                icon.source: "icons/x.svg"
                                icon.width: 18
                                icon.height: 18
                                icon.color: "#26362c"
                                Accessible.name: "Cancel edit"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                Layout.preferredWidth: 36
                                Layout.preferredHeight: 36
                                onClicked: {
                                    frequencyField.clear()
                                    window.editingTuningIndex = -1
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                        Label { text: window.controller.tuningController.tunings.length + " tuning(s)"; color: "#68766d" }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 8
                        spacing: 0
                        ResizableHeaderCell {
                            title: "ITEM"
                            columnWidth: window.tuningIndexColumnWidth
                            minimumColumnWidth: 40
                            onResized: window.tuningIndexColumnWidth = newWidth
                        }
                        ResizableHeaderCell {
                            title: "FREQUENCY"
                            columnWidth: window.tuningFrequencyColumnWidth
                            minimumColumnWidth: 130
                            onResized: window.tuningFrequencyColumnWidth = newWidth
                        }
                        ResizableHeaderCell {
                            title: "BANDWIDTH"
                            columnWidth: window.tuningBandwidthColumnWidth
                            minimumColumnWidth: 90
                            onResized: window.tuningBandwidthColumnWidth = newWidth
                        }
                        Label {
                            text: "ACTIONS"
                            Layout.fillWidth: true
                            color: "#77847b"
                            font.pixelSize: 11
                            font.bold: true
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                    ListView {
                        id: tuningList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: window.controller.tuningController.tunings
                        delegate: ItemDelegate {
                            id: tuningDelegate
                            required property var modelData
                            width: tuningList.width
                            height: 48
                            leftPadding: 8
                            rightPadding: 8
                            topPadding: 0
                            bottomPadding: 0
                            background: Rectangle {
                                color: tuningDelegate.hovered ? "#f8faf8" : "transparent"
                            }
                            contentItem: RowLayout {
                                spacing: 0
                                Label {
                                    text: String(tuningDelegate.modelData.index + 1).padStart(2, "0")
                                    color: "#829087"
                                    Layout.preferredWidth: window.tuningIndexColumnWidth
                                    Layout.minimumWidth: 40
                                }
                                Label {
                                    text: Number(tuningDelegate.modelData.frequency).toFixed(3) + " MHz"
                                    color: "#26362c"
                                    Layout.preferredWidth: window.tuningFrequencyColumnWidth
                                    Layout.minimumWidth: 130
                                }
                                Label {
                                    text: tuningDelegate.modelData.bandwidth + " MHz"
                                    color: "#526158"
                                    Layout.preferredWidth: window.tuningBandwidthColumnWidth
                                    Layout.minimumWidth: 90
                                }
                                ToolButton {
                                    icon.source: "icons/pencil.svg"
                                    icon.width: 18
                                    icon.height: 18
                                    icon.color: "#26362c"
                                    Accessible.name: "Edit tuning"
                                    ToolTip.visible: hovered
                                    ToolTip.text: Accessible.name
                                    Layout.preferredWidth: 36
                                    Layout.minimumWidth: 36
                                    enabled: !window.controller.generalBusy
                                    onClicked: {
                                        window.editingTuningIndex = tuningDelegate.modelData.index
                                        frequencyField.text = Number(tuningDelegate.modelData.frequency).toFixed(3)
                                        bandwidthBox.currentIndex = Math.max(0, ["6", "7", "8"].indexOf(String(tuningDelegate.modelData.bandwidth)))
                                    }
                                }
                                ToolButton {
                                    icon.source: "icons/x.svg"
                                    icon.width: 18
                                    icon.height: 18
                                    icon.color: "#26362c"
                                    Accessible.name: "Remove tuning"
                                    ToolTip.visible: hovered
                                    ToolTip.text: Accessible.name
                                    Layout.preferredWidth: 36
                                    Layout.minimumWidth: 36
                                    onClicked: window.controller.tuningController.removeTuning(tuningDelegate.modelData.index)
                                }
                                Item { Layout.fillWidth: true }
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: tuningList.count === 0
                            text: "No tunings configured"
                            color: "#849087"
                        }
                    }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 3
                            Label { text: "Channels"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: window.controller.channelController.channels.length + " service(s) in the current map"; color: "#68766d" }
                        }
                        Item { Layout.fillWidth: true }
                        Button { action: scanTuningsAction }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 0
                        spacing: 4
                        Label { text: "Station Streams"; font.pixelSize: 14; font.bold: true; color: "#26362c" }
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "NETWORK NAME"; Layout.preferredWidth: 180; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "DVB ID"; Layout.preferredWidth: 120; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "CHANNELS"; Layout.preferredWidth: 80; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "FREQUENCY"; Layout.preferredWidth: 130; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "BANDWIDTH"; Layout.fillWidth: true; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        }
                        ListView {
                            id: stationStreamList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 0
                            clip: true
                            model: window.controller.channelController.stationStreams
                            delegate: RowLayout {
                                id: stationStreamDelegate
                                required property var modelData
                                width: stationStreamList.width
                                height: 32
                                Label { text: stationStreamDelegate.modelData.networkName; Layout.preferredWidth: 180; color: "#26362c"; elide: Text.ElideRight }
                                Label { text: stationStreamDelegate.modelData.dvbId; Layout.preferredWidth: 120; color: "#758178"; font.pixelSize: 12 }
                                Label { text: stationStreamDelegate.modelData.channels; Layout.preferredWidth: 80; color: "#526158"; horizontalAlignment: Text.AlignHCenter }
                                Label { text: stationStreamDelegate.modelData.frequency; Layout.preferredWidth: 130; color: "#526158" }
                                Label {
                                    text: stationStreamDelegate.modelData.bandwidth
                                    Layout.fillWidth: true
                                    color: "#526158"
                                }
                            }
                            Label {
                                anchors.centerIn: parent
                                visible: stationStreamList.count === 0
                                text: "No streams scanned or loaded"
                                color: "#849087"
                            }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 0
                        spacing: 4
                        Label { text: "Services"; font.pixelSize: 14; font.bold: true; color: "#26362c" }
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: "LCN"
                                Layout.preferredWidth: 25
                                Layout.rightMargin: 10
                                color: "#77847b"
                                font.pixelSize: 11
                                font.bold: true
                                horizontalAlignment: Text.AlignLeft
                                ToolTip.visible: lcnHeaderHover.hovered
                                ToolTip.text: "Logical Channel Number"
                                HoverHandler { id: lcnHeaderHover }
                            }
                            Label { text: "CHANNEL"; Layout.preferredWidth: 180; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "DVB ID"; Layout.preferredWidth: 150; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                            Label { text: "TYPE"; Layout.fillWidth: true; color: "#77847b"; font.pixelSize: 11; font.bold: true; horizontalAlignment: Text.AlignLeft }
                        }
                        ListView {
                            id: channelList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 0
                            clip: true
                            model: window.controller.channelController.channels
                            delegate: RowLayout {
                                id: channelDelegate
                                required property var modelData
                                width: channelList.width
                                height: 32
                                Label {
                                    text: channelDelegate.modelData.lcn === null ? "-" : channelDelegate.modelData.lcn
                                    Layout.preferredWidth: 25
                                    Layout.rightMargin: 10
                                    color: "#526158"
                                    horizontalAlignment: Text.AlignRight
                                }
                                Label { text: channelDelegate.modelData.name; Layout.preferredWidth: 180; color: "#26362c"; elide: Text.ElideRight }
                                Label { text: channelDelegate.modelData.id; Layout.preferredWidth: 150; color: "#758178"; font.pixelSize: 12 }
                                Label {
                                    text: channelDelegate.modelData.type
                                    Layout.fillWidth: true
                                    color: "#526158"
                                    horizontalAlignment: Text.AlignLeft
                                }
                            }
                            Label {
                                anchors.centerIn: parent
                                visible: channelList.count === 0
                                text: "No services scanned or loaded"
                                color: "#849087"
                            }
                        }
                    }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 3
                            Label { text: "Server"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: "Live MPEG transport streams over HTTP"; color: "#68766d" }
                        }
                        Item { Layout.fillWidth: true }
                        Rectangle {
                            width: 10; height: 10; radius: 5
                            color: window.controller.serverController.serverState === "Running" ? "#338457" : "#a4aaa5"
                        }
                        Label { text: window.controller.serverController.serverState; color: "#526158" }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    Label { text: "Listener"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: hostField
                            Layout.fillWidth: true
                            text: window.controller.serverController.serverHost
                            placeholderText: "Bind address"
                            enabled: window.controller.serverController.serverState !== "Running"
                            onEditingFinished: window.controller.serverController.serverHost = text
                        }
                        TextField {
                            id: portField
                            Layout.preferredWidth: 130
                            text: window.controller.serverController.serverPort.toString()
                            placeholderText: "Port"
                            validator: IntValidator { bottom: 0; top: 65535 }
                            enabled: window.controller.serverController.serverState !== "Running"
                            onEditingFinished: {
                                if (acceptableInput)
                                    window.controller.serverController.serverPort = Number(text)
                            }
                        }
                        Button {
                            text: "Start"
                            highlighted: true
                            enabled: window.controller.serverController.serverState === "Stopped" && !window.controller.userDeviceBusy
                            onClicked: window.controller.serverController.startServer()
                        }
                        Button {
                            text: "Stop"
                            enabled: window.controller.serverController.serverState === "Running" && !window.controller.userDeviceBusy
                            onClicked: window.controller.serverController.stopServer()
                        }
                    }
                    Label { text: "Playlist URL"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    TextField {
                        Layout.fillWidth: true
                        readOnly: true
                        text: window.controller.serverController.playlistUrl || "Start the server to expose the playlist"
                        selectByMouse: true
                    }
                    Label { text: "Adapter signal"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    Label { text: window.controller.serverController.serverSignalSummary; color: "#526158"; wrapMode: Text.Wrap }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "CONNECTED CLIENTS"; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label { text: window.controller.serverController.serverClients.length; color: "#77847b"; font.pixelSize: 11 }
                    }
                    ListView {
                        id: clientList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: window.controller.serverController.serverClients
                        delegate: RowLayout {
                            id: clientDelegate
                            required property var modelData
                            width: clientList.width
                            height: 48
                            Label { text: clientDelegate.modelData.service; Layout.fillWidth: true; color: "#26362c"; elide: Text.ElideRight }
                            Label { text: clientDelegate.modelData.serviceId; Layout.preferredWidth: 150; color: "#758178" }
                            Label { text: clientDelegate.modelData.peer; Layout.preferredWidth: 170; color: "#526158" }
                            Label { text: Number(clientDelegate.modelData.bytesSent).toLocaleString() + " B"; Layout.preferredWidth: 110; color: "#526158" }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: clientList.count === 0
                            text: "No connected clients"
                            color: "#849087"
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "Binding beyond localhost makes streams reachable on your network."
                        color: "#947044"
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                }
            }

            Item {
                Loader {
                    id: playerLoader
                    property bool playbackAvailable: window.playerAvailable
                    property bool playerOnlyMode: window.playerOnlyMode
                    property bool fullScreenMode: window.visibility === Window.FullScreen
                    anchors.fill: parent
                    active: playbackAvailable
                    source: active ? "Player.qml" : ""
                    onLoaded: {
                        item.appController = window.controller
                        item.selectedChannelId = window.selectedChannelId
                        item.playerOnlyMode = playerOnlyMode
                        item.fullScreenMode = fullScreenMode
                    }
                    onPlayerOnlyModeChanged: {
                        if (item)
                            item.playerOnlyMode = playerOnlyMode
                    }
                    onFullScreenModeChanged: {
                        if (item)
                            item.fullScreenMode = fullScreenMode
                    }
                }
                Item {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: 18

                    HoverHandler {
                        enabled: window.playerOnlyMode
                        onHoveredChanged: {
                            if (hovered) {
                                window.playerSidebarRevealed = true
                                playerSidebarHideTimer.stop()
                            } else if (window.playerSidebarRevealed) {
                                playerSidebarHideTimer.restart()
                            }
                        }
                    }
                }
                Connections {
                    target: playerLoader.item
                    function onChannelSelectionRequested(channelId) {
                        window.selectedChannelId = channelId
                    }
                    function onPlayerOnlyModeToggleRequested() {
                        window.playerOnlyMode = !window.playerOnlyMode
                    }
                    function onFullScreenRequested() {
                        if (window.visibility === Window.FullScreen)
                            window.exitFullScreen()
                        else
                            window.enterPlayerFullScreen()
                    }
                }
                ColumnLayout {
                    anchors.centerIn: parent
                    visible: !playerLoader.playbackAvailable
                    spacing: 10
                    Label {
                        text: "Player"
                        font.pixelSize: 26
                        font.bold: true
                        color: "#17251e"
                    }
                    Label {
                        text: "Install the optional playback dependencies and restart the app:"
                        color: "#68766d"
                        wrapMode: Text.Wrap
                    }
                    Label {
                        text: "python3 -m pip install -e '.[player]'"
                        color: "#26362c"
                        font.family: "monospace"
                        wrapMode: Text.Wrap
                    }
                }

                Timer {
                    id: playerSidebarHideTimer
                    interval: 1200
                    onTriggered: {
                        if (window.playerOnlyMode && !playerSidebarHoverHandler.hovered)
                            window.playerSidebarRevealed = false
                    }
                }
            }
        }
    }

    FileDialog {
        id: openProfileDialog
        title: "Open PyDVB profile"
        fileMode: FileDialog.OpenFile
        nameFilters: ["PyDVB profile (*.json)", "All files (*)"]
        onAccepted: window.controller.loadProfile(selectedFile.toString())
    }

    FileDialog {
        id: saveProfileDialog
        title: "Save PyDVB profile"
        fileMode: FileDialog.SaveFile
        nameFilters: ["PyDVB profile (*.json)", "All files (*)"]
        onAccepted: window.controller.saveProfile(selectedFile.toString())
    }

    FileDialog {
        id: scanTableDialog
        title: "Open DVB-T scan table"
        fileMode: FileDialog.OpenFile
        onAccepted: window.controller.tuningController.loadLocalScanTable(selectedFile.toString())
    }

    Component {
        id: logWindowComponent

        Window {
            id: logWindow
            required property var appController
            transientParent: window
            visible: false
            width: 900
            height: 480
            minimumWidth: 560
            minimumHeight: 300
            title: "PyDVB Logs"
            color: "#f2f4f1"

            onClosing: function(closeEvent) {
                closeEvent.accepted = false
                visible = false
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        text: logWindow.appController.logController.logMessages.length + " recent log message(s)"
                        color: "#526158"
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Clear"
                        onClicked: logWindow.appController.logController.clearLogs()
                    }
                    Button {
                        text: "Close"
                        onClicked: window.logWindowObject.visible = false
                    }
                }

                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    TextArea {
                        text: logWindow.appController.logController.logMessages.join("\n")
                        readOnly: true
                        selectByMouse: true
                        textFormat: TextEdit.PlainText
                        wrapMode: TextEdit.NoWrap
                        font.family: "monospace"
                        font.pixelSize: 12
                        onTextChanged: cursorPosition = length
                    }
                }
            }
        }
    }
}