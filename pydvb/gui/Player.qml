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
    property bool playerControlsVisible: false
    property bool playerOnlyMode: false
    property bool fullScreenMode: false
    property bool playbackStopped: false
    readonly property int overlayControlSize: 56
    readonly property int overlayIconSize: 24
    readonly property int overlayTextPixelSize: 18
    signal playerOnlyModeToggleRequested()
    signal fullScreenRequested()
    readonly property bool isPlaying: player.playbackState === MediaPlayer.PlayingState
    readonly property string playbackUrl: appController && selectedChannel
        && appController.serverController.streamBaseUrl.length > 0
        ? appController.serverController.streamBaseUrl + "/service/" + selectedChannel.id
        : ""
    readonly property bool canTogglePlayback: playbackUrl.length > 0
        && player.mediaStatus !== MediaPlayer.LoadingMedia
    readonly property bool canStopPlayback: player.playbackState !== MediaPlayer.StoppedState
    readonly property bool waitingForVideoFrame:
        player.source.toString().length > 0 && !hasVideoFrame
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

    onSelectedChannelIdChanged: root.playbackStopped = false

    Rectangle {
        anchors.fill: parent
        color: "#000000"

        VideoOutput {
            id: videoOutputItem
            anchors.fill: parent
            fillMode: VideoOutput.PreserveAspectFit
        }
        Column {
            anchors.centerIn: parent
            width: parent.width - 48
            spacing: 10
            visible: player.source.toString().length === 0
                || (root.waitingForVideoFrame && root.playbackError.length === 0)
            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                palette.highlight: "#ffffff"
                running: root.waitingForVideoFrame
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
            onTriggered: root.playerControlsVisible = false
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
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 18
                height: overlayInfo.implicitHeight + 20
                radius: 8
                color: "#b8141a16"

                RowLayout {
                    id: overlayInfo
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 12

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 16

                        Label {
                            visible: !!root.selectedChannel
                            text: root.selectedChannel
                                ? (root.selectedChannel.lcn === null
                                    ? "-" : root.selectedChannel.lcn)
                                : ""
                            color: "#eaf1ec"
                            font.pixelSize: root.overlayTextPixelSize
                        }
                        Label {
                            Layout.fillWidth: true
                            text: root.selectedChannel
                                ? root.selectedChannel.name
                                : "Select a channel from the sidebar"
                            color: "#eaf1ec"
                            font.pixelSize: root.overlayTextPixelSize
                            elide: Text.ElideRight
                        }
                    }
                    Label {
                        text: root.playbackStateText()
                        color: "#eaf1ec"
                        font.pixelSize: root.overlayTextPixelSize
                    }
                }
            }

            Rectangle {
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
        audioOutput: AudioOutput {}
        videoOutput: videoOutputItem

        onSourceChanged: {
            root.hasVideoFrame = false
            root.playbackError = ""
            if (source.toString().length > 0)
                play()
            else
                stop()
        }
        onErrorOccurred: function(error, errorString) {
            root.playbackError = errorString
        }
    }

    Connections {
        target: videoOutputItem.videoSink

        function onVideoFrameChanged(frame) {
            if (root.hasVideoFrame)
                return
            root.hasVideoFrame = true
        }
    }

    function playbackStateText() {
        switch (player.playbackState) {
        case MediaPlayer.PlayingState:
            return "Playing"
        case MediaPlayer.PausedState:
            return "Paused"
        default:
            return "Stopped"
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
            return "Preparing " + root.selectedChannel.name + "..."
        switch (player.mediaStatus) {
        case MediaPlayer.LoadingMedia:
            return "Connecting to " + root.selectedChannel.name + "..."
        case MediaPlayer.BufferingMedia:
            return "Buffering " + root.selectedChannel.name + "..."
        case MediaPlayer.StalledMedia:
            return "Waiting for data from " + root.selectedChannel.name + "..."
        case MediaPlayer.LoadedMedia:
            return "Starting " + root.selectedChannel.name + "..."
        case MediaPlayer.BufferedMedia:
            return "Waiting for video from " + root.selectedChannel.name + "..."
        default:
            return player.playbackState === MediaPlayer.PlayingState
                ? "Waiting for video from " + root.selectedChannel.name + "..."
                : "Preparing " + root.selectedChannel.name + "..."
        }
    }
}
