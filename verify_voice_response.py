"""Replay real PCM through the real Vosk worker; no microphone or notifications."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from unittest.mock import patch
import wave

from granny.audio import VoiceCheck
from granny.core import Response, classify_response
from setup_models import SPEECH_MODEL

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / '.granny/test-media'


def pcm(name, text):
    wav = MEDIA / (name + '.wav')
    subprocess.run(['/usr/bin/say', '-r', '155', '-o', str(MEDIA / (name + '.aiff')), text],
                   check=True, timeout=15)
    subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1',
                    str(MEDIA / (name + '.aiff')), str(wav)], check=True, timeout=15)
    with wave.open(str(wav), 'rb') as stream:
        audio = stream.readframes(stream.getnframes())
    if not audio or not any(audio):
        raise RuntimeError('Mac speech synthesis produced no audio; rerun with access to macOS speech services.')
    return audio


class Speaker:
    seconds = 0
    def speak(self, _phrase, stop):
        stop.wait(self.seconds)
    def stop(self):
        pass


def main():
    MEDIA.mkdir(parents=True, exist_ok=True)
    speaker = Speaker()
    voice = VoiceCheck(SPEECH_MODEL, speaker=speaker)
    report = {'scope': 'Real recognizer with synthesized PCM replay; not a room accuracy estimate', 'cases': []}
    cases = [('help-interrupt', 'Help', Response.HELP, True),
             ('help-boundary', 'Help', Response.HELP, True),
             ('help-pause', 'Help me', Response.HELP, False),
             ('help-repeated-interrupt', 'Help help help!', Response.HELP, True),
             ('help-polite-pause', 'Help me please!', Response.HELP, False),
             ('help-now-pause', 'Please help me now!', Response.HELP, False),
             ('negative-reply', 'I am not okay', Response.HELP, False),
             ('safe-reply', 'I am okay', Response.OK, False),
             ('prompt-echo', 'Are you okay?', Response.UNKNOWN, True),
             ('unrelated-reply', 'Please turn on the television', Response.UNKNOWN, False)]
    for name, phrase, expected, during_prompt in cases:
        audio = pcm(name, phrase)
        lead = .35 if during_prompt else .65
        end_of_phrase = lead + len(audio) / 32000
        speaker.seconds = end_of_phrase + .5 if during_prompt else .05
        if name == 'help-boundary':
            speaker.seconds = .5  # Playback ends halfway through the word.
        samples = bytes(round(lead * 32000)) + audio + bytes(64000)
        ended = threading.Event()
        began = []

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs['callback']
            def __enter__(self):
                def feed():
                    began.append(time.monotonic())
                    for offset in range(0, len(samples), 1600):
                        if ended.is_set():
                            break
                        self.capture(samples[offset:offset+1600], 800, None, None)
                        ended.wait(.05)
                self.thread = threading.Thread(target=feed, daemon=True)
                self.thread.start()
                return self
            def __exit__(self, *_):
                ended.set()
                self.thread.join(1)

        events = []
        with patch('sounddevice.RawInputStream', Microphone):
            try:
                voice.start(name)
                deadline = time.monotonic() + len(samples) / 32000 + 1
                while time.monotonic() < deadline:
                    try:
                        event = voice.events.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if event.incident_id != name:
                        continue
                    if event.kind == 'error':
                        raise AssertionError(event.text)
                    if event.kind in {'speech', 'urgent'}:
                        decision = Response.HELP if event.kind == 'urgent' else classify_response(event.text, event.confidence)
                        events.append((event, decision))
                        if decision == expected and expected != Response.UNKNOWN:
                            break
            finally:
                voice.stop()
        if expected == Response.UNKNOWN:
            assert not events, (name, [(e.text, d.value) for e,d in events])
            row = {'case': name, 'decision': 'no action'}
        else:
            assert events and events[0][1] == expected, (name, events)
            event = events[0][0]
            row = {'case': name, 'decision': expected.value, 'recognized': event.text,
                   'event': event.kind, 'seconds_after_phrase_end': round(event.at-began[0]-end_of_phrase, 3)}
            if during_prompt and name != 'help-boundary':
                assert event.at-began[0] < speaker.seconds, 'Help waited until prompt finished'
        report['cases'].append(row)
        print('PASS:', json.dumps(row), flush=True)
    report['status'] = 'passed'
    (MEDIA/'voice-response-report.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__ == '__main__':
    main()
