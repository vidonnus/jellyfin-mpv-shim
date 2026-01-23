from __future__ import annotations

import logging
import os

from .conf import settings
from .media import Media
from .player import playerManager
from .timeline import timelineManager
from .utils import execute_command

log = logging.getLogger("event_handler")
bindings = {}

NAVIGATION_DICT = {
    "Back": "back",
    "Select": "ok",
    "MoveUp": "up",
    "MoveDown": "down",
    "MoveRight": "right",
    "MoveLeft": "left",
    "GoHome": "home",
    "GoToSettings": "home",
}

from typing import TYPE_CHECKING, Optional, List

if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type


def find_next_unwatched_index(
    client: "JellyfinClient_type", item_ids: List[str]
) -> tuple[Optional[int], Optional[int]]:
    """
    Find the index of the next unwatched episode and its resume position.

    This function queries each item to check its PlayedPercentage and UserData
    to determine which episode should be played next. It returns:
    1. The first partially watched episode (in progress) with resume position, or
    2. The first unwatched episode with no resume position, or
    3. (None, None) if all episodes are watched

    Args:
        client: Jellyfin API client
        item_ids: List of episode item IDs

    Returns:
        Tuple of (index, resume_ticks) where:
        - index: Index of the next unwatched episode, or None if all are watched
        - resume_ticks: Resume position in ticks, or None to start from beginning
    """
    if not item_ids:
        return None, None

    try:
        first_unwatched_index = None

        for index, item_id in enumerate(item_ids):
            try:
                item = client.jellyfin.get_item(item_id)
                user_data = item.get("UserData", {})

                # Check if episode is in progress (partially watched)
                played_percentage = user_data.get("PlayedPercentage", 0)
                playback_position_ticks = user_data.get("PlaybackPositionTicks", 0)

                if played_percentage > 0 and played_percentage < 100:
                    if settings.log_decisions:
                        log.info(
                            "Found in-progress episode at index %d: %s (%.1f%% watched)",
                            index,
                            item.get("Name"),
                            played_percentage,
                        )
                    return index, playback_position_ticks

                # Check if episode is unwatched
                is_played = user_data.get("Played", False)
                if not is_played and first_unwatched_index is None:
                    first_unwatched_index = index
                    # Don't return yet - keep looking for in-progress episodes

            except Exception as e:
                log.warning("Error checking item %s: %s", item_id, e)
                continue

        if first_unwatched_index is not None:
            return first_unwatched_index, None

        # All episodes are watched, return None to use default behavior
        return None, None

    except Exception as e:
        log.error("Error finding next unwatched episode: %s", e, exc_info=True)
        return None, None


def bind(event_name: str):
    def decorator(func):
        bindings[event_name] = func
        return func

    return decorator


