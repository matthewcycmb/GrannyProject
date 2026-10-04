"""Exercise the actual pose and speech models using public/synthetic fixtures.

Run from a normal Mac terminal: .venv/bin/python verify_runtime.py
Does not use the camera, record your microphone, or send any notifications.
"""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
import wave

from granny.audio import VoiceCheck, new_recognizer, transcript
from granny.core import Response, classify_response
from granny.vision import PoseTracker
from setup_models import POSE_MODEL, SPEECH_MODEL, ROOT
from telegram_setup import ssl_context

FIXTURES = ROOT / ".granny" / "test-media"


def main():
    import cv2
    import numpy as np
    FIXTURES.mkdir(parents=True, exist_ok=True)
    sample = FIXTURES / "pose.jpg"
    if not sample.exists():
        with urlopen("https://storage.googleapis.com/mediapipe-assets/pose.jpg",
                     context=ssl_context(), timeout=30) as response:
            sample.write_bytes(response.read())
    report = {}
    tracker = PoseTracker(POSE_MODEL)
    try:
        pose, landmarks, _ = tracker.process(np.zeros((480, 640, 3), dtype=np.uint8), 0)
        assert pose is None and not landmarks, "Blank frame produced a person"
        frame = cv2.imread(str(sample))
        started = time.perf_counter()
        for index in range(1, 11):
            pose, landmarks, _ = tracker.process(frame, index / 10)
            assert pose is not None and len(landmarks) == 33, "Known person was not tracked"
        report["pose_model"] = {"blank_frame": "pass", "known_person_10_frames": "pass",
                                "frames_per_second": round(10 / (time.perf_counter() - started), 1)}
        print("PASS: real pose model, blank image and known person", flush=True)
    finally:
        tracker.close()
    # A repeated public sample tests real video decoding and the full GUI loop.
    video = cv2.VideoWriter(str(FIXTURES / "sample.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                            10, (frame.shape[1], frame.shape[0]))
    assert video.isOpened(), "Could not create video fixture"
    for _ in range(220):
        video.write(frame)
    video.release()
    voice = VoiceCheck(SPEECH_MODEL)
    cases = [("okay", "I am okay", Response.OK),
             ("im-okay", "I'm okay", Response.OK),
             ("not-okay", "I am not okay", Response.HELP),
             ("cannot-get-up", "I can't get up", Response.HELP),
             ("help", "Help me", Response.HELP),
             ("no", "No", Response.HELP),
             ("unrelated", "Please turn on the television", Response.UNKNOWN)]
    report["speech_model"] = []
    for name, phrase, expected in cases:
        aiff, wav = FIXTURES / f"{name}.aiff", FIXTURES / f"{name}.wav"
        subprocess.run(["/usr/bin/say", "-r", "150", "-o", str(aiff), phrase], check=True, timeout=15)
        subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        str(aiff), str(wav)], check=True, timeout=15)
        with wave.open(str(wav), "rb") as audio:
            assert audio.getnframes() > 0, "macOS speech services unavailable (empty audio file)"
            # Real microphone input has a short lead-in before the person speaks.
            samples = bytes(16000) + audio.readframes(audio.getnframes()) + bytes(32000)
        recognizer = new_recognizer(voice.model)
        results = []
        for offset in range(0, len(samples), 3200):
            if recognizer.AcceptWaveform(samples[offset:offset + 3200]):
                results.append(transcript(recognizer.Result()))
        results.append(transcript(recognizer.FinalResult()))
        text, confidence = max(results, key=lambda item: len(item[0]))
        actual = classify_response(text, confidence)
        print(f"{name}: {text!r}, confidence {confidence:.2f}, decision {actual.value}", flush=True)
        assert actual == expected, f"Speech case {name}: expected {expected.value}, got {actual.value}"
        report["speech_model"].append({"case": name, "recognized": text, "decision": actual.value})
    recognizer = new_recognizer(voice.model)
    recognizer.AcceptWaveform(bytes(16000 * 2 * 3))
    text, confidence = transcript(recognizer.FinalResult())
    assert classify_response(text, confidence) == Response.UNKNOWN
    report["silence"] = "pass"
    (FIXTURES / "runtime-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: actual model checks. No camera/microphone capture or Telegram sends.", flush=True)


if __name__ == "__main__":
    main()
