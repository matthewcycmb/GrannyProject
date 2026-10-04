"""ElevenLabs speech API and realtime transcription; credentials stay on the Mac."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
from urllib import error, parse, request
import uuid
import wave

from telegram_setup import ssl_context
from .speech import PHRASES, CHECK_AUDIO_LIMIT, UPDATE_AUDIO_LIMIT

DIRECTORY = Path(__file__).resolve().parent.parent / ".granny"
CONFIG = DIRECTORY / "elevenlabs.json"
CACHE = DIRECTORY / "elevenlabs-voice"
ENV = DIRECTORY.parent / ".env"
MODEL = "eleven_flash_v2_5"
VOICE_SETTINGS = {"stability": .6, "similarity_boost": .75, "speed": .85}


class ElevenLabsError(Exception):
    pass


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_config(config):
    if not isinstance(config, dict):
        raise ElevenLabsError("Invalid ElevenLabs configuration")
    key = config.get("api_key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", key):
        raise ElevenLabsError("Enter a valid ElevenLabs API key privately in the setup command.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(config.get("voice_id", ""))):
        raise ElevenLabsError("Choose a valid ElevenLabs voice ID.")
    return dict(config)


def load_api_key(env_path=ENV, environ=None):
    """Process environment, then project .env. Values are never executed or expanded."""
    environ = os.environ if environ is None else environ
    key = environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        try:
            lines = Path(env_path).read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        except (OSError, UnicodeError):
            raise ElevenLabsError("Could not read the project .env file.") from None
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, separator, value = line.partition("=")
            if name.strip() != "ELEVENLABS_API_KEY":
                continue
            match = re.fullmatch(r'''(?:'([^']*)'|"([^"]*)"|([^#\s]*))\s*(?:#.*)?''', value.strip())
            if not separator or match is None:
                raise ElevenLabsError("Invalid ELEVENLABS_API_KEY entry in .env. Use one KEY=value line.")
            key = next(item for item in match.groups() if item is not None).strip()
    if not key:
        return None
    return validate_config({"api_key": key, "voice_id": "setup"})["api_key"]


def save_config(config, path=CONFIG, *, store_api_key=True):
    config = validate_config(config)
    if not store_api_key:
        config.pop("api_key")
    private_write(path, json.dumps(config, indent=2).encode())


def load_config(path=CONFIG, *, env_path=ENV, environ=None):
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ElevenLabsError("Run: .venv/bin/python elevenlabs_setup.py setup") from None
    if not isinstance(config, dict):
        raise ElevenLabsError("Invalid ElevenLabs configuration")
    key = load_api_key(env_path, environ)
    if key:
        config["api_key"] = key
    return validate_config(config)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class ElevenLabsClient:
    def __init__(self, api_key):
        self.api_key = validate_config({"api_key": api_key, "voice_id": "setup"})["api_key"]

    def _request(self, path, body=None):
        req = request.Request("https://api.elevenlabs.io" + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"xi-api-key": self.api_key, "Content-Type": "application/json"})
        opener = request.build_opener(request.HTTPSHandler(context=ssl_context()), NoRedirect())
        try:
            with opener.open(req, timeout=20) as response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ElevenLabsError("ElevenLabs response was too large.")
                return data
        except error.HTTPError as exc:
            raise ElevenLabsError(f"ElevenLabs returned HTTP {exc.code}. Check API permissions, voice access and credit.") from None
        except (OSError, error.URLError):
            raise ElevenLabsError("ElevenLabs connection failed. Check internet access; no automatic retry was made.") from None

    def voices(self):
        try:
            data = json.loads(self._request("/v2/voices?page_size=100"))
            voices = data["voices"]
            if not isinstance(voices, list):
                raise ValueError
            return [item for item in voices if isinstance(item, dict)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(item.get("voice_id", "")))]
        except (ValueError, KeyError, TypeError):
            raise ElevenLabsError("ElevenLabs returned an invalid voice list.") from None

    def synthesize(self, voice_id, text):
        validate_config({"api_key": self.api_key, "voice_id": voice_id})
        pcm = self._request(f"/v1/text-to-speech/{voice_id}?output_format=pcm_16000",
                            {"text": text, "model_id": MODEL,
                             "voice_settings": VOICE_SETTINGS})
        if len(pcm) < 3200 or len(pcm) % 2 or len(pcm) > 16000 * 2 * UPDATE_AUDIO_LIMIT:
            raise ElevenLabsError("ElevenLabs returned invalid or overly long audio.")
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(pcm)
        return output.getvalue()


def clip_name(voice_id, text):
    settings = json.dumps(VOICE_SETTINGS, sort_keys=True)
    return hashlib.sha256(f"{MODEL}|{settings}|{voice_id}|{text}".encode()).hexdigest() + ".wav"


def valid_clip(path, phrase):
    try:
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            return (audio.getnchannels() == 1 and audio.getsampwidth() == 2
                    and audio.getframerate() == 16000
                    and .1 <= duration <= (CHECK_AUDIO_LIMIT if phrase == "check" else UPDATE_AUDIO_LIMIT))
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return False


def prepare_clips(config, directory=CACHE, progress=print):
    config = validate_config(config)
    directory = Path(directory)
    client = ElevenLabsClient(config["api_key"])
    for name, text in PHRASES.items():
        path = directory / clip_name(config["voice_id"], text)
        if not valid_clip(path, name):
            private_write(path, client.synthesize(config["voice_id"], text))
            if not valid_clip(path, name):
                path.unlink(missing_ok=True)
                raise ElevenLabsError("Generated prompt is too long. Choose a faster voice and run prepare again.")
        progress(f"Ready: {name}")
    private_write(directory / "voice.json", json.dumps({"voice_id": config["voice_id"]}).encode())


def load_clips(directory=CACHE):
    directory = Path(directory)
    try:
        voice_id = json.loads((directory / "voice.json").read_text())["voice_id"]
        clips = {name: directory / clip_name(voice_id, text) for name, text in PHRASES.items()}
        if not all(valid_clip(path, name) for name, path in clips.items()):
            raise ValueError
        return clips
    except (OSError, ValueError, KeyError, TypeError):
        raise ElevenLabsError("Prepare the ElevenLabs voice first: .venv/bin/python elevenlabs_setup.py prepare") from None


class ScribeSession:
    """One incident, including repeat prompts. Only final raw cloud transcripts."""

    def __init__(self, api_key):
        from websockets.sync.client import connect
        params = parse.urlencode({"model_id": "scribe_v2_realtime", "audio_format": "pcm_16000",
                                  "language_code": "en", "commit_strategy": "vad",
                                  "vad_silence_threshold_secs": .35,
                                  "min_speech_duration_ms": 100,
                                  "keepalive_interval_ms": 1000})
        self._context = connect("wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + params,
                                  additional_headers={"xi-api-key": api_key}, ssl=ssl_context(),
                                  proxy=None, open_timeout=2, close_timeout=.2,
                                  ping_interval=2, ping_timeout=2, max_size=65536, max_queue=16)
        self.connection = self._context.__enter__()
        self._closed = False
        self._close_lock = threading.Lock()

    def send(self, pcm):
        self.connection.send(json.dumps({"message_type": "input_audio_chunk",
                                        "audio_base_64": base64.b64encode(pcm).decode(),
                                        "sample_rate": 16000}))

    def receive(self):
        try:
            data = json.loads(self.connection.recv(timeout=.02))
        except TimeoutError:
            return None
        if not isinstance(data, dict):
            raise ElevenLabsError("Invalid transcription event")
        kind = data.get("message_type", "")
        if data.get("error") or "error" in kind or kind in {"rate_limited", "quota_exceeded", "resource_exhausted"}:
            raise ElevenLabsError("ElevenLabs recognition unavailable; using local recognition.")
        if kind == "committed_transcript" and isinstance(data.get("text"), str):
            return data["text"].strip()[:500]
        return None

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._context.__exit__(None, None, None)
