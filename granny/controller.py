"""Connect the tested state machine to replaceable audio and notification adapters."""
import queue
import time
import uuid
from .core import Monitor, State
from .speech import PHRASES, alert_phrase


class Controller:
    def __init__(self, alerts, voice=None, monitor=None):
        self.monitor = monitor or Monitor()
        self.alerts = alerts
        self.voice = voice
        self.audio_status = "Ready" if voice else "Keyboard mode: O = okay, H = help"
        self.alert_status = "No incident"
        self.heard = ""
        self.snapshot = None
        self.voice_repeating = False
        self.deliveries = []
        self.call_listening = False
        self.call_audio_muted = False
        self.call_token = None
        self.family_update = ""
        self.live_snapshot = None
        self.live_snapshot_at = 0
        self.backup_ready = False
        self._backup_session = ""
        self._start_backup()

    def _start_backup(self):
        if not self.voice:
            return
        self._backup_session = uuid.uuid4().hex
        self.backup_ready = False
        self.audio_status = 'Starting local voice backup…'
        self.voice.listen_backup(self._backup_session)

    def announce(self, incident_id, phrase, repeat_until_ack=False):
        if self.voice:
            self._backup_session = ""
            self.backup_ready = False
            try:
                self.voice.announce(incident_id, phrase, repeat_until_ack=repeat_until_ack)
                self.voice_repeating = repeat_until_ack
                self.audio_status = "Speaking: " + PHRASES[phrase]
            except Exception:
                self.voice_repeating = False
                self.audio_status = "Announcement unavailable; check notification status."

    def actions(self, actions, now, snapshot=None):
        for action in actions:
            if action.kind == "prompt":
                self._backup_session = ""
                self.backup_ready = False
                self.audio_status = 'Starting voice check…' if self.voice else 'Button mode: I am okay or Help'
                self.call_audio_muted = False
                self.family_update = ""
                self.deliveries = []
                self.snapshot = snapshot
                self.heard = ""
                self.alert_status = "Waiting for response"
                if self.voice:
                    self.voice.start(action.incident_id)
                else:
                    self.monitor.prompt_finished(now, action.incident_id)
            elif action.kind == "repeat":
                if self.voice:
                    self.voice.repeat()
            elif action.kind == "cancel":
                if self.voice:
                    self.voice.stop()
                self.audio_status = "Voice check finished"
                self.alert_status = "Cancelled before sending"
                self.announce(action.incident_id, "cancel")
            elif action.kind == "alert":
                self.audio_status = "Voice check finished"
                self.alert_status = "Dispatching demo alert"
                try:
                    submitted = self.alerts.submit(action.incident_id, self.snapshot, action.reason)
                except Exception as exc:
                    self.alert_status = f"Alert could not be queued ({type(exc).__name__})"
                    self.announce(action.incident_id, "failed", repeat_until_ack=True)
                else:
                    # Queue both alert channels before any audio/network teardown.
                    if self.voice:
                        self.voice.stop()
                    if submitted is not None:
                        self.announce(action.incident_id, alert_phrase(self.alerts.telegram, self.alerts.calls),
                                      repeat_until_ack=True)

    def observe(self, pose, now, snapshot=None, unavailable="Body not fully visible"):
        self.live_snapshot, self.live_snapshot_at = snapshot, now
        self.actions(self.monitor.observe(pose, now, unavailable), now, snapshot)

    def request_help(self, now, source='Voice backup'):
        if self.monitor.state == State.ALERTED:
            return
        if self.monitor.state != State.CHECKING:
            self.snapshot = self.live_snapshot if now - self.live_snapshot_at <= 1 else None
            self.deliveries = []
            self.family_update = ''
            self.call_audio_muted = False
        self._backup_session = ''
        self.backup_ready = False
        self.heard = f'{source}: help'
        self.actions(self.monitor.request_help(now), now)

    def begin_check(self, now, snapshot=None):
        self.actions(self.monitor.begin_check(now), now, snapshot)

    def respond(self, text, now, confidence=1.0, source="Keyboard"):
        if self.monitor.state != State.CHECKING:
            return
        self.heard = f"{source}: {text}"
        self.actions(self.monitor.respond(text, confidence, now, self.monitor.incident_id), now)

    def tick(self, now):
        relay = getattr(self.alerts, "call_relay", None)
        if relay is not None and isinstance(getattr(relay, "events", None), queue.Queue):
            from .call_relay import AudioRequest
            while True:
                try:
                    event = relay.events.get_nowait()
                except queue.Empty:
                    break
                if isinstance(event, AudioRequest):
                    allowed = (event.incident == self.monitor.incident_id
                               and self.monitor.state == State.ALERTED and not self.call_audio_muted
                               and time.monotonic() < event.expires)
                    if allowed:
                        if self.voice:
                            self.voice.stop()
                        self.voice_repeating = False
                        self.call_listening = True
                        self.call_token = event.token
                        self.audio_status = "Listening to the family call. The Mac microphone is off."
                    event.allowed = allowed
                    event.done.set()
                elif event[0] == self.monitor.incident_id and event[1] == self.call_token:
                    self.call_listening = False
                    self.call_token = None
                    self.audio_status = "Call audio finished. Check Family alerts for their response."
        progress = getattr(self.alerts, "progress_events", None)
        if isinstance(progress, queue.Queue):
            while True:
                try:
                    incident, rows = progress.get_nowait()
                except queue.Empty:
                    break
                if incident == self.monitor.incident_id:
                    self.deliveries = rows
        if self.voice:
            while True:
                try:
                    event = self.voice.events.get_nowait()
                except queue.Empty:
                    break
                if event.kind.startswith('backup_'):
                    if not self._backup_session or event.incident_id != self._backup_session:
                        continue
                    if event.kind == 'backup_help':
                        from .audio import URGENT_COMMANDS
                        from .core import normalize_speech
                        if (now - event.at <= 2 and normalize_speech(event.text) in URGENT_COMMANDS
                                and (event.confidence is None or event.confidence >= .75)):
                            self.request_help(now, source='Local voice backup')
                        else:
                            self._backup_session = ''
                            self.backup_ready = False
                    else:
                        self.backup_ready = event.kind == 'backup_ready'
                        self.audio_status = event.text
                    continue
                if event.incident_id != self.monitor.incident_id:
                    continue
                if event.kind in {"announcement", "announcement_error", "acknowledged"} and self.monitor.state in {State.ALERTED, State.COOLDOWN}:
                    if self.monitor.state == State.ALERTED and not self.voice_repeating:
                        continue
                    if event.kind == "acknowledged":
                        self.heard = "Acknowledged locally: " + event.text
                        self.stop_voice()
                    else:
                        self.audio_status = event.text
                        if event.kind == "announcement_error":
                            self.voice_repeating = False
                    continue
                if self.monitor.state != State.CHECKING:
                    continue
                if event.kind == "ready":
                    self.monitor.prompt_finished(event.at, event.incident_id)
                    self.audio_status = event.text
                elif event.kind == "urgent":
                    from .audio import URGENT_COMMANDS
                    from .core import normalize_speech
                    if normalize_speech(event.text) in URGENT_COMMANDS:
                        self.heard = f"{event.source}: {event.text} (urgent command)"
                        self.actions(self.monitor.respond("help", 1, event.at, event.incident_id), now)
                elif event.kind == "speech":
                    confidence = event.confidence
                    if confidence is None:
                        self.heard = f"{event.source}: {event.text} (final transcript)"
                        # Final cloud text passes the same explicit phrase/negation rules;
                        # the provider has no confidence field to apply a score threshold to.
                        confidence = 1.0
                    else:
                        self.heard = f"Heard: {event.text} ({confidence:.0%})"
                    self.actions(self.monitor.respond(event.text, confidence, event.at,
                                                       event.incident_id), now)
                else:
                    self.audio_status = event.text
        self.actions(self.monitor.tick(now), now)
        if (self.voice and not self._backup_session and
                self.monitor.state in {State.UNCALIBRATED, State.MONITORING, State.POSSIBLE_FALL, State.COOLDOWN}
                and not self.voice.is_busy()):
            self._start_backup()
        if relay is not None and self.voice_repeating and not self.call_listening:
            calls = [row for row in self.deliveries if row["channel"] == "calls"]
            outcome = ""
            if any(row.get("confirmation") == "coming" for row in calls):
                outcome = "family_confirmed"
            elif calls and all(row.get("state") in {"completed", "failed", "busy", "no-answer", "canceled"}
                               for row in calls):
                outcome = "family_unconfirmed"
            if outcome and outcome != self.family_update:
                self.family_update = outcome
                self.announce(self.monitor.incident_id, outcome, repeat_until_ack=True)
        while True:
            try:
                incident_id, message = self.alerts.events.get_nowait()
            except queue.Empty:
                break
            if incident_id == self.monitor.incident_id:
                self.alert_status = message

    def stop_voice(self):
        if self.monitor.state != State.ALERTED or not self.voice_repeating:
            return False
        self.voice_repeating = False
        if self.voice:
            self.voice.stop()
        self.audio_status = "Voice stopped. Previously sent alerts remain active."
        return True

    def stop_call_audio(self):
        if not self.call_listening:
            return False
        self.call_audio_muted = True
        self.call_listening = False
        self.call_token = None
        self.alerts.call_relay.mute(self.monitor.incident_id)
        self.audio_status = "Call audio muted on this Mac. The phone call continues."
        return True

    def reset(self):
        cancel_calls = getattr(self.alerts, "cancel_calls", None)
        if cancel_calls:
            cancel_calls(self.monitor.incident_id)
        relay = getattr(self.alerts, "call_relay", None)
        if relay is not None:
            relay.mute(self.monitor.incident_id)
        if self.voice:
            self.voice.stop()
        self.monitor.reset()
        self.voice_repeating = False
        self.call_listening = False
        self.call_token = None
        self.audio_status = "Ready" if self.voice else "Keyboard mode: O = okay, H = help"
        self.deliveries = []
        self.heard = ""
        self.snapshot = None
        self.alert_status = "Reset. Call retries stopped. Previously sent messages cannot be recalled."
        self.live_snapshot = None
        self._start_backup()
