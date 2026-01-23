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

    Uses the Jellyfin /Shows/NextUp API endpoint for efficient server-side lookup
    instead of querying each episode individually. This reduces API calls from
    potentially 1000+ to just 2 (one to get SeriesId, one for NextUp).

    Falls back to batch fetching if NextUp is not available or doesn't return
    a result.

    Returns:
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
        # Try the optimized NextUp approach first
        result = _find_next_unwatched_via_nextup(client, item_ids)
        if result[0] is not None:
            return result

        # Fall back to batch fetching if NextUp didn't find anything
        # This handles edge cases where NextUp might not return results
        # but there are still unwatched episodes in the queue
        return _find_next_unwatched_via_batch(client, item_ids)

    except Exception as e:
        log.error("Error finding next unwatched episode: %s", e, exc_info=True)
        return None, None


def _find_next_unwatched_via_nextup(
    client: "JellyfinClient_type", item_ids: List[str]
) -> tuple[Optional[int], Optional[int]]:
    """
    Find next unwatched episode using the /Shows/NextUp API endpoint.

    This is the most efficient approach as it lets the server handle the logic
    with just 2 API calls total (1 to get SeriesId, 1 for NextUp).

    Args:
        client: Jellyfin API client
        item_ids: List of episode item IDs

    Returns:
        Tuple of (index, resume_ticks) or (None, None) if not found
    """
    try:
        # Get the first episode to find the SeriesId
        first_item = client.jellyfin.get_item(item_ids[0])
        series_id = first_item.get("SeriesId")

        if not series_id:
            if settings.log_decisions:
                log.info("No SeriesId found, falling back to batch fetch")
            return None, None

        # Query NextUp for this specific series
        next_up_result = client.jellyfin.shows(
            "/NextUp",
            {
                "UserId": "{UserId}",
                "SeriesId": series_id,
                "Limit": 1,
                "Fields": "UserData",
            },
        )

        items = next_up_result.get("Items", [])
        if not items:
            if settings.log_decisions:
                log.info("NextUp returned no results for series %s", series_id)
            return None, None

        next_episode = items[0]
        next_episode_id = next_episode.get("Id")

        # Find the index of this episode in our item_ids list
        try:
            index = item_ids.index(next_episode_id)
        except ValueError:
            # Episode not in our list (might be from a different season)
            if settings.log_decisions:
                log.info(
                    "NextUp episode %s not in current queue, falling back to batch fetch",
                    next_episode_id,
                )
            return None, None

        # Get resume position if available
        user_data = next_episode.get("UserData", {})
        playback_position_ticks = user_data.get("PlaybackPositionTicks", 0)
        played_percentage = user_data.get("PlayedPercentage", 0)

        # Only return resume position if episode is in progress
        resume_ticks = playback_position_ticks if played_percentage > 0 else None

        if settings.log_decisions:
            log.info(
                "NextUp found episode at index %d: %s (%.1f%% watched)",
                index,
                next_episode.get("Name"),
                played_percentage,
            )

        return index, resume_ticks

    except Exception as e:
        if settings.log_decisions:
            log.info("NextUp lookup failed: %s, falling back to batch fetch", e)
        return None, None


def _find_next_unwatched_via_batch(
    client: "JellyfinClient_type", item_ids: List[str]
) -> tuple[Optional[int], Optional[int]]:
    """
    Find next unwatched episode by batch fetching all items.

    This is the fallback approach when NextUp doesn't work. It fetches all
    items in a single API call and processes them locally.

    Args:
        client: Jellyfin API client
        item_ids: List of episode item IDs

    Returns:
        Tuple of (index, resume_ticks) or (None, None) if all watched
    """
    try:
        # Fetch all items in a single API call
        items_response = client.jellyfin.get_items(item_ids)
        items = items_response.get("Items", [])

        if not items:
            return None, None

        # Build a map of item_id -> item for quick lookup
        items_by_id = {item.get("Id"): item for item in items}

        first_unwatched_index = None

        # Iterate through item_ids to maintain order
        for index, item_id in enumerate(item_ids):
            item = items_by_id.get(item_id)
            if not item:
                continue

            user_data = item.get("UserData", {})
            played_percentage = user_data.get("PlayedPercentage", 0)
            playback_position_ticks = user_data.get("PlaybackPositionTicks", 0)

            # Check if episode is in progress (partially watched)
            if played_percentage > 0 and played_percentage < 100:
                if settings.log_decisions:
                    log.info(
                        "Batch fetch found in-progress episode at index %d: %s (%.1f%% watched)",
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

        if first_unwatched_index is not None:
            if settings.log_decisions:
                log.info(
                    "Batch fetch found first unwatched episode at index %d",
                    first_unwatched_index,
                )
            return first_unwatched_index, None

        # All episodes are watched
        if settings.log_decisions:
            log.info("All episodes are watched")
        return None, None

    except Exception as e:
        log.warning("Batch fetch failed: %s", e)
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
