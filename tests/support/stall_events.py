"""Test-support plugin: stalls OctoPrint's event delivery on demand.

Steered through files in ``STALL_EVENTS_DIR``:

- ``block`` present: the next Upload event blocks OctoPrint's event thread until the
  file is removed, the way Backup Scheduler 0.3.0 did on PrintDone.
- ``recovery_pending`` present: the ``print_recovery_pending`` helper reports True,
  as octoprint-power-panic does while an interrupted print may still be resumed.
"""

import os
import time

import octoprint.plugin

__plugin_name__ = "Stall events (test support)"
__plugin_pythoncompat__ = ">=3.7,<4"

CONTROL = os.environ.get("STALL_EVENTS_DIR", "")


def _path(name):
    return os.path.join(CONTROL, name)


class StallEventsPlugin(octoprint.plugin.EventHandlerPlugin):
    def on_event(self, event, payload):
        if event == "Upload" and os.path.exists(_path("block")):
            self._logger.info("Blocking the event thread")
            while os.path.exists(_path("block")):
                time.sleep(0.2)
            self._logger.info("Released the event thread")


def print_recovery_pending():
    return os.path.exists(_path("recovery_pending"))


__plugin_implementation__ = StallEventsPlugin()
__plugin_helpers__ = {"print_recovery_pending": print_recovery_pending}
