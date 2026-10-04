"""Exercise the microphone worker with slow cloud and prompt playback."""
import json
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from granny.audio import VoiceCheck, VoiceEvent, FastHelp, is_help_command
from granny.controller import Controller
from granny.core import State
from granny.speech import PHRASES


class FastVoiceTests(unittest.TestCase):
    def test_partial_help_must_be_stable_and_partial_okay_never_cancels(self):
        fast = FastHelp()
        for text in ('I am okay', 'no help needed', 'helpful', ''):
            self.assertFalse(fast.update(text, 0))
            self.assertFalse(fast.update(text, 1))
        self.assertFalse(fast.update('help', 2))
        self.assertFalse(fast.update('help', 2.05))
        self.assertTrue(fast.update('help', 2.2))
        self.assertFalse(fast.update('help', 2.4))

    def test_transient_help_then_corrected_partial_does_not_alert(self):
        fast = FastHelp()
        self.assertFalse(fast.update('help', 0))
        self.assertFalse(fast.update('hello', .1))
        self.assertFalse(fast.update('help', .2))

    def test_repeated_help_does_not_restart_the_urgent_timer(self):
        fast = FastHelp(.25)
        self.assertFalse(fast.update('help', 0))
        self.assertFalse(fast.update('help help', .15))
        self.assertTrue(fast.update('help help help', .3))

    def test_polite_help_phrase_can_grow_without_losing_the_command(self):
        fast = FastHelp(.25)
        self.assertFalse(fast.update('help me', 0))
        self.assertTrue(fast.update('help me please', .3))

    def test_complete_help_requests_only(self):
        for phrase in ('help help help', 'help me please', 'please help me now',
                       'help help me please help', 'i need your help', 'help now'):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_help_command(phrase))
        for phrase in ('no help needed', 'i do not need help', 'help is not needed',
                       'helpful', 'hello', 'can you help with homework', 'help me no',
                       'the television said help', '[unk] help', 'help [unk]'):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_help_command(phrase))

    def test_urgent_voice_dispatches_before_audio_shutdown_and_only_once(self):
        order = []
        voice = Mock()
        voice.events = queue.Queue()
        voice.stop.side_effect = lambda: order.append('stop voice')
        alerts = Mock(telegram=True, calls=True)
        alerts.events = queue.Queue()
        alerts.submit.side_effect = lambda *_: order.append('dispatch') or object()
        control = Controller(alerts, voice)
        control.begin_check(0)
        for _ in range(2):
            voice.events.put(VoiceEvent('urgent', control.monitor.incident_id, 1, 'help', None))
        control.tick(1)
        self.assertEqual(control.monitor.state, State.ALERTED)
        self.assertEqual(order[0], 'dispatch')
        alerts.submit.assert_called_once()

    def test_help_during_prompt_does_not_wait_for_cloud_connection(self):
        release = threading.Event()
        producing = threading.Event()
        speaker = Mock()
        speaker.speak.side_effect = lambda _, stop: stop.wait(3)
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = False
        recognizer.PartialResult.return_value = json.dumps({'partial': 'help'})

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs['callback']
            def __enter__(self):
                def feed():
                    while not producing.wait(.05):
                        self.capture(bytes(1600), 800, None, None)
                self.worker = threading.Thread(target=feed, daemon=True)
                self.worker.start()
                return self
            def __exit__(self, *_):
                producing.set()
                self.worker.join(1)

        def delayed_connection(*_):
            release.wait(3)
            return Mock(receive=Mock(return_value=None))

        with patch('vosk.Model'), patch('granny.audio.new_recognizer', return_value=recognizer), \
                patch('sounddevice.RawInputStream', Microphone), \
                patch('granny.elevenlabs.ScribeSession', side_effect=delayed_connection):
            voice = VoiceCheck('unused', speaker=speaker, cloud_key='test-only')
            try:
                started = time.monotonic()
                voice.start('incident')
                while time.monotonic() - started < 1:
                    event = voice.events.get(timeout=1)
                    if event.kind == 'urgent':
                        self.assertEqual(event.text, 'help')
                        self.assertLess(time.monotonic() - started, .8)
                        break
                else:
                    self.fail('Help waited for the network or speaker')
            finally:
                voice.stop()
                release.set()

    def test_prompt_echo_cannot_cancel_and_is_not_sent_to_cloud(self):
        capture_done = threading.Event()
        speaker = Mock()
        speaker.speak.side_effect = lambda _, stop: stop.wait(2)
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.return_value = json.dumps({'text':'i am okay','result':[{'conf':1}]})
        cloud = Mock(receive=Mock(return_value=None))

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs['callback']
            def __enter__(self):
                self.capture(b'\x01' * 1600, 800, None, None)
                capture_done.set()
                return self
            def __exit__(self, *_):
                pass

        with patch('vosk.Model'), patch('granny.audio.new_recognizer', return_value=recognizer), \
                patch('sounddevice.RawInputStream', Microphone), \
                patch('granny.elevenlabs.ScribeSession', return_value=cloud):
            voice = VoiceCheck('unused', speaker=speaker, cloud_key='test-only')
            try:
                voice.start('incident')
                self.assertTrue(capture_done.wait(1))
                until = time.monotonic() + 1
                while not cloud.send.called and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(cloud.send.called)
                self.assertEqual(cloud.send.call_args.args[0], bytes(1600))
                self.assertFalse(any(e.kind in {'speech','urgent'} for e in voice.events.queue))
                self.assertNotIn('help', PHRASES['check'].lower())
            finally:
                voice.stop()


if __name__ == '__main__':
    unittest.main()
