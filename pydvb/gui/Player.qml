import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia

Item {
    id: root

    property var appController: null
    property string selectedChannelId: ""
    property string playbackError: ""
    property bool hasVideoFrame: false
    readonly property bool waitingForVideoFrame:
        player.source.toString().length > 0 && !hasVideoFrame

    function playSelectedChannel() {
        if (player.source.toString().length === 0
                || player.mediaStatus === MediaPlayer.LoadingMedia)
            return
        root.playbackError = ""
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

    ColumnLayout {
        anchors.fill: parent
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                spacing: 3
                Label {
                    text: "Player"
                    font.pixelSize: 26
                    font.bold: true
                    color: "#17251e"
                }
                Label {
                    text: root.selectedChannel ? root.selectedChannel.name : "Select a channel from the sidebar"
                    color: "#68766d"
                }
            }
            Item { Layout.fillWidth: true }
            Label {
                text: root.playbackStateText()
                color: "#526158"
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: "#111512"

            VideoOutput {
                id: videoOutput
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
        }

        Label {
            Layout.fillWidth: true
            visible: root.playbackError.length > 0
            text: root.playbackError
            color: "#a33b32"
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            Button {
                text: player.playbackState === MediaPlayer.PlayingState ? "Pause" : "Play"
                enabled: player.source.toString().length > 0
                    && player.mediaStatus !== MediaPlayer.LoadingMedia
                onClicked: {
                    if (player.playbackState === MediaPlayer.PlayingState)
                        player.pause()
                    else
                        root.playSelectedChannel()
                }
            }
            Button {
                text: "Stop"
                enabled: player.playbackState !== MediaPlayer.StoppedState
                onClicked: player.stop()
            }
        }
    }

    MediaPlayer {
        id: player
        source: root.appController && root.selectedChannel
            && root.appController.serverController.streamBaseUrl.length > 0
            ? root.appController.serverController.streamBaseUrl + "/service/" + root.selectedChannel.id
            : ""
        audioOutput: AudioOutput {}
        videoOutput: videoOutput

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
        target: videoOutput.videoSink

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
