import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: window
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
    readonly property var sectionNames: ["Devices", "Tuning", "TV Stations", "Server"]

    onSectionIndexChanged: {
        if (sectionIndex === 1 && !scanTablesRefreshAttempted) {
            scanTablesRefreshAttempted = true
            controller.tuningController.refreshKnownTables()
        }
    }

    function toggleLogWindow() {
        if (logWindowObject === null) {
            logWindowObject = logWindowComponent.createObject(window, {
                appController: controller
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
            return controller.devicesController.deviceSummary
        case 1:
            return controller.tuningController.tunings.length > 0
                ? controller.tuningController.tunings.length + " tuning(s) loaded"
                : "No tunings loaded"
        case 2:
            return controller.channelController.hasChannelMap ? "A channel map is active" : "No channel map loaded"
        case 3:
            return controller.serverController.serverState === "Running" ? "Server is running" : "Server is not running"
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
            return controller.devicesController.devices.some(function(device) { return device.adapterCount > 0 })
        case 1:
            return controller.tuningController.tunings.length > 0
        case 2:
            return controller.channelController.hasChannelMap
        case 3:
            return controller.serverController.serverState === "Running"
        case 4:
            return playerLoader.playbackAvailable && window.selectedChannelId.length > 0
        default:
            return false
        }
    }

    function channelExists(channelId) {
        return controller.channelController.channels.some(function(channel) {
            return channel.id === channelId
        })
    }

    onSelectedChannelIdChanged: {
        if (playerLoader.item)
            playerLoader.item.selectedChannelId = selectedChannelId
    }

    onClosing: function(closeEvent) {
        if (!controller.shuttingDown) {
            closeEvent.accepted = false
            controller.shutdown()
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
        id: toggleLogWindowAction
        text: window.logWindowObject && window.logWindowObject.visible
            ? "Hide logs"
            : "Show logs"
        shortcut: Qt.platform.os === "osx" ? "Meta+Shift+L" : "Ctrl+Shift+L"
        onTriggered: window.toggleLogWindow()
    }

    Action {
        id: toggleServerAction
        text: controller.serverController.serverState === "Running" ? "Stop Server" : "Start Server"
        shortcut: Qt.platform.os === "osx" ? "Meta+Alt+R" : "Ctrl+Alt+R"
        enabled: (controller.serverController.serverState === "Stopped"
                  || controller.serverController.serverState === "Running")
                 && !controller.userDeviceBusy
        onTriggered: {
            if (controller.serverController.serverState === "Running")
                controller.serverController.stopServer()
            else
                controller.serverController.startServer()
        }
    }

    menuBar: MenuBar {
        Menu {
            title: "&File"
            MenuItem {
                action: openProfileAction
            }
            MenuItem {
                action: saveProfileAction
            }
        }
        Menu {
            title: "&Server"
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
        target: controller
        function onShutdownFinished() {
            Qt.quit()
        }
        function onChannelsChanged() {
            if (window.selectedChannelId && !window.channelExists(window.selectedChannelId))
                window.selectedChannelId = ""
        }
    }

    header: ToolBar {
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
                running: controller.busy
                visible: running
                implicitWidth: 24
                implicitHeight: 24
            }
            Label {
                text: controller.statusMessage
                color: "#526158"
                elide: Text.ElideRight
                // Layout.maximumWidth: 330
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.preferredWidth: 218
            Layout.fillHeight: true
            color: "#e7ece7"

            ColumnLayout {
                anchors.fill: parent
                anchors.topMargin: 24
                anchors.bottomMargin: 18
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 5

                Label {
                    text: "WORKSPACE"
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
                        text: modelData
                        checkable: true
                        checked: window.sectionIndex === index
                        highlighted: checked
                        onClicked: window.sectionIndex = index
                        horizontalPadding: 13
                        height: 46
                        Accessible.description: window.sectionStatus(index)
                        ToolTip.visible: hovered
                        ToolTip.text: window.sectionStatus(index)
                        background: Rectangle {
                            radius: 5
                            color: checked ? "#d8e5da" : (parent.hovered ? "#f0f3f0" : "transparent")
                            border.color: checked ? "#b8cdbd" : "transparent"
                        }
                        contentItem: RowLayout {
                            spacing: 10
                            Label {
                                text: modelData
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignLeft
                                font.pixelSize: 14
                                font.weight: sectionButton.checked ? Font.DemiBold : Font.Normal
                                color: "#26362c"
                            }
                            ToolButton {
                                visible: index === 3
                                Layout.preferredWidth: visible ? 28 : 0
                                Layout.preferredHeight: visible ? 28 : 0
                                enabled: toggleServerAction.enabled
                                text: controller.serverController.serverState === "Running" ? "■" : "▶"
                                Accessible.name: toggleServerAction.text
                                ToolTip.visible: hovered
                                ToolTip.text: toggleServerAction.text
                                onClicked: toggleServerAction.trigger()
                                background: Rectangle {
                                    radius: 4
                                    color: parent.hovered ? "#c8d8cb" : "transparent"
                                }
                                contentItem: Label {
                                    text: parent.text
                                    color: "#26362c"
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                            Rectangle {
                                Layout.preferredWidth: 10
                                Layout.preferredHeight: 10
                                radius: 5
                                color: window.sectionReady(index) ? "#338457" : "#d7a63b"
                            }
                        }
                    }
                }

                Button {
                    id: playerSectionButton
                    Layout.fillWidth: true
                    text: "Player"
                    checkable: true
                    checked: window.sectionIndex === 4
                    highlighted: checked
                    onClicked: window.sectionIndex = 4
                    horizontalPadding: 13
                    height: 46
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
                        spacing: 10
                        Label {
                            text: "Player"
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignLeft
                            font.pixelSize: 14
                            font.weight: playerSectionButton.checked ? Font.DemiBold : Font.Normal
                            color: "#26362c"
                        }
                        ToolButton {
                            Layout.preferredWidth: 28
                            Layout.preferredHeight: 28
                            enabled: playerLoader.item
                                && playerLoader.item.canTogglePlayback
                            text: playerLoader.item && playerLoader.item.isPlaying ? "Ⅱ" : "▶"
                            Accessible.name: playerLoader.item && playerLoader.item.isPlaying
                                ? "Pause playback"
                                : "Play playback"
                            ToolTip.visible: hovered
                            ToolTip.text: Accessible.name
                            onClicked: playerLoader.item.togglePlayback()
                            background: Rectangle {
                                radius: 4
                                color: parent.hovered ? "#c8d8cb" : "transparent"
                            }
                            contentItem: Label {
                                text: parent.text
                                color: "#26362c"
                                font.pixelSize: 12
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                        ToolButton {
                            Layout.preferredWidth: 28
                            Layout.preferredHeight: 28
                            enabled: playerLoader.item
                                && playerLoader.item.canStopPlayback
                            text: "■"
                            Accessible.name: "Stop playback"
                            ToolTip.visible: hovered
                            ToolTip.text: Accessible.name
                            onClicked: playerLoader.item.stopPlayback()
                            background: Rectangle {
                                radius: 4
                                color: parent.hovered ? "#c8d8cb" : "transparent"
                            }
                            contentItem: Label {
                                text: parent.text
                                color: "#26362c"
                                font.pixelSize: 12
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                        Rectangle {
                            Layout.preferredWidth: 10
                            Layout.preferredHeight: 10
                            radius: 5
                            color: window.sectionReady(4) ? "#338457" : "#d7a63b"
                        }
                    }
                }

                Label {
                    text: "CHANNELS"
                    color: "#78857c"
                    font.pixelSize: 11
                    font.bold: true
                    Layout.leftMargin: 10
                    Layout.topMargin: 8
                }
                ListView {
                    id: playerChannelList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: controller.channelController.channels
                    delegate: ItemDelegate {
                        required property var modelData
                        width: playerChannelList.width
                        height: 42
                        enabled: modelData.id !== "Unknown"
                        onClicked: {
                            window.selectedChannelId = modelData.id
                            window.sectionIndex = 4
                        }
                        background: Rectangle {
                            radius: 5
                            color: window.selectedChannelId === modelData.id
                                ? "#d8e5da"
                                : (parent.hovered ? "#f0f3f0" : "transparent")
                        }
                        contentItem: RowLayout {
                            spacing: 8
                            Label {
                                text: modelData.lcn === null ? "-" : modelData.lcn
                                Layout.preferredWidth: 27
                                color: "#758178"
                                font.pixelSize: 12
                            }
                            Label {
                                text: modelData.name
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

        Rectangle { Layout.fillHeight: true; width: 1; color: "#d9dfda" }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: window.sectionIndex === 4 ? 0 : 26
            currentIndex: window.sectionIndex

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 3
                            Label { text: "Devices"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: "USB receivers and configured adapters"; color: "#68766d" }
                        }
                        Item { Layout.fillWidth: true }
                        CheckBox {
                            text: "Auto-discover and initialize devices"
                            checked: controller.devicesController.autoDiscoverAndInitializeDevices
                            onToggled: controller.devicesController.autoDiscoverAndInitializeDevices = checked
                        }
                        CheckBox {
                            text: "Show unsupported USB devices"
                            checked: controller.devicesController.showUnsupportedDevices
                            onToggled: controller.devicesController.showUnsupportedDevices = checked
                        }
                        Button { text: "Discover"; enabled: !controller.userDeviceBusy; onClicked: controller.devicesController.discover() }
                        Button {
                            text: "Initialize selected"
                            enabled: !controller.userDeviceBusy && controller.devicesController.devices.length > 0
                            highlighted: true
                            onClicked: controller.devicesController.initializeSelectedDevice()
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "DEVICE"; Layout.preferredWidth: 240; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "STATE"; Layout.preferredWidth: 180; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "ADAPTERS"; Layout.preferredWidth: 100; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "SIGNAL"; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                    }
                    ListView {
                        id: deviceList
                        Layout.fillWidth: true
                        Layout.preferredHeight: 270
                        Layout.minimumHeight: 160
                        clip: true
                        model: controller.devicesController.devices
                        delegate: ItemDelegate {
                            required property var modelData
                            width: deviceList.width
                            height: 76
                            onClicked: controller.devicesController.selectDevice(modelData.deviceIndex)
                            background: Rectangle {
                                color: modelData.selected ? "#e4eee6" : (parent.hovered ? "#f8faf8" : "transparent")
                                border.color: modelData.selected ? "#b8cdbd" : "transparent"
                                radius: 5
                            }
                            contentItem: RowLayout {
                                spacing: 14
                                ColumnLayout {
                                    Layout.preferredWidth: 226
                                    Label { text: modelData.name; font.bold: true; color: "#26362c"; elide: Text.ElideRight }
                                    Label { text: modelData.deviceId; color: "#758178"; font.pixelSize: 12 }
                                }
                                Label { text: modelData.state; Layout.preferredWidth: 180; color: "#4e5e53"; elide: Text.ElideRight }
                                Label { text: modelData.adapterCount; Layout.preferredWidth: 100; color: "#4e5e53" }
                                Label { text: modelData.signal; Layout.fillWidth: true; color: "#4e5e53"; elide: Text.ElideRight }
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: deviceList.count === 0
                            text: "No devices discovered"
                            color: "#849087"
                        }
                    }
                    Label { text: "ADAPTERS"; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                    ListView {
                        id: adapterList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 90
                        clip: true
                        model: controller.devicesController.adapters
                        delegate: ItemDelegate {
                            required property var modelData
                            width: adapterList.width
                            height: 54
                            onClicked: controller.devicesController.selectAdapter(modelData.adapterIndex)
                            highlighted: modelData.selected
                            contentItem: Label {
                                text: modelData.name + "     " + modelData.state + "     " + modelData.signal
                                color: "#26362c"
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                color: modelData.selected ? "#e4eee6" : (parent.hovered ? "#f8faf8" : "transparent")
                                border.color: modelData.selected ? "#b8cdbd" : "transparent"
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
                        Item { Layout.fillWidth: true }
                        Button {
                            text: "Scan services"
                            highlighted: true
                            enabled: !controller.userDeviceBusy && controller.tuningController.tunings.length > 0
                            onClicked: controller.tuningController.scan()
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: tableBox
                            Layout.fillWidth: true
                            model: controller.tuningController.knownTables
                            editable: true
                            displayText: currentText || "Select a known scan table"
                            onActivated: controller.tuningController.selectScanTable(currentText)
                            onAccepted: controller.tuningController.selectScanTable(editText)
                        }
                        Button { text: "Refresh tables"; enabled: !controller.generalBusy; onClicked: controller.tuningController.refreshKnownTables() }
                        Button { text: "Open local table"; onClicked: scanTableDialog.open() }
                    }
                    Label { text: "Manual tuning"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: frequencyField
                            Layout.preferredWidth: 210
                            placeholderText: "Frequency in MHz"
                            validator: DoubleValidator { bottom: 1; decimals: 6 }
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                        }
                        ComboBox { id: bandwidthBox; model: ["6", "7", "8"]; currentIndex: 1; Layout.preferredWidth: 120 }
                        Button {
                            text: window.editingTuningIndex < 0 ? "Add tuning" : "Save tuning"
                            enabled: !controller.generalBusy
                            onClicked: {
                                if (window.editingTuningIndex < 0)
                                    controller.tuningController.addTuning(frequencyField.text, bandwidthBox.currentText)
                                else
                                    controller.tuningController.updateTuning(window.editingTuningIndex, frequencyField.text, bandwidthBox.currentText)
                                frequencyField.clear()
                                window.editingTuningIndex = -1
                            }
                        }
                        Button {
                            text: "Cancel edit"
                            visible: window.editingTuningIndex >= 0
                            onClicked: {
                                frequencyField.clear()
                                window.editingTuningIndex = -1
                            }
                        }
                        Item { Layout.fillWidth: true }
                        Label { text: controller.tuningController.tunings.length + " tuning(s)"; color: "#68766d" }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    ListView {
                        id: tuningList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: controller.tuningController.tunings
                        delegate: RowLayout {
                            required property var modelData
                            width: tuningList.width
                            height: 48
                            Label { text: (modelData.index + 1).toString().padStart(2, "0"); color: "#829087"; Layout.preferredWidth: 48 }
                            Label { text: Number(modelData.frequency).toFixed(3) + " MHz"; color: "#26362c"; Layout.fillWidth: true }
                            Label { text: modelData.bandwidth + " MHz"; color: "#526158"; Layout.preferredWidth: 100 }
                            ToolButton {
                                text: "Edit"
                                enabled: !controller.generalBusy
                                onClicked: {
                                    window.editingTuningIndex = modelData.index
                                    frequencyField.text = Number(modelData.frequency).toFixed(3)
                                    bandwidthBox.currentIndex = Math.max(0, ["6", "7", "8"].indexOf(String(modelData.bandwidth)))
                                }
                            }
                            ToolButton { text: "Remove"; onClicked: controller.tuningController.removeTuning(modelData.index) }
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#e1e6e2" }
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
                            Label { text: "TV Stations"; font.pixelSize: 26; font.bold: true; color: "#17251e" }
                            Label { text: controller.channelController.channels.length + " service(s) in the current map"; color: "#68766d" }
                        }
                        Item { Layout.fillWidth: true }
                        Button { text: "Open profile"; onClicked: openProfileDialog.open() }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "LCN"; Layout.preferredWidth: 70; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "SERVICE"; Layout.fillWidth: true; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "TYPE"; Layout.preferredWidth: 150; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "FREQUENCY"; Layout.preferredWidth: 130; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Label { text: "DVB ID"; Layout.preferredWidth: 150; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                    }
                    ListView {
                        id: channelList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: controller.channelController.channels
                        delegate: RowLayout {
                            required property var modelData
                            width: channelList.width
                            height: 50
                            Label { text: modelData.lcn === null ? "-" : modelData.lcn; Layout.preferredWidth: 70; color: "#526158" }
                            Label { text: modelData.name; Layout.fillWidth: true; color: "#26362c"; elide: Text.ElideRight }
                            Label { text: modelData.type; Layout.preferredWidth: 150; color: "#526158" }
                            Label { text: modelData.frequency; Layout.preferredWidth: 130; color: "#526158" }
                            Label { text: modelData.id; Layout.preferredWidth: 150; color: "#758178"; font.pixelSize: 12 }
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#e1e6e2" }
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
                            color: controller.serverController.serverState === "Running" ? "#338457" : "#a4aaa5"
                        }
                        Label { text: controller.serverController.serverState; color: "#526158" }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    Label { text: "Listener"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            id: hostField
                            Layout.fillWidth: true
                            text: controller.serverController.serverHost
                            placeholderText: "Bind address"
                            enabled: controller.serverController.serverState !== "Running"
                            onEditingFinished: controller.serverController.serverHost = text
                        }
                        TextField {
                            id: portField
                            Layout.preferredWidth: 130
                            text: controller.serverController.serverPort.toString()
                            placeholderText: "Port"
                            validator: IntValidator { bottom: 0; top: 65535 }
                            enabled: controller.serverController.serverState !== "Running"
                            onEditingFinished: {
                                if (acceptableInput)
                                    controller.serverController.serverPort = Number(text)
                            }
                        }
                        Button {
                            text: "Start"
                            highlighted: true
                            enabled: controller.serverController.serverState === "Stopped" && !controller.userDeviceBusy
                            onClicked: controller.serverController.startServer()
                        }
                        Button {
                            text: "Stop"
                            enabled: controller.serverController.serverState === "Running" && !controller.userDeviceBusy
                            onClicked: controller.serverController.stopServer()
                        }
                    }
                    Label { text: "Playlist URL"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    TextField {
                        Layout.fillWidth: true
                        readOnly: true
                        text: controller.serverController.playlistUrl || "Start the server to expose the playlist"
                        selectByMouse: true
                    }
                    Label { text: "Adapter signal"; font.pixelSize: 16; font.bold: true; color: "#26362c" }
                    Label { text: controller.serverController.serverSignalSummary; color: "#526158"; wrapMode: Text.Wrap }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#d9dfda" }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "CONNECTED CLIENTS"; color: "#77847b"; font.pixelSize: 11; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label { text: controller.serverController.serverClients.length; color: "#77847b"; font.pixelSize: 11 }
                    }
                    ListView {
                        id: clientList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: controller.serverController.serverClients
                        delegate: RowLayout {
                            required property var modelData
                            width: clientList.width
                            height: 48
                            Label { text: modelData.service; Layout.fillWidth: true; color: "#26362c"; elide: Text.ElideRight }
                            Label { text: modelData.serviceId; Layout.preferredWidth: 150; color: "#758178" }
                            Label { text: modelData.peer; Layout.preferredWidth: 170; color: "#526158" }
                            Label { text: Number(modelData.bytesSent).toLocaleString() + " B"; Layout.preferredWidth: 110; color: "#526158" }
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
                    property bool playbackAvailable: playerAvailable
                    anchors.fill: parent
                    active: playbackAvailable
                    source: active ? "Player.qml" : ""
                    onLoaded: {
                        item.appController = controller
                        item.selectedChannelId = window.selectedChannelId
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
            }
        }
    }

    FileDialog {
        id: openProfileDialog
        title: "Open PyDVB profile"
        fileMode: FileDialog.OpenFile
        nameFilters: ["PyDVB profile (*.json)", "All files (*)"]
        onAccepted: controller.loadProfile(selectedFile.toString())
    }

    FileDialog {
        id: saveProfileDialog
        title: "Save PyDVB profile"
        fileMode: FileDialog.SaveFile
        nameFilters: ["PyDVB profile (*.json)", "All files (*)"]
        onAccepted: controller.saveProfile(selectedFile.toString())
    }

    FileDialog {
        id: scanTableDialog
        title: "Open DVB-T scan table"
        fileMode: FileDialog.OpenFile
        onAccepted: controller.tuningController.loadLocalScanTable(selectedFile.toString())
    }

    Component {
        id: logWindowComponent

        Window {
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
                        text: appController.logController.logMessages.length + " recent log message(s)"
                        color: "#526158"
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Clear"
                        onClicked: appController.logController.clearLogs()
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
                        text: appController.logController.logMessages.join("\n")
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