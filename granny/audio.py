"""Local standby help detection and response-window recognition."""
from dataclasses import dataclass
import json
import queue
import threading
import time

from .speech import PHRASES, Speaker
from .core import acknowledges_update, normalize_speech, classify_response, Response

GRAMMAR = ["i am okay", "i'm okay", "i am ok", "i am fine", "i am okay thank you",
           "i am not okay", "i'm not okay", "not okay", "no", "help", "help me",
           "i cannot get up", "i can't get up", "i am hurt", "i am in pain",
           "okay", "ok", "thank you", "okay thank you", "ok thank you", "stop voice", "stop speaking", "[unk]"]
PROMPT = PHRASES["check"]


@dataclass(frozen=True)
class VoiceEvent:
    kind: str
    incident_id: str
    at: float
    text: str = ""
    confidence: float | None = 0.0
    source: str = "Local"


def new_recognizer(model, sample_rate=16000):
    from vosk import KaldiRecognizer
    recognizer = KaldiRecognizer(model, sample_rate, json.dumps(GRAMMAR))
    recognizer.SetWords(True)
    # Word alignment delays short partials until the endpoint. We only need
    # partial text for the urgent keyword; final results still include confidence.
    recognizer.SetPartialWords(False)
    return recognizer


URGENT_COMMANDS = {"help", "help me", "please help", "please help me", "i need help"}
BACKUP_GRAMMAR = sorted(URGENT_COMMANDS | {
    '[unk]', 'hello', 'hello there', 'helpful', 'no help', 'no help needed',
    'i do not need help', "i don't need help", 'i am okay', 'i am fine',
})


class FastHelp:
    """Act only on a stable explicit help command; never cancel from partial text."""
    def __init__(self, stable_seconds=.12):
        self.text = ""
        self.since = None
        self.sent = False
        self.stable_seconds = stable_seconds

    def update(self, text, now):
        text = normalize_speech(text)
        if text not in URGENT_COMMANDS:
            self.text, self.since = "", None
            return False
        if text != self.text:
            self.text, self.since = text, now
            return False
        if not self.sent and self.since is not None and now - self.since >= self.stable_seconds:
            self.sent = True
            return True
        return False


def transcript(result):
    data = json.loads(result)
    words = data.get("result", [])
    # Use phrase confidence: short words such as "I" can score below the rest of
    # an otherwise clear phrase. Exact wording and negation checks remain separate.
    confidence = sum(word.get("conf", 0.0) for word in words) / len(words) if words else 0.0
    return data.get("text", ""), confidence


