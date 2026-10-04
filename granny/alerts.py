"""Independent Telegram sends with durable incident deduplication and demo mode."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import queue
import sqlite3
import threading
import time

import telegram_setup
from .calls import TwilioClient, TwilioError, load_config as load_call_config, validate_config
from .delivery import DeliveryProgress, CALL_DETAILS


class AlertDispatcher:
    def __init__(self, directory, telegram=False, config=None, client=None,
                 calls=False, call_config=None, call_client=None, call_status_client=None, call_relay=None,
                 call_retry_seconds=30, call_poll_seconds=2):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "alerts.sqlite3"
        self.telegram = telegram
        self.config = config if config is not None else (telegram_setup.load_config() if telegram else {})
        self.client = client or telegram_setup.api
        self.calls = calls
        self.call_config = (validate_config(call_config if call_config is not None else load_call_config(),
                                            require_recipients=True) if calls else {})
        self.call_client = call_client or (TwilioClient(self.call_config).create_call if calls else None)
        self.call_status_client = call_status_client or (
            TwilioClient(self.call_config).call_status if calls and call_client is None else None)
        self.call_relay = call_relay
        self.events = queue.Queue()
        self.progress_events = queue.Queue()
        self.progress = DeliveryProgress(self.progress_events)
        self.stopping = threading.Event()
        self.call_stops = {}
        self.call_retry_seconds = call_retry_seconds
        self.call_poll_seconds = call_poll_seconds
        self.watchers = ThreadPoolExecutor(max_workers=5, thread_name_prefix="granny-call-status")
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="granny-alert")
        self.futures = []
        self.call_futures = []
        self.lock = threading.Lock()
        if telegram and not self.config.get("recipients"):
            raise ValueError("Run python3 telegram_setup.py contacts before enabling Telegram.")
        self._sql("CREATE TABLE IF NOT EXISTS incidents "
                  "(id TEXT PRIMARY KEY, created TEXT, reason TEXT, mode TEXT, status TEXT)")
        self._sql("CREATE TABLE IF NOT EXISTS deliveries "
                  "(incident_id TEXT, channel TEXT, recipient TEXT, status TEXT, provider_id TEXT, "
                  "PRIMARY KEY (incident_id, channel, recipient))")
        self._sql("CREATE TABLE IF NOT EXISTS call_attempts "
                  "(incident_id TEXT, recipient TEXT, attempt INTEGER, provider_id TEXT, status TEXT, "
                  "PRIMARY KEY (incident_id, recipient, attempt))")

    def _sql(self, statement, parameters=()):
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            cursor = connection.execute(statement, parameters)
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()

    def submit(self, incident_id, photo, reason):
        with self.lock:
            claimed = self._sql("INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?, ?)",
                                (incident_id, datetime.now().astimezone().isoformat(), reason,
                                 ("telegram+calls-demo" if self.telegram and self.calls else "calls-demo"
                                  if self.calls else "telegram-demo" if self.telegram else "dry-run"), "queued"))
            if not claimed:
                return None
            self.call_stops[incident_id] = threading.Event()
            while len(self.call_stops) > 32:
                self.call_stops.pop(next(iter(self.call_stops))).set()
            self.progress.begin(incident_id, self.config.get("recipients", []) if self.telegram else [],
                                self.call_config.get("recipients", []) if self.calls else [])
            future = self.pool.submit(self._deliver, incident_id, photo, reason)
            self.futures.append(future)
            return future

    def _deliver(self, incident_id, photo, reason):
        try:
            if photo:
                try:
                    folder = self.directory / "incidents"
                    folder.mkdir(exist_ok=True)
                    (folder / f"{incident_id}.jpg").write_bytes(photo)
                except OSError:
                    # Storage failure must not suppress available communication channels.
                    self.events.put((incident_id, "Could not save snapshot locally; continuing notifications."))
            if not self.telegram and not self.calls:
                message = "Demo alert recorded. Telegram is OFF."
                self._sql("UPDATE incidents SET status=? WHERE id=?", ("simulated", incident_id))
                self.events.put((incident_id, message))
                return
            caption = ("GRANNY PROJECT — DEMO ALERT / TEST ONLY\n"
                       f"{reason}. Please check on the demo participant.\n"
                       f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}\n"
                       f"Incident: {incident_id[:8]}\nEmergency calling is not enabled.")
            # Deduplicate selected accounts even if configuration was edited by hand.
            recipients = {str(person["id"]): person for person in self.config["recipients"]} if self.telegram else {}

            def send(chat_id):
                self.progress.update(incident_id, "telegram", chat_id, state="sending", detail="Sending photo and message" if photo else "Sending message")
                try:
                    if photo:
                        self.client(self.config["token"], "sendPhoto",
                                    {"chat_id": chat_id, "caption": caption}, (photo, "image/jpeg"))
                    else:
                        self.client(self.config["token"], "sendMessage",
                                    {"chat_id": chat_id, "text": caption + "\nCamera image unavailable."})
                    self.progress.update(incident_id, "telegram", chat_id, state="accepted",
                                         detail="Telegram accepted the photo and message" if photo else "Telegram accepted the message")
                    return True, "", ""
                except Exception:
                    # An ambiguous timeout is not automatically retried: it could duplicate an alert.
                    self.progress.update(incident_id, "telegram", chat_id, state="unconfirmed",
                                         detail="Delivery unconfirmed; check Telegram before retrying")
                    return False, "", ""

            def call(number):
                self.progress.update(incident_id, "calls", number, state="sending", detail="Starting the phone call")
                try:
                    if not self._calls_active(incident_id):
                        self.progress.update(incident_id, "calls", number, state="canceled", detail="Calls stopped")
                        return False, "", "Calls stopped."
                    if self.call_relay:
                        result = self.call_relay.create_call(number, incident_id, reason, self.progress)
                    else:
                        self.progress.update(incident_id, "calls", number, confirmation="not-requested")
                        result = self.call_client(number, incident_id, reason)
                    state = result.get("status", "queued")
                    self.progress.update(incident_id, "calls", number, state=state,
                                         detail=CALL_DETAILS.get(state, "Call request accepted"))
                    if self.call_relay and result.get("sid"):
                        self._record_attempt(incident_id, number, 1, result["sid"], state)
                        self.call_futures.append(self.watchers.submit(
                            self._watch_family_call, incident_id, number, result["sid"], reason))
                    elif self.call_status_client and result.get("sid"):
                        self.watchers.submit(self._watch_call, incident_id, number, result["sid"])
                    if result.get("status") in {"failed", "canceled", "busy", "no-answer"}:
                        return False, result.get("sid", ""), "Twilio reports the call was not connected."
                    return True, result.get("sid", ""), ""
                except TwilioError as exc:
                    self.progress.update(incident_id, "calls", number, state="unconfirmed", detail=str(exc))
                    return False, "", str(exc)
                except Exception:
                    self.progress.update(incident_id, "calls", number, state="unconfirmed",
                                         detail="Call unconfirmed; check the phone before retrying")
                    return False, "", "Call failed or is unconfirmed; check Twilio Voice logs."

            jobs = [("calls", person["number"], call) for person in self.call_config.get("recipients", [])]
            jobs.extend(("telegram", chat_id, send) for chat_id in recipients)
            totals = {channel: sum(job[0] == channel for job in jobs) for channel in ("calls", "telegram")}
            finished = {"calls": 0, "telegram": 0}
            accepted = {"calls": 0, "telegram": 0}
            errors = []

            def summary():
                parts = []
                if self.telegram:
                    parts.append(f"Telegram accepted {accepted['telegram']}/{totals['telegram']} alerts. "
                                 + ("Check phones for receipt." if accepted['telegram'] == totals['telegram']
                                    else "Some sends failed or are unconfirmed; check phones."
                                    if finished['telegram'] == totals['telegram'] else "Sending…"))
                if self.calls:
                    parts.append(f"Twilio accepted {accepted['calls']}/{totals['calls']} calls. "
                                 + ("See Family alerts for connection and response status." if accepted['calls'] == totals['calls']
                                    else "Some calls failed or are unconfirmed; check Voice logs."
                                    if finished['calls'] == totals['calls'] else "Requesting calls…"))
                if errors:
                    parts.append(errors[0])
                return " ".join(parts)

            with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
                pending = {pool.submit(operation, recipient): (channel, recipient)
                           for channel, recipient, operation in jobs}
                for future in as_completed(pending):
                    channel, recipient = pending[future]
                    success, provider_id, detail = future.result()
                    finished[channel] += 1
                    accepted[channel] += int(success)
                    if detail:
                        errors.append(detail)
                    self._sql("INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?, ?, ?)",
                              (incident_id, channel, recipient, "accepted" if success else "failed_or_unconfirmed",
                               provider_id))
                    # Report each channel promptly, even while another channel is still waiting.
                    if finished[channel] == totals[channel]:
                        self.events.put((incident_id, summary()))
            status = "accepted" if sum(accepted.values()) == len(jobs) else "partial_or_failed"
            self._sql("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
        except Exception as exc:
            self.events.put((incident_id, f"Alert failed ({type(exc).__name__}); check local storage/network."))

    def cancel_calls(self, incident):
        """Stop future attempts. An already submitted phone call may still finish."""
        with self.lock:
            stop = self.call_stops.get(incident)
            if stop is not None:
                stop.set()

    def _calls_active(self, incident):
        stop = self.call_stops.get(incident)
        return (stop is not None and not stop.is_set() and not self.stopping.is_set()
                and not self.progress.family_confirmed(incident))

    def _wait_call(self, incident, seconds):
        deadline = time.monotonic() + seconds
        stop = self.call_stops.get(incident)
        while self._calls_active(incident):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            stop.wait(min(.2, remaining))
        return False

    def _record_attempt(self, incident, number, attempt, sid, status):
        self._sql("INSERT OR REPLACE INTO call_attempts VALUES (?, ?, ?, ?, ?)",
                  (incident, number, attempt, sid, status))

    def _watch_family_call(self, incident, number, sid, reason):
        attempt = 1
        deadline = time.monotonic() + 180
        while self._calls_active(incident):
            outcome = self.call_relay.outcome(sid)
            if not outcome:
                break
            terminal, confirmation = outcome
            if confirmation in {"coming", "unavailable"}:
                return
            if terminal:
                self._record_attempt(incident, number, attempt, sid, terminal)
                if terminal == "canceled":
                    return
                self.progress.update(incident, "calls", number, attempt=attempt, state="retry-wait",
                                     detail=f"No clear response. Calling again in {self.call_retry_seconds:g} seconds.")
                if not self._wait_call(incident, self.call_retry_seconds):
                    self.progress.update(incident, "calls", number, attempt=attempt,
                                         state="retry-stopped", detail="Further calls stopped.")
                    return
                # A late speech callback during the pause may have resolved this contact.
                latest = self.call_relay.outcome(sid)
                if latest is None:
                    break
                if latest[1] in {"coming", "unavailable"}:
                    self.progress.update(incident, "calls", number, attempt=attempt,
                                         state="retry-stopped", detail="Family response received; retries stopped.")
                    return
                attempt += 1
                self.progress.update(incident, "calls", number, attempt=attempt, state="sending",
                                     detail=f"Calling again · attempt {attempt}")
                try:
                    result = self.call_relay.create_call(number, incident, reason, self.progress, attempt=attempt)
                    sid = result["sid"]
                    self._record_attempt(incident, number, attempt, sid, result.get("status", "queued"))
                    self._sql("UPDATE deliveries SET provider_id=? WHERE incident_id=? AND channel='calls' AND recipient=?",
                              (sid, incident, number))
                except Exception:
                    self._record_attempt(incident, number, attempt, "", "unconfirmed")
                    # POST timeout may mean a call exists. Never create a duplicate blindly.
                    break
                deadline = time.monotonic() + 180
                continue
            if time.monotonic() >= deadline:
                break  # No confirmed ending: do not risk two overlapping calls.
            if not self._wait_call(incident, self.call_poll_seconds):
                return
            try:
                status = self.call_status_client(sid).get("status") if self.call_status_client else None
                self.call_relay.outcome(sid, status)
            except Exception:
                pass  # Signed callbacks can still provide a confirmed ending.
        if self._calls_active(incident):
            self.progress.update(incident, "calls", number, attempt=attempt, state="unconfirmed",
                                 detail="Call status uncertain. Retries paused; check Twilio Voice logs.")

    def _watch_call(self, incident, number, sid):
        until = time.monotonic() + 180
        while not self.stopping.wait(2) and time.monotonic() < until:
            try:
                status = self.call_status_client(sid).get("status")
                if status not in CALL_DETAILS:
                    continue
                self.progress.update(incident, "calls", number, state=status, detail=CALL_DETAILS[status])
                if status in {"completed", "failed", "busy", "no-answer", "canceled"}:
                    return
            except Exception:
                self.progress.update(incident, "calls", number, detail="Live status unavailable; check the phone")

    def close(self, block=True):
        self.stopping.set()
        with self.lock:
            for stop in self.call_stops.values():
                stop.set()
        self.pool.shutdown(wait=block, cancel_futures=False)
        self.watchers.shutdown(wait=block, cancel_futures=True)
