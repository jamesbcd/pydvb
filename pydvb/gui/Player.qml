import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia

Item {
    id: root

    property var appController: null
    property string selectedChannelId: ""
    property string playbackError: ""
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
                text: player.playbackState === MediaPlayer.PlayingState ? "Playing"
                    : player.playbackState === MediaPlayer.PausedState ? "Paused"
                    : "Stopped"
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
            Label {
                anchors.centerIn: parent
                width: parent.width - 48
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                color: "#d9dfda"
                visible: player.source.toString().length === 0
                text: !root.selectedChannel
                    ? "Choose a channel from the Player list."
                    : root.appController.serverController.serverState !== "Running"
                        ? "Start the HTTP server to play this channel."
                        : "Connecting to " + root.selectedChannel.name + "..."
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
                onClicked: {
                    root.playbackError = ""
                    if (player.playbackState === MediaPlayer.PlayingState)
                        player.pause()
                    else
                        player.play()
                }
            }
            Button {
                text: "Stop"
                enabled: player.playbackState !== MediaPlayer.StoppedState
                onClicked: player.stop()
            }
            Slider {
                Layout.fillWidth: true
                from: 0
                to: Math.max(player.duration, 1)
                value: player.position
                enabled: player.seekable
                onMoved: player.position = value
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
}