class VoiceCheck:
    def __init__(self, model_path, device=None, speaker=None, cloud_key=None, acknowledgement_seconds=5):
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        self.model = Model(str(model_path))
        self.device = device
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._repeat = threading.Event()
        self._thread = None
        self.speaker = speaker or Speaker()
        self.cloud_key = cloud_key
        self._cloud = None
        self._cloud_lock = threading.Lock()
        self._microphone_lock = threading.Lock()
        self.acknowledgement_seconds = acknowledgement_seconds

    def is_busy(self):
        return self._thread is not None and self._thread.is_alive()

    def listen_backup(self, session_id):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._backup, args=(session_id, self._stop), daemon=True)
        self._thread.start()

    def _backup(self, session_id, stop):
        """No camera or cloud dependency; microphone is shared with voice checks.

        Include unknown speech and explicit negative phrases so they compete
        with the help keyword. Only complete explicit help phrases can act.
        """
        import sounddevice as sd
        from vosk import KaldiRecognizer
        while not stop.is_set():
            try:
                recognizer = KaldiRecognizer(self.model, 16000, json.dumps(BACKUP_GRAMMAR))
                recognizer.SetWords(True)
                recognizer.SetPartialWords(False)
                fast = FastHelp(stable_seconds=.25)
                chunks = queue.Queue(maxsize=6)

                def capture(indata, _frames, _timing, _status):
                    try:
                        chunks.put_nowait(bytes(indata))
                    except queue.Full:
                        # Do not let old background audio become a later alert.
                        try:
                            chunks.get_nowait()
                            chunks.put_nowait(bytes(indata))
                        except (queue.Empty, queue.Full):
                            pass

                with self._microphone_lock:
                    if stop.is_set():
                        return
                    with sd.RawInputStream(samplerate=16000, blocksize=800, device=self.device,
                                           dtype='int16', channels=1, callback=capture):
                        self._emit('backup_ready', session_id, 'Voice backup ON: say help to alert your family immediately.')
                        while not stop.is_set():
                            try:
                                chunk = chunks.get(timeout=.05)
                            except queue.Empty:
                                continue
                            if recognizer.AcceptWaveform(chunk):
                                text, confidence = transcript(recognizer.Result())
                                urgent = normalize_speech(text) in URGENT_COMMANDS and confidence >= .75
                                fast = FastHelp(stable_seconds=.25)
                            else:
                                text = json.loads(recognizer.PartialResult()).get('partial', '')
                                confidence = None
                                urgent = fast.update(text, time.monotonic())
                            if urgent and not stop.is_set():
                                self._emit('backup_help', session_id, text, confidence, 'Local voice backup')
                                return
            except Exception as exc:
                if not stop.is_set():
                    self._emit('backup_error', session_id,
                               f'Voice backup unavailable ({type(exc).__name__}); use the Help button. Retrying…')
                    stop.wait(3)

    def start(self, incident_id):
        self.stop()
        self._stop = threading.Event()
        self._repeat = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(incident_id, self._stop, self._repeat), daemon=True)
        self._thread.start()

    def announce(self, incident_id, phrase, repeat_until_ack=False):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._announcement,
                                        args=(incident_id, phrase, self._stop, repeat_until_ack), daemon=True)
        self._thread.start()

    def _announcement(self, incident_id, phrase, stop, repeat_until_ack=False):
        try:
            while not stop.is_set():
                with self._microphone_lock:
                    if stop.is_set():
                        return
                    self._emit("announcement", incident_id, "Speaking: " + PHRASES[phrase])
                    self.speaker.speak(phrase, stop)
                if stop.is_set():
                    return
                if not repeat_until_ack:
                    self._emit("announcement", incident_id, "Spoken update finished")
                    return
                try:
                    answer = self._listen_for_acknowledgement(incident_id, stop)
                    if answer and not stop.is_set():
                        self._emit("acknowledged", incident_id, answer)
                        return
                except Exception:
                    # Announcements can still repeat if microphone permission fails.
                    self._emit("announcement", incident_id,
                               "Microphone unavailable. Update will repeat; press Stop voice to silence it.")
                    stop.wait(self.acknowledgement_seconds)
        except Exception:
            if not stop.is_set():
                self._emit("announcement_error", incident_id, "Announcement unavailable; check notification status.")

    def _listen_for_acknowledgement(self, incident_id, stop):
        """After dispatch, acknowledge locally. This never cancels or resends alerts."""
        import sounddevice as sd
        recognizer = new_recognizer(self.model)
        chunks = queue.Queue(maxsize=40)

        def capture(indata, _frames, _timing, _status):
            try:
                chunks.put_nowait(bytes(indata))
            except queue.Full:
                pass

        # Avoid recognizing the final sound from the speaker as an acknowledgement.
        if stop.wait(.3):
            return None
        with self._microphone_lock:
            if stop.is_set():
                return None
            with sd.RawInputStream(samplerate=16000, blocksize=1600, device=self.device,
                                   dtype="int16", channels=1, callback=capture):
                self._emit("announcement", incident_id,
                           "Listening locally: say okay to stop the repeated update, or press Stop voice.")
                until = time.monotonic() + self.acknowledgement_seconds
                while not stop.is_set() and time.monotonic() < until:
                    try:
                        chunk = chunks.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(chunk):
                        text, confidence = transcript(recognizer.Result())
                        if acknowledges_update(text, confidence):
                            return text
        return None

    def repeat(self):
        self._repeat.set()

    def stop(self):
        self._stop.set()
        self.speaker.stop()
        with self._cloud_lock:
            cloud = self._cloud
            self._cloud = None
        if cloud is not None:
            try:
                cloud.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=.4)

    def _emit(self, kind, incident_id, text="", confidence=0.0, source="Local"):
        self.events.put(VoiceEvent(kind, incident_id, time.monotonic(), text, confidence, source))

    def _prompt(self, incident_id, stop):
        if stop.is_set():
            return
        self._emit("status", incident_id, "Speaking: Are you okay? You can say help now.")
        self.speaker.speak("check", stop)
        stop.wait(.25)

    def _cloud_recognition(self, incident_id, stop, chunks, active, failed):
        """Network work cannot block the local microphone/urgent-command loop."""
        cloud = None
        try:
            from .elevenlabs import ScribeSession
            cloud = ScribeSession(self.cloud_key)
            with self._cloud_lock:
                if stop.is_set():
                    return
                self._cloud = cloud
            active.set()
            while not stop.is_set():
                try:
                    chunk = chunks.get(timeout=.05)
                except queue.Empty:
                    chunk = None
                if chunk is not None:
                    cloud.send(chunk)
                # Drain accumulated provider messages rather than one per audio block.
                for _ in range(8):
                    text = cloud.receive()
                    if text:
                        if not stop.is_set():
                            self._emit("speech", incident_id, text, None, "ElevenLabs")
                    else:
                        break
        except Exception:
            failed.set()
            if not stop.is_set():
                self._emit("status", incident_id, "Cloud recognition unavailable; local recognition is listening.")
        finally:
            active.clear()
            if cloud is not None:
                try:
                    cloud.close()
                except Exception:
                    pass
            with self._cloud_lock:
                if self._cloud is cloud:
                    self._cloud = None

    def _run(self, incident_id, stop, repeat):
        import sounddevice as sd
        chunks = queue.Queue(maxsize=20)
        cloud_chunks = queue.Queue(maxsize=20)
        cloud_active = threading.Event()
        cloud_failed = threading.Event()
        talking = threading.Event()
        prompt_done = threading.Event()
        talking.set()
        prompt_thread = None

        def latest(queue_, value):
            try:
                queue_.put_nowait(value)
            except queue.Full:
                # Prefer current speech over building seconds of microphone lag.
                try:
                    queue_.get_nowait()
                except queue.Empty:
                    pass
                try:
                    queue_.put_nowait(value)
                except queue.Full:
                    pass

        def capture(indata, _frames, _timing, status):
            if status:
                self._emit("status", incident_id, "Microphone overflow; use the Help button if needed.")
            latest(chunks, (bytes(indata), talking.is_set()))

        def prompt():
            try:
                self._prompt(incident_id, stop)
            except Exception:
                if not stop.is_set():
                    self._emit("status", incident_id, "Speaker unavailable; listening for your reply.")
            finally:
                talking.clear()
                prompt_done.set()

        try:
            recognizer = new_recognizer(self.model)
            urgent_recognizer = new_recognizer(self.model)
            fast = FastHelp()
            previous_talking = True
            with self._microphone_lock:
                if stop.is_set():
                    return
                with sd.RawInputStream(samplerate=16000, blocksize=800, device=self.device,
                                       dtype="int16", channels=1, callback=capture):
                    if self.cloud_key:
                        threading.Thread(target=self._cloud_recognition,
                                         args=(incident_id, stop, cloud_chunks, cloud_active, cloud_failed), daemon=True).start()
                    repeat.clear()
                    prompt_thread = threading.Thread(target=prompt, daemon=True)
                    prompt_thread.start()
                    while not stop.is_set():
                        if prompt_done.is_set():
                            prompt_done.clear()
                            mode = "local help + ElevenLabs" if cloud_active.is_set() else "local"
                            self._emit("ready", incident_id, f"Listening ({mode}): say I am okay, or help")
                        if repeat.is_set() and not talking.is_set():
                            repeat.clear()
                            talking.set()
                            prompt_thread = threading.Thread(target=prompt, daemon=True)
                            prompt_thread.start()
                        try:
                            chunk, during_prompt = chunks.get(timeout=.03)
                        except queue.Empty:
                            continue
                        if during_prompt != previous_talking:
                            recognizer.Reset()
                            previous_talking = during_prompt
                        # This decoder stays continuous across prompt boundaries,
                        # so a help command cannot be cut in half by playback ending.
                        urgent_final = urgent_recognizer.AcceptWaveform(chunk)
                        if urgent_final:
                            text, confidence = transcript(urgent_recognizer.Result())
                            if normalize_speech(text) in URGENT_COMMANDS and confidence >= .75:
                                if not stop.is_set() and not fast.sent:
                                    self._emit("speech", incident_id, text, confidence, "Local fast response")
                            fast = FastHelp()
                        else:
                            partial = json.loads(urgent_recognizer.PartialResult()).get("partial", "")
                            if fast.update(partial, time.monotonic()) and not stop.is_set():
                                self._emit("urgent", incident_id, partial, None, "Local keyword")
                        final = not during_prompt and recognizer.AcceptWaveform(chunk)
                        if final:
                            text, confidence = transcript(recognizer.Result())
                            response = classify_response(text, confidence)
                            if (response == Response.HELP or
                                    (response == Response.OK and (not self.cloud_key or cloud_failed.is_set()))):
                                if not stop.is_set():
                                    self._emit("speech", incident_id, text, confidence, "Local fast response")
                        if self.cloud_key:
                            # Never send our speaker prompt to the recognizer. Local
                            # explicit help remains enabled even during playback.
                            latest(cloud_chunks, bytes(len(chunk)) if during_prompt else chunk)
        except Exception as exc:
            if not stop.is_set():
                self._emit("error", incident_id,
                           f"Audio unavailable ({type(exc).__name__}). Check microphone permission; use O/H keys.")
        finally:
            stop.set()
            self.speaker.stop()
            if prompt_thread is not None:
                prompt_thread.join(timeout=.3)
