import logging
import threading
import os

import jellyfin_apiclient_python.exceptions

from .conf import settings
from .player import playerManager, _mpv_errors
from .utils import Timer, execute_command

log = logging.getLogger("timeline")


class TimelineManager(threading.Thread):
    def __init__(self):
        self.idleTimer = Timer()
        self.halt = False
        self.trigger = threading.Event()
        self.is_idle = True

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join()

    def run(self):
        while not self.halt:
            try:
                if playerManager.is_active() and (
                    not settings.idle_when_paused or not playerManager.is_paused()
                ):
                    if not playerManager.is_paused():
                        self.send_timeline()
                    if self.is_idle and settings.idle_ended_cmd:
                        execute_command(settings.idle_ended_cmd)
                    self.delay_idle()

                # Dynamic interval based on playback state
                if playerManager.is_not_paused():
                    interval = 5  # Playing - normal update frequency
                elif playerManager.is_paused():
                    interval = 10  # Paused - reduced update frequency
                else:
                    interval = 5  # Stopped/unknown - default frequency

                if (
                    self.idleTimer.elapsed() > settings.idle_cmd_delay
                    and not self.is_idle
                ):
                    if (
                        settings.idle_when_paused
                        and settings.stop_idle
                        and playerManager.has_video()
                    ):
                        playerManager.stop()
                    if settings.idle_cmd:
                        execute_command(settings.idle_cmd)
                    self.is_idle = True
            except (BrokenPipeError, OSError):
                # MPV terminated, exit gracefully
                log.debug("MPV connection lost, timeline thread exiting")
                break
            if self.trigger.wait(interval):
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    @staticmethod
    def send_timeline():
        try:
            # Send_timeline sometimes (once every couple hours) gets a 404 response from Jellyfin.
            # Without this try/except that would cause this entire thread to crash keeping it from self-healing.
            playerManager.send_timeline()
        except jellyfin_apiclient_python.exceptions.HTTPException as e:
            log.warning(
                f"Failed to send timeline update to Jellyfin server: {e}. "
                "This is an expected occasional issue and will self-heal."
            )
        except _mpv_errors:
            pass


timelineManager = TimelineManager()
