"""Fixed demo phrases and interruptible local playback of Mac or cached AI speech."""
from pathlib import Path
import subprocess
import threading

CHECK_AUDIO_LIMIT = 10
UPDATE_AUDIO_LIMIT = 24
MAC_WORDS_PER_MINUTE = 135

PHRASES = {
    # Keep the prompt short and omit the urgent keyword: local help recognition
    # remains active during this prompt without hearing its own command.
    "check": "Are you okay?",
    "cancel": "Okay. I have cancelled the alert.",
    "dry_run": "Demo alert triggered. No messages or calls are being sent.",
    "telegram": "Your alert has started. I am sending your family a photo and message.",
    "calls": "Your alert has started. I am calling your family now.",
    "both": "Your alert has started. I am calling your family and sending them a photo and message.",
    "failed": "I could not start the family alert. Please use another way to call for help.",
    "family_confirmed": "A family member has confirmed that they can come and check on you.",
    "family_unconfirmed": "Your family has not confirmed that they can come. Please use another way to ask for help.",
}


def alert_phrase(telegram, calls):
    return "both" if telegram and calls else "telegram" if telegram else "calls" if calls else "dry_run"


class Speaker:
    def __init__(self, clips=None):
        self.clips = clips or {}
        self._process = None
        self._lock = threading.Lock()

    def stop(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()

    def _play(self, command, stop, timeout):
        with self._lock:
            if stop.is_set():
                return
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._process = process
        try:
            code = process.wait(timeout=timeout)
            if code and not stop.is_set():
                raise RuntimeError("Speaker playback failed")
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("Speaker playback timed out") from None
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

    def speak(self, phrase, stop):
        timeout = (CHECK_AUDIO_LIMIT if phrase == "check" else UPDATE_AUDIO_LIMIT) + 1
        clip = self.clips.get(phrase)
        if clip is not None and Path(clip).is_file():
            try:
                self._play(["/usr/bin/afplay", str(clip)], stop, timeout)
                return
            except (OSError, RuntimeError):
                if stop.is_set():
                    return
        # Playback has no API dependency. Missing/broken AI clips use the Mac voice.
        self._play(["/usr/bin/say", "-r", str(MAC_WORDS_PER_MINUTE), PHRASES[phrase]], stop, timeout)