class EventHandler(object):
    mirror = None

    def handle_event(
        self,
        client: "JellyfinClient_type",
        event_name: str,
        arguments: dict,
    ):
        if event_name in bindings:
            log.debug("Handled Event {0}: {1}".format(event_name, arguments))
            bindings[event_name](self, client, event_name, arguments)
        else:
            log.debug("Unhandled Event {0}: {1}".format(event_name, arguments))

    @bind("Play")
    def play_media(self, client: "JellyfinClient_type", _event_name, arguments: dict):
        play_command = arguments.get("PlayCommand")
        if not playerManager.has_video():
            play_command = "PlayNow"

        if play_command == "PlayNow":
            seq = arguments.get("StartIndex")
            item_ids = arguments.get("ItemIds")
            resume_ticks = None

            # If StartIndex is not provided, try to find the next unwatched episode
            if seq is None and item_ids and len(item_ids) > 1:
                next_unwatched, resume_ticks = find_next_unwatched_index(
                    client, item_ids
                )
                if next_unwatched is not None:
                    seq = next_unwatched
                    if settings.log_decisions:
                        log.info("Auto-selected episode at index %d", seq)
                else:
                    seq = 0
            elif seq is None:
                seq = 0

            media = Media(
                client,
                item_ids,
                seq=seq,
                user_id=arguments.get("ControllingUserId"),
                aid=arguments.get("AudioStreamIndex"),
                sid=arguments.get("SubtitleStreamIndex"),
                srcid=arguments.get("MediaSourceId"),
            )

            log.debug("EventHandler::playMedia %s" % media)

            # Use resume position from unwatched detection if available, otherwise use StartPositionTicks
            offset = arguments.get("StartPositionTicks")
            if resume_ticks is not None:
                offset = resume_ticks

            if offset is not None:
                offset /= 10000000

            video = media.video
            if video:
                if settings.pre_media_cmd:
                    execute_command(settings.pre_media_cmd)
                playerManager.play(video, offset, is_initial_play=True)
                timelineManager.send_timeline()
                if arguments.get("SyncPlayGroup") is not None:
                    playerManager.syncplay.join_group(arguments["SyncPlayGroup"])
                if settings.play_cmd:
                    execute_command(settings.play_cmd)
        elif play_command == "PlayLast":
            playerManager.get_video().parent.insert_items(
                arguments.get("ItemIds"), append=True
            )
            playerManager.upd_player_hide()
        elif play_command == "PlayNext":
            playerManager.get_video().parent.insert_items(
                arguments.get("ItemIds"), append=False
            )
            playerManager.upd_player_hide()

    @bind("GeneralCommand")
    def general_command(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        command = arguments.get("Name")
        if command == "SetVolume":
            # There is currently a bug that causes this to be spammed, so we
            # only update it if the value actually changed.
            if playerManager.get_volume(True) != int(arguments["Arguments"]["Volume"]):
                playerManager.set_volume(int(arguments["Arguments"]["Volume"]))
        elif command == "SetAudioStreamIndex":
            playerManager.set_streams(int(arguments["Arguments"]["Index"]), None)
        elif command == "SetSubtitleStreamIndex":
            playerManager.set_streams(None, int(arguments["Arguments"]["Index"]))
        elif command == "DisplayContent":
            # If you have an idle command set, this will delay it.
            timelineManager.delay_idle()
            if self.mirror:
                self.mirror.display_content(client, arguments)
        elif command in (
            "Back",
            "Select",
            "MoveUp",
            "MoveDown",
            "MoveRight",
            "MoveLeft",
            "GoHome",
            "GoToSettings",
        ):
            playerManager.menu_action(NAVIGATION_DICT[command])
        elif command in ("Mute", "Unmute"):
            playerManager.set_mute(command == "Mute")
        elif command == "TakeScreenshot":
            playerManager.screenshot()
        elif command == "ToggleFullscreen" or command is None:
            # Currently when you hit the fullscreen button, no command is specified...
            playerManager.toggle_fullscreen()

    @bind("Playstate")
    def play_state(self, _client: "JellyfinClient_type", _event_name, arguments: dict):
        command = arguments.get("Command")
        if command == "PlayPause":
            playerManager.toggle_pause()
            timelineManager.send_timeline()
        elif command == "Pause":
            playerManager.pause_if_playing()
            timelineManager.send_timeline()
        elif command == "Unpause":
            playerManager.play_if_paused()
            timelineManager.send_timeline()
        elif command == "PreviousTrack":
            playerManager.play_prev()
        elif command == "NextTrack":
            playerManager.play_next()
        elif command == "Stop":
            playerManager.stop()
        elif command == "Seek":
            playerManager.seek(
                arguments.get("SeekPositionTicks") / 10000000, absolute=True
            )

    @bind("PlayPause")
    def pause_play(self, _client: "JellyfinClient_type", _event_name, _arguments: dict):
        playerManager.toggle_pause()
        timelineManager.send_timeline()

    @bind("SyncPlayGroupUpdate")
    def sync_play_group_update(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_group_update(arguments)

    @bind("SyncPlayCommand")
    def sync_play_command(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_command(arguments)


eventHandler = EventHandler()
