"""Thread-safe, recipient-level progress with no credentials in browser data."""
from copy import deepcopy
import threading


class DeliveryProgress:
    def __init__(self, events):
        self.events = events
        self.lock = threading.Lock()
        self.rows = {}

    def begin(self, incident, telegram, calls):
        rows = {}
        for channel, people in (("telegram", telegram), ("calls", calls)):
            for index, person in enumerate(people, 1):
                recipient = str(person["id"] if channel == "telegram" else person["number"])
                label = (f"Telegram contact {index}" if channel == "telegram"
                         else f"Family contact · {recipient[-4:]}")
                rows[(channel, recipient)] = {"channel": channel, "label": label, "state": "queued",
                                               "detail": "Waiting to start", "confirmation": "pending",
                                               "audio": "off", "attempt": 1}
        with self.lock:
            self.rows[incident] = rows
            # Retain recent incidents for late callbacks, with a bounded memory footprint.
            while len(self.rows) > 32:
                self.rows.pop(next(iter(self.rows)))
            self._publish(incident)

    def update(self, incident, channel, recipient, **fields):
        with self.lock:
            row = self.rows.get(incident, {}).get((channel, str(recipient)))
            if row is None:
                return
            attempt = fields.get("attempt", 1)
            if channel == "calls" and attempt < row["attempt"]:
                return  # An old call's callback cannot overwrite its replacement.
            if channel == "calls" and attempt > row["attempt"]:
                row.update(attempt=attempt, state="queued", detail="Waiting to start",
                           confirmation="pending", audio="off")
            allowed = {key: value for key, value in fields.items()
                       if key in {"state", "detail", "confirmation", "audio"}}
            stages = {"sending": 0, "queued": 1, "initiated": 2, "ringing": 3, "in-progress": 4,
                      "completed": 5, "busy": 5, "no-answer": 5, "canceled": 5, "failed": 5,
                      "retry-wait": 6, "unconfirmed": 7, "retry-stopped": 7}
            if channel == "calls" and "state" in allowed:
                old, new = row["state"], allowed["state"]
                if old != "queued" or row["detail"] != "Waiting to start":
                    if stages.get(new, -1) < stages.get(old, -1):
                        allowed.pop("state", None)
                        allowed.pop("detail", None)
                if allowed.get("state") in {"completed", "busy", "no-answer", "canceled", "failed"}:
                    if row["confirmation"] == "pending":
                        allowed.setdefault("confirmation", "no-response")
                    if row["audio"] == "waiting":
                        allowed.setdefault("audio", "unavailable")
            if all(row.get(key) == value for key, value in allowed.items()):
                return
            row.update(allowed)
            self._publish(incident)

    def family_confirmed(self, incident):
        with self.lock:
            return any(row["channel"] == "calls" and row["confirmation"] == "coming"
                       for row in self.rows.get(incident, {}).values())

    def _publish(self, incident):
        self.events.put((incident, deepcopy(list(self.rows[incident].values()))))


CALL_DETAILS = {
    "queued": "Call request accepted; waiting to dial",
    "initiated": "Dialing your family contact",
    "ringing": "Their phone is ringing",
    "in-progress": "Call connected; waiting for their response",
    "completed": "Call ended; see their response below",
    "busy": "Their phone was busy",
    "no-answer": "No answer",
    "canceled": "Call cancelled",
    "failed": "Call failed",
}
