import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia

Item {
    id: root

    signal channelSelectionRequested(string channelId)

    property var appController: null
    property string selectedChannelId: ""
    property string playbackError: ""
    property bool hasVideoFrame: false
    property bool mediaSourceReady: false
    property bool playerControlsVisible: false
    property bool playerOnlyMode: false
    property bool fullScreenMode: false
    property bool playbackStopped: false
    property real playerVolume: 1.0
    property real volumeBeforeMute: 1.0
    property bool playerMuted: false
    readonly property int overlayControlSize: 56
    readonly property int overlayIconSize: 24
    readonly property int overlayTextPixelSize: 18
    signal playerOnlyModeToggleRequested()
    signal fullScreenRequested()
    readonly property bool isPlaying: player.playbackState === MediaPlayer.PlayingState
    readonly property bool isConnectedToServer: !playbackStopped
        && playbackError.length === 0
        && player.source.toString().length > 0
        && (player.mediaStatus === MediaPlayer.LoadedMedia
            || player.mediaStatus === MediaPlayer.BufferingMedia
            || player.mediaStatus === MediaPlayer.BufferedMedia)
    readonly property string playbackUrl: appController && selectedChannel
        && appController.serverController.streamBaseUrl.length > 0
        ? appController.serverController.streamBaseUrl + "/service/" + selectedChannel.id
        : ""
    readonly property bool canTogglePlayback: playbackUrl.length > 0
        && player.mediaStatus !== MediaPlayer.LoadingMedia
    readonly property bool canStopPlayback: player.playbackState !== MediaPlayer.StoppedState
    readonly property bool isAudioOnly: !!root.selectedChannel
        && root.selectedChannel.metatype === "RADIO"
    readonly property bool waitingForPlaybackOutput:
        player.source.toString().length > 0
        && (!mediaSourceReady
            || (root.isAudioOnly
                ? player.playbackState !== MediaPlayer.PlayingState
                    && player.playbackState !== MediaPlayer.PausedState
                : !hasVideoFrame))
    readonly property var selectableChannels: appController
        ? appController.channelController.channels.filter(function(channel) {
            return channel.id !== "Unknown"
        })
        : []

    function togglePlayback() {
        if (root.isPlaying)
            player.pause()
        else
            root.playSelectedChannel()
    }

    function stopPlayback() {
        root.playbackStopped = true
        player.stop()
    }

    function selectAdjacentChannel(direction) {
        const channels = root.selectableChannels
        if (channels.length === 0)
            return

        let currentIndex = -1
        for (let index = 0; index < channels.length; index++) {
            if (channels[index].id === root.selectedChannelId) {
                currentIndex = index
                break
            }
        }

        const nextIndex = currentIndex === -1
            ? (direction > 0 ? 0 : channels.length - 1)
            : (currentIndex + direction + channels.length) % channels.length
        root.channelSelectionRequested(channels[nextIndex].id)
    }

    function revealPlayerControls() {
        root.playerControlsVisible = true
        controlsHideTimer.restart()
    }

    function playSelectedChannel() {
        if (root.playbackUrl.length === 0
                || player.mediaStatus === MediaPlayer.LoadingMedia)
            return
        root.playbackError = ""
        root.playbackStopped = false
        if (player.source.toString().length > 0)
            player.play()
    }

    readonly property var selectedChannel: {
        if (!appController)
            return null
        const channels = appController.channelController.channels
        for (let index = 0; index < channels.length; index++) {
            if (channels[index].id === selectedChannelId)
                return channels[index]
        }
        return null
    }
    readonly property string selectedChannelDisplayName: {
        if (!root.selectedChannel)
            return ""
        return root.selectedChannel.lcn === null
            ? root.selectedChannel.name
            : "channel " + root.selectedChannel.lcn + ": " + root.selectedChannel.name
    }
    property bool eventInfoVisible: false
    readonly property var selectedProgramGuideEntry: {
        if (!root.appController || !root.selectedChannel)
            return null
        const programGuide = root.appController.channelController.programGuide
        for (let index = 0; index < programGuide.length; index++) {
            if (programGuide[index].id === root.selectedChannel.id)
                return programGuide[index]
        }
        return null
    }
    readonly property string currentEventTitle: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.now : ""
    readonly property string currentEventTime: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.nowTime : ""
    readonly property string currentEventDescription: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.nowDescription : ""
    readonly property string nextEventTitle: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.next : ""
    readonly property string nextEventTime: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.nextTime : ""
    readonly property string nextEventDescription: root.selectedProgramGuideEntry
        ? root.selectedProgramGuideEntry.nextDescription : ""

    onSelectedChannelIdChanged: {
        root.playbackStopped = false
        root.hasVideoFrame = false
        root.mediaSourceReady = false
        root.playbackError = ""
    }

    Rectangle {
        anchors.fill: parent
        color: "#000000"

        VideoOutput {
            id: videoOutputItem
            anchors.fill: parent
            fillMode: VideoOutput.PreserveAspectFit
        }
        Image {
            anchors.centerIn: parent
            source: "icons/music.svg"
            sourceSize: Qt.size(96, 96)
            width: 96
            height: 96
            fillMode: Image.PreserveAspectFit
            visible: root.isAudioOnly
                && root.mediaSourceReady
                && !root.waitingForPlaybackOutput
                && root.playbackError.length === 0
            Accessible.name: "Audio-only channel"
        }
        Column {
            anchors.centerIn: parent
            width: parent.width - 48
            spacing: 10
            visible: player.source.toString().length === 0
                || (root.waitingForPlaybackOutput && root.playbackError.length === 0)
            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                palette.text: "#ffffff" // Used by Fusion style?
                palette.highlight: "#ffffff" // Used by other styles?
                running: root.waitingForPlaybackOutput
                    && root.playbackError.length === 0
                visible: running
            }
            Label {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                color: "#d9dfda"
                text: root.playerStatusMessage()
            }
        }

        HoverHandler {
            property point lastPosition: Qt.point(-1, -1)

            onHoveredChanged: {
                if (hovered) {
                    lastPosition = point.position
                    root.revealPlayerControls()
                }
            }
            onPointChanged: {
                if (hovered && (point.position.x !== lastPosition.x
                                || point.position.y !== lastPosition.y)) {
                    lastPosition = point.position
                    root.revealPlayerControls()
                }
            }
        }

        Timer {
            id: controlsHideTimer
            interval: 2000
            onTriggered: {
                if (volumeProximityHandler.hovered
                        || volumeButton.hovered
                        || volumeSlider.hovered
                        || volumeSlider.pressed)
                    root.revealPlayerControls()
                else
                    root.playerControlsVisible = false
            }
        }

        Item {
            anchors.fill: parent
            opacity: root.playerControlsVisible ? 1 : 0
            visible: opacity > 0
            z: 1
            Behavior on opacity {
                NumberAnimation { duration: 180 }
            }

            Rectangle {
                id: controlsBackground
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 24
                width: controlsRow.implicitWidth + 24
                height: controlsRow.implicitHeight + 24
                radius: height / 2
                color: "#b8141a16"

                Row {
                    id: controlsRow
                    anchors.centerIn: parent
                    spacing: 12

                    Button {
                        id: playerOnlyModeButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        Accessible.name: root.playerOnlyMode
                            ? "Restore window layout"
                            : "Show player only"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.playerOnlyModeToggleRequested()
                        background: Rectangle {
                            radius: width / 2
                            color: playerOnlyModeButton.down
                                ? "#dce9df"
                                : (playerOnlyModeButton.hovered ? "#f5faf6" : "#eaf1ec")
                        }
                        contentItem: Item {
                            Image {
                                source: "icons/video-panel-left.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Button {
                        id: previousChannelButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        enabled: root.selectableChannels.length > 0
                        Accessible.name: "Previous channel"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.selectAdjacentChannel(-1)
                        background: Rectangle {
                            radius: width / 2
                            color: previousChannelButton.down
                                ? "#dce9df"
                                : (previousChannelButton.hovered ? "#f5faf6" : "#eaf1ec")
                            opacity: previousChannelButton.enabled ? 1 : 0.55
                        }
                        contentItem: Item {
                            Image {
                                source: "icons/video-skip-back.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Button {
                        id: playbackButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        enabled: root.canTogglePlayback
                        Accessible.name: root.isPlaying ? "Pause playback" : "Play playback"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.togglePlayback()
                        background: Rectangle {
                            radius: width / 2
                            color: playbackButton.down
                                ? "#dce9df"
                                : (playbackButton.hovered ? "#f5faf6" : "#eaf1ec")
                            opacity: playbackButton.enabled ? 1 : 0.55
                        }
                        contentItem: Item {
                            Image {
                                source: root.isPlaying
                                    ? "icons/video-pause.svg"
                                    : "icons/video-play.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Button {
                        id: stopButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        enabled: root.canStopPlayback
                        Accessible.name: "Stop playback"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.stopPlayback()
                        background: Rectangle {
                            radius: width / 2
                            color: stopButton.down
                                ? "#dce9df"
                                : (stopButton.hovered ? "#f5faf6" : "#eaf1ec")
                            opacity: stopButton.enabled ? 1 : 0.55
                        }
                        contentItem: Item {
                            Image {
                                source: "icons/video-stop.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Button {
                        id: nextChannelButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        enabled: root.selectableChannels.length > 0
                        Accessible.name: "Next channel"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.selectAdjacentChannel(1)
                        background: Rectangle {
                            radius: width / 2
                            color: nextChannelButton.down
                                ? "#dce9df"
                                : (nextChannelButton.hovered ? "#f5faf6" : "#eaf1ec")
                            opacity: nextChannelButton.enabled ? 1 : 0.55
                        }
                        contentItem: Item {
                            Image {
                                source: "icons/video-skip-forward.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Item {
                        id: volumeControl
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        property bool sliderVisible: false

                        Timer {
                            id: volumeSliderHideTimer
                            interval: 1000
                            onTriggered: {
                                if (!volumeProximityHandler.hovered
                                        && !volumeButton.hovered
                                        && !volumeSlider.hovered
                                        && !volumeSlider.pressed)
                                    volumeControl.sliderVisible = false
                            }
                        }

                        Button {
                            id: volumeButton
                            anchors.fill: parent
                            padding: 0
                            Accessible.name: root.playerMuted ? "Unmute" : "Mute"
                            ToolTip.visible: hovered
                            ToolTip.text: Accessible.name
                            onClicked: {
                                if (root.playerMuted) {
                                    root.playerVolume = root.volumeBeforeMute
                                    root.playerMuted = false
                                } else {
                                    root.volumeBeforeMute = root.playerVolume
                                    root.playerMuted = true
                                }
                            }
                            onHoveredChanged: {
                                if (hovered) {
                                    volumeControl.sliderVisible = true
                                    volumeSliderHideTimer.stop()
                                } else {
                                    volumeSliderHideTimer.restart()
                                }
                            }
                            background: Rectangle {
                                radius: width / 2
                                color: volumeButton.down
                                    ? "#dce9df"
                                    : (volumeButton.hovered ? "#f5faf6" : "#eaf1ec")
                            }
                            contentItem: Item {
                                Image {
                                    source: root.playerMuted
                                        ? "icons/volume-muted.svg"
                                        : "icons/volume.svg"
                                    sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                    width: root.overlayIconSize
                                    height: root.overlayIconSize
                                    fillMode: Image.PreserveAspectFit
                                    anchors.centerIn: parent
                                }
                            }

                        }
                    }

                    Button {
                        id: eventInfoButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        checkable: true
                        checked: root.eventInfoVisible
                        Accessible.name: root.eventInfoVisible
                            ? "Hide event information"
                            : "Show event information"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.eventInfoVisible = checked
                        background: Rectangle {
                            radius: width / 2
                            color: eventInfoButton.checked
                                ? "#bfd2c4"
                                : (eventInfoButton.down
                                    ? "#dce9df"
                                    : (eventInfoButton.hovered ? "#f5faf6" : "#eaf1ec"))
                        }
                        contentItem: Item {
                            Image {
                                source: "icons/info.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                    Button {
                        id: fullScreenButton
                        width: root.overlayControlSize
                        height: root.overlayControlSize
                        padding: 0
                        Accessible.name: root.fullScreenMode
                            ? "Exit full screen"
                            : "Enter full screen"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.fullScreenRequested()
                        background: Rectangle {
                            radius: width / 2
                            color: fullScreenButton.down
                                ? "#dce9df"
                                : (fullScreenButton.hovered ? "#f5faf6" : "#eaf1ec")
                        }
                        contentItem: Item {
                            Image {
                                source: root.fullScreenMode
                                    ? "icons/video-minimize.svg"
                                    : "icons/video-maximize.svg"
                                sourceSize: Qt.size(root.overlayIconSize, root.overlayIconSize)
                                width: root.overlayIconSize
                                height: root.overlayIconSize
                                fillMode: Image.PreserveAspectFit
                                anchors.centerIn: parent
                            }
                        }
                    }

                }
            }

            Item {
                id: volumeProximityArea
                x: controlsBackground.x + controlsRow.x + volumeControl.x - 36
                y: controlsBackground.y + controlsRow.y + volumeControl.y - 164
                width: 128
                height: 220
                z: 2

                HoverHandler {
                    id: volumeProximityHandler
                    onHoveredChanged: {
                        if (hovered) {
                            volumeControl.sliderVisible = true
                            volumeSliderHideTimer.stop()
                            controlsHideTimer.restart()
                        } else {
                            volumeSliderHideTimer.restart()
                        }
                    }
                }
            }

            Slider {
                id: volumeSlider
                x: controlsBackground.x + controlsRow.x + volumeControl.x
                    + (volumeControl.width - width) / 2
                y: controlsBackground.y + controlsRow.y + volumeControl.y - height
                width: root.overlayControlSize
                height: 120
                z: 3
                orientation: Qt.Vertical
                from: 0.0
                to: 1.0
                stepSize: 0.05
                value: root.playerMuted ? 0 : root.playerVolume
                visible: volumeControl.sliderVisible
                Accessible.name: "Volume"
                ToolTip.visible: hovered
                ToolTip.text: Math.round(value * 100) + "%"
                onMoved: {
                    root.playerVolume = value
                    if (root.playerMuted)
                        root.playerMuted = false
                }
                onPressedChanged: {
                    if (pressed) {
                        volumeSliderHideTimer.stop()
                        controlsHideTimer.stop()
                    } else {
                        root.revealPlayerControls()
                        volumeSliderHideTimer.restart()
                    }
                }
                onHoveredChanged: {
                    if (hovered) {
                        volumeControl.sliderVisible = true
                        volumeSliderHideTimer.stop()
                    } else {
                        volumeSliderHideTimer.restart()
                    }
                }
            }
        }

        Column {
            id: eventOverlayColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            anchors.topMargin: 18
            spacing: 8
            z: 1

            Item {
                id: currentEventHeader
                width: parent.width
                height: Math.max(currentChannelPanel.height, currentEventPanel.height)
                opacity: root.playerControlsVisible ? 1 : 0
                Behavior on opacity {
                    NumberAnimation { duration: 180 }
                }
                property real channelReservation: root.selectedChannel
                    ? currentChannelPanel.width + 8 : 0

                Rectangle {
                    id: currentChannelPanel
                    anchors.left: parent.left
                    anchors.top: parent.top
                    width: currentChannelContent.width + 20
                    height: currentChannelContent.implicitHeight + 20
                    radius: 8
                    color: "#b8141a16"
                    visible: !!root.selectedChannel

                    Column {
                        id: currentChannelContent
                        anchors.centerIn: parent
                        width: Math.min(
                            currentChannelLabel.implicitWidth,
                            Math.max(0, currentEventHeader.width * 0.35 - 20)
                        )

                        Label {
                            id: currentChannelLabel
                            width: parent.width
                            text: root.selectedChannel
                                ? (root.selectedChannel.lcn === null
                                    ? root.selectedChannel.name
                                    : root.selectedChannel.lcn + "  "
                                        + root.selectedChannel.name)
                                : ""
                            color: "#eaf1ec"
                            font.pixelSize: root.overlayTextPixelSize
                            horizontalAlignment: Text.AlignLeft
                            wrapMode: Text.Wrap
                        }
                    }
                }

                Rectangle {
                    id: currentEventPanel
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    width: currentEventContent.width + 20
                    height: currentEventContent.implicitHeight + 20
                    radius: 8
                    color: "#b8141a16"
                    visible: root.currentEventTitle.length > 0
                        || root.currentEventTime.length > 0

                    Column {
                        id: currentEventContent
                        anchors.centerIn: parent
                        width: Math.min(
                            Math.max(
                                currentEventTitleLabel.implicitWidth,
                                currentEventTimeLabel.implicitWidth
                            ),
                            Math.max(
                                0,
                                currentEventHeader.width
                                    - 2 * currentEventHeader.channelReservation - 20
                            )
                        )
                        spacing: 2

                        Label {
                            id: currentEventTitleLabel
                            width: parent.width
                            visible: root.currentEventTitle.length > 0
                            text: root.currentEventTitle
                            color: "#eaf1ec"
                            font.pixelSize: root.overlayTextPixelSize
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.Wrap
                        }
                        Label {
                            id: currentEventTimeLabel
                            width: parent.width
                            visible: root.currentEventTime.length > 0
                            text: root.currentEventTime
                            color: "#d9dfda"
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width, 720)
                height: eventInfoContent.implicitHeight + 20
                radius: 8
                color: "#b8141a16"
                visible: root.eventInfoVisible

                Column {
                    id: eventInfoContent
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 8

                    RowLayout {
                        width: parent.width
                        visible: root.currentEventTitle.length > 0
                            || root.currentEventTime.length > 0
                            || root.currentEventDescription.length > 0
                        spacing: 6

                        Label {
                            text: "NOW:"
                            color: "#c5d5c9"
                            font.pixelSize: 15
                            font.bold: true
                            Layout.alignment: Qt.AlignTop
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: root.currentEventTitle.length > 0
                                || root.currentEventTime.length > 0
                            text: root.currentEventTitle
                                + (root.currentEventTime.length > 0
                                    ? "  |  " + root.currentEventTime : "")
                            color: "#eaf1ec"
                            font.pixelSize: 15
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                    }
                    Label {
                        width: parent.width
                        visible: root.currentEventDescription.length > 0
                        text: root.currentEventDescription
                        color: "#eaf1ec"
                        font.pixelSize: 15
                        wrapMode: Text.Wrap
                    }
                    Item {
                        width: 1
                        height: 8
                        visible: (root.currentEventTitle.length > 0
                                || root.currentEventTime.length > 0
                                || root.currentEventDescription.length > 0)
                            && (root.nextEventTitle.length > 0
                                || root.nextEventTime.length > 0
                                || root.nextEventDescription.length > 0)
                    }
                    RowLayout {
                        width: parent.width
                        visible: root.nextEventTitle.length > 0
                            || root.nextEventTime.length > 0
                            || root.nextEventDescription.length > 0
                        spacing: 6

                        Label {
                            text: "NEXT:"
                            color: "#c5d5c9"
                            font.pixelSize: 15
                            font.bold: true
                            Layout.alignment: Qt.AlignTop
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: root.nextEventTitle.length > 0
                                || root.nextEventTime.length > 0
                            text: root.nextEventTitle
                                + (root.nextEventTime.length > 0
                                    ? "  |  " + root.nextEventTime : "")
                            color: "#eaf1ec"
                            font.pixelSize: 15
                            font.bold: true
                            wrapMode: Text.Wrap
                        }
                    }
                    Label {
                        width: parent.width
                        visible: root.nextEventDescription.length > 0
                        text: root.nextEventDescription
                        color: "#eaf1ec"
                        font.pixelSize: 15
                        wrapMode: Text.Wrap
                    }
                    Label {
                        width: parent.width
                        visible: root.currentEventTitle.length === 0
                            && root.currentEventTime.length === 0
                            && root.currentEventDescription.length === 0
                            && root.nextEventTitle.length === 0
                            && root.nextEventTime.length === 0
                            && root.nextEventDescription.length === 0
                        text: "No event information available"
                        color: "#d9dfda"
                        font.pixelSize: 15
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        Label {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            anchors.bottomMargin: 16
            visible: root.playbackError.length > 0
            text: root.playbackError
            color: "#ff9a90"
            wrapMode: Text.Wrap
            z: 2
        }
    }

    MediaPlayer {
        id: player
        source: root.playbackStopped ? "" : root.playbackUrl
        audioOutput: AudioOutput {
            volume: root.playerVolume
            muted: root.playerMuted
        }
        videoOutput: videoOutputItem

        onSourceChanged: {
            root.hasVideoFrame = false
            root.mediaSourceReady = false
            root.playbackError = ""
            if (source.toString().length > 0)
                play()
            else
                stop()
        }
        onMediaStatusChanged: {
            if (mediaStatus === MediaPlayer.LoadingMedia)
                root.mediaSourceReady = false
            else if (mediaStatus === MediaPlayer.LoadedMedia
                     || mediaStatus === MediaPlayer.BufferedMedia)
                root.mediaSourceReady = true
        }
        onErrorOccurred: function(error, errorString) {
            root.playbackError = errorString
        }
    }

    Connections {
        target: videoOutputItem.videoSink

        function onVideoFrameChanged(frame) {
            if (!root.mediaSourceReady || root.hasVideoFrame)
                return
            root.hasVideoFrame = true
        }
    }

    function playerStatusMessage() {
        if (!root.selectedChannel)
            return "Choose a channel from the Player list."
        if (!root.appController
                || root.appController.serverController.serverState !== "Running")
            return "Start the HTTP server to play this channel."
        if (root.playbackStopped)
            return "Playback stopped."
        if (player.source.toString().length === 0)
            return "Preparing " + root.selectedChannelDisplayName + "..."
        if (!root.mediaSourceReady)
            return "Connecting to " + root.selectedChannelDisplayName + "..."
        switch (player.mediaStatus) {
        case MediaPlayer.LoadingMedia:
            return "Connecting to " + root.selectedChannelDisplayName + "..."
        case MediaPlayer.BufferingMedia:
            return "Buffering " + root.selectedChannelDisplayName + "..."
        case MediaPlayer.StalledMedia:
            return "Waiting for data from " + root.selectedChannelDisplayName + "..."
        case MediaPlayer.LoadedMedia:
            return "Starting " + root.selectedChannelDisplayName + "..."
        case MediaPlayer.BufferedMedia:
            if (root.isAudioOnly)
                return player.playbackState === MediaPlayer.PlayingState
                    ? "Playing " + root.selectedChannelDisplayName + "."
                    : "Starting " + root.selectedChannelDisplayName + "..."
            return "Waiting for video from " + root.selectedChannelDisplayName + "..."
        default:
            if (root.isAudioOnly && player.playbackState === MediaPlayer.PlayingState)
                return "Playing " + root.selectedChannelDisplayName + "."
            return player.playbackState === MediaPlayer.PlayingState
                ? "Waiting for video from " + root.selectedChannelDisplayName + "..."
                : "Preparing " + root.selectedChannelDisplayName + "..."
        }
    }
}
