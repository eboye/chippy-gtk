"""Decide which state transitions deserve a desktop notification."""

from .state import Snapshot

OFFLINE_NOTIFY_AFTER = 600
_DONE_STATES = {"docked", "charging", "idle"}


class Notifier:
    def __init__(
        self,
        attention_faults: frozenset[int] = frozenset(),
        reminder_faults: frozenset[int] = frozenset(),
    ):
        self._attention = attention_faults
        self._reminders = reminder_faults
        self._was_cleaning = False
        self._offline_since: float | None = None
        self._offline_sent = False

    def events(self, prev: Snapshot, cur: Snapshot, now: float) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if cur.state == "cleaning":
            self._was_cleaning = True
        if cur.state == "offline":
            if self._was_cleaning:
                if self._offline_since is None:
                    self._offline_since = now
                elif not self._offline_sent and now - self._offline_since > OFFLINE_NOTIFY_AFTER:
                    self._offline_sent = True
                    out.append(
                        ("Vacuum is offline", "Lost contact for over 10 minutes during a clean")
                    )
            return out
        self._offline_since = None
        self._offline_sent = False
        if self._was_cleaning and cur.state in _DONE_STATES:
            self._was_cleaning = False
            area = cur.cleaned_area or prev.cleaned_area
            minutes = cur.cleaning_time or prev.cleaning_time
            out.append(("Cleaning finished", f"{area} m² in {minutes} min"))
        if cur.state == "error":
            # A clean that ends in an error is not "finished" when the robot later docks.
            self._was_cleaning = False
        if cur.state == "error" and prev.state != "error":
            out.append(("Vacuum needs help", cur.error or "Unknown error"))
        elif cur.fault in self._attention and cur.fault != prev.fault:
            out.append(("Vacuum needs attention", cur.error))
        elif cur.fault in self._reminders and cur.fault != prev.fault:
            out.append(("Vacuum reminder", cur.error))
        return out
