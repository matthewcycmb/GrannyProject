import queue
import tempfile
import unittest
from unittest.mock import Mock

from granny.alerts import AlertDispatcher
from granny.audio import VoiceEvent
from granny.controller import Controller
from granny.core import State
from granny.web import Command, Dashboard
from test_monitor import STANDING


class VoiceBackupTests(unittest.TestCase):
    def setUp(self):
        self.voice = Mock()
        self.voice.events = queue.Queue()
        self.voice.is_busy.return_value = False
        self.alerts = Mock(telegram=True, calls=True)
        self.alerts.events = queue.Queue()
        self.controller = Controller(self.alerts, self.voice)

    def help(self, at=1, text='help', session=None, confidence=None):
        self.voice.events.put(VoiceEvent('backup_help', session or self.controller._backup_session,
                                         at, text, confidence, 'Local voice backup'))

    def test_uncalibrated_and_camera_missing_still_dispatch_immediately(self):
        self.controller.observe(None, .9, unavailable='Camera disconnected')
        self.help()
        self.controller.tick(1)
        self.assertEqual(self.controller.monitor.state, State.ALERTED)
        self.assertIsNone(self.controller.monitor.deadline)
        self.voice.start.assert_not_called()
        self.alerts.submit.assert_called_once_with(self.controller.monitor.incident_id, None,
                                                   'Person requested help')

    def test_monitoring_uses_a_fresh_photo_without_waiting_for_a_fall(self):
        self.controller.monitor.calibrate(STANDING)
        self.controller.observe(STANDING, .9, b'current camera photo')
        self.help()
        self.controller.tick(1)
        self.assertEqual(self.alerts.submit.call_args.args[1], b'current camera photo')

    def test_old_photo_is_not_sent_as_current_evidence(self):
        self.controller.observe(None, 0, b'old photo')
        self.help(at=2)
        self.controller.tick(2)
        self.assertIsNone(self.alerts.submit.call_args.args[1])

    def test_duplicate_help_and_help_after_stop_voice_never_resend(self):
        session = self.controller._backup_session
        self.help(session=session)
        self.help(session=session)
        self.controller.tick(1)
        self.controller.stop_voice()
        self.controller.request_help(2)
        self.alerts.submit.assert_called_once()

    def test_reset_changes_session_and_ignores_queued_old_speech(self):
        old = self.controller._backup_session
        self.controller.reset()
        self.assertNotEqual(old, self.controller._backup_session)
        self.help(session=old)
        self.controller.tick(1)
        self.alerts.submit.assert_not_called()
        self.help(at=2)
        self.controller.tick(2)
        self.alerts.submit.assert_called_once()

    def test_background_phrases_and_stale_commands_do_not_alert(self):
        for phrase in ('no help needed', 'helpful', 'i am okay', 'turn on the television'):
            self.help(text=phrase)
            self.controller.tick(1)
        self.help(at=0)
        self.controller.tick(5)
        self.help(at=6, confidence=.5)
        self.controller.tick(6)
        self.alerts.submit.assert_not_called()

    def test_backup_resumes_after_cancel_audio_finishes(self):
        self.controller.begin_check(0)
        self.controller.respond('i am okay', 1)
        self.voice.is_busy.return_value = True
        self.controller.tick(2)
        self.assertEqual(self.controller._backup_session, '')
        self.voice.is_busy.return_value = False
        self.controller.tick(3)
        self.assertTrue(self.controller._backup_session)
        self.help(at=4)
        self.controller.tick(4)
        self.alerts.submit.assert_called_once()

    def test_standby_ready_status_is_cleared_during_own_playback(self):
        session = self.controller._backup_session
        self.voice.events.put(VoiceEvent('backup_ready', session, 1, 'Listening locally'))
        self.controller.tick(1)
        self.assertTrue(self.controller.backup_ready)
        self.controller.begin_check(2)
        self.assertFalse(self.controller.backup_ready)
        self.help(at=3, session=session)
        self.controller.tick(3)
        self.alerts.submit.assert_not_called()

    def test_help_button_works_with_camera_and_microphone_unavailable(self):
        control = Controller(self.alerts)
        dashboard = Dashboard()
        command = Command('help', '')
        dashboard.commands.put(command)
        dashboard.apply_commands(control, None, 1, None)
        self.assertEqual(command.result[0], 200)
        self.assertEqual(control.monitor.state, State.ALERTED)

    def test_missing_camera_sends_text_and_calls_to_all_test_recipients(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = Mock(return_value={'message_id': 1})
            call = Mock(return_value={'sid': 'CA' + 'c' * 32, 'status': 'queued'})
            alerts = AlertDispatcher(directory, telegram=True, calls=True,
                config={'token': '0:FAKE', 'recipients': [{'id': str(i)} for i in (1, 2, 3)]}, client=telegram,
                call_config={'account_sid': 'AC'+'a'*32, 'auth_token': 'b'*32, 'from_number': '+14155550100',
                             'recipients': [{'number': '+16045550101'}, {'number': '+16045550102'}]}, call_client=call)
            try:
                control = Controller(alerts, self.voice)
                self.voice.events.put(VoiceEvent('backup_help', control._backup_session, 1, 'help', None))
                control.tick(1)
                for future in alerts.futures:
                    future.result(timeout=3)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(telegram.call_count, 3)
                self.assertEqual({item.args[1] for item in telegram.call_args_list}, {'sendMessage'})
            finally:
                alerts.close()
