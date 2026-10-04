"""Real Vosk PCM replay -> standby help -> real dispatcher with mock providers.

Uses synthesized speech, no live microphone, camera, cloud requests or recipients.
This verifies the software path, not recognition accuracy in a noisy room.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import Mock, patch

from granny.audio import VoiceCheck
from granny.alerts import AlertDispatcher
from granny.controller import Controller
from granny.core import State
from setup_models import SPEECH_MODEL
from verify_voice_response import pcm, Speaker


def main():
    voice = VoiceCheck(SPEECH_MODEL, speaker=Speaker(), cloud_key='unused-test-key')
    report = {'scope': 'Synthesized PCM, real recognizer and dispatcher; mock recipients', 'cases': [],
              'real_notifications_sent': 0}
    cases = [('backup-help', 'Help!', True), ('backup-help-me', 'Help me!', True),
             ('backup-no-help', 'I do not need help', False),
             ('backup-unrelated', 'Please turn on the television', False),
             ('backup-prompt', 'Are you okay?', False)]
    try:
        for name, text, expected in cases:
            audio = pcm(name, text)
            samples = bytes(11200) + audio + bytes(64000)
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

            with tempfile.TemporaryDirectory(prefix='granny-backup-test-') as directory:
                telegram = Mock(return_value={'message_id': 1})
                call = Mock(return_value={'sid': 'CA' + 'c'*32, 'status': 'queued'})
                alerts = AlertDispatcher(directory, telegram=True, calls=True,
                    config={'token': '0:FAKE', 'recipients': [{'id': str(i)} for i in (1, 2, 3)]}, client=telegram,
                    call_config={'account_sid': 'AC'+'a'*32, 'auth_token': 'b'*32, 'from_number': '+14155550100',
                                 'recipients': [{'number': '+16045550101'}, {'number': '+16045550102'}]}, call_client=call)
                try:
                    with patch('sounddevice.RawInputStream', Microphone), \
                            patch('granny.elevenlabs.ScribeSession') as cloud:
                        control = Controller(alerts, voice)
                        until = time.monotonic() + len(samples)/32000 + 2
                        while time.monotonic() < until:
                            now = time.monotonic()
                            control.observe(None, now, unavailable='Camera disconnected')
                            control.tick(now)
                            if control.monitor.state == State.ALERTED:
                                break
                            time.sleep(.01)
                        recognized_at = time.monotonic()
                        voice.stop()
                        assert (control.monitor.state == State.ALERTED) == expected, (name, control.heard)
                        cloud.assert_not_called()
                        for future in alerts.futures:
                            future.result(timeout=3)
                        assert call.call_count == (2 if expected else 0)
                        assert telegram.call_count == (3 if expected else 0)
                        if expected:
                            assert all(item.args[1] == 'sendMessage' for item in telegram.call_args_list)
                            delay = recognized_at - began[0] - .35 - len(audio)/32000
                            assert delay < 1.5, (name, delay)
                            control.request_help(time.monotonic())
                            assert call.call_count == 2 and telegram.call_count == 3
                        row = {'case': name, 'alerted': expected, 'mock_calls': call.call_count,
                               'mock_messages': telegram.call_count}
                        if expected:
                            row['seconds_after_phrase_end'] = round(delay, 3)
                        report['cases'].append(row)
                        print('PASS:', json.dumps(row), flush=True)
                finally:
                    voice.stop()
                    alerts.close()
        report['status'] = 'passed'
        Path('.granny/test-media/voice-backup-report.json').write_text(json.dumps(report, indent=2)+'\n')
    finally:
        voice.stop()


if __name__ == '__main__':
    main()
