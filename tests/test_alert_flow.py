from concurrent.futures import wait
import queue
import tempfile
import unittest
from unittest.mock import Mock

from granny.alerts import AlertDispatcher
from granny.audio import VoiceEvent
from granny.controller import Controller
from granny.core import State
from test_monitor import FLOOR, STANDING


class AlertFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Mock(return_value={"message_id": 1})
        self.config = {"token": "0:FAKE", "recipients": [{"id": str(i)} for i in (1, 2, 3)]}

    def dispatcher(self, enabled=True):
        dispatcher = AlertDispatcher(self.directory.name, telegram=enabled,
                                     config=self.config, client=self.client)
        self.addCleanup(dispatcher.close)
        return dispatcher

    def test_dry_run_never_contacts_telegram(self):
        alerts = self.dispatcher(False)
        alerts.submit("dry", b"photo", "Test").result(timeout=2)
        self.client.assert_not_called()
        self.assertIn("OFF", alerts.events.get_nowait()[1])

    def test_recipient_failure_does_not_block_other_contacts(self):
        def send(token, method, fields, photo):
            if fields["chat_id"] == "2":
                raise RuntimeError("Recipient unavailable")
        self.client.side_effect = send
        alerts = self.dispatcher()
        alerts.submit("incident", b"photo", "Test").result(timeout=2)
        self.assertEqual(self.client.call_count, 3)
        self.assertIn("2/3", alerts.events.get_nowait()[1])

    def test_same_incident_is_not_resent_even_after_restart(self):
        first = self.dispatcher()
        first.submit("same", None, "Test").result(timeout=2)
        self.assertIsNone(first.submit("same", None, "Test"))
        second = self.dispatcher()
        self.assertIsNone(second.submit("same", None, "Test"))
        self.assertEqual(self.client.call_count, 3)

    def test_missing_snapshot_sends_text(self):
        alerts = self.dispatcher()
        alerts.submit("text", None, "Test").result(timeout=2)
        self.assertEqual({call.args[1] for call in self.client.call_args_list}, {"sendMessage"})

    def test_duplicate_contact_is_only_messaged_once(self):
        self.config["recipients"].append({"id": "1"})
        alerts = self.dispatcher()
        alerts.submit("dup", b"photo", "Test").result(timeout=2)
        self.assertEqual(self.client.call_count, 3)

    def test_fall_to_silence_to_photo_delivery_uses_incident_snapshot(self):
        alerts = self.dispatcher()
        voice = Mock()
        voice.events = queue.Queue()
        control = Controller(alerts, voice)
        control.monitor.calibrate(STANDING)
        control.observe(STANDING, 0, b"standing image")
        for step in range(33):
            control.observe(FLOOR, .5 + step / 10, b"incident image")
        self.assertEqual(control.monitor.state, State.CHECKING)
        incident = control.monitor.incident_id
        voice.start.assert_called_once_with(incident)
        voice.events.put(VoiceEvent("ready", incident, 4, "Listening"))
        control.tick(4)
        control.observe(STANDING, 5, b"later image")
        control.tick(8.99)
        voice.repeat.assert_not_called()
        control.tick(9)
        voice.repeat.assert_called_once()
        control.tick(49)
        wait(alerts.futures, timeout=2)
        control.tick(50)
        self.assertEqual(control.monitor.state, State.ALERTED)
        self.assertEqual(self.client.call_count, 3)
        for call in self.client.call_args_list:
            self.assertEqual(call.args[3][0], b"incident image")
            self.assertIn("TEST ONLY", call.args[2]["caption"])
        self.assertIn("3/3", control.alert_status)

    def test_recognized_okay_cancels_without_sending(self):
        alerts = self.dispatcher()
        voice = Mock()
        voice.events = queue.Queue()
        control = Controller(alerts, voice)
        control.begin_check(0, b"photo")
        voice.events.put(VoiceEvent("speech", control.monitor.incident_id, 2, "i am okay", .95))
        control.tick(2)
        self.assertEqual(control.monitor.state, State.COOLDOWN)
        self.client.assert_not_called()
        self.assertEqual(alerts.futures, [])

    def test_negated_okay_from_recognizer_escalates(self):
        alerts = self.dispatcher()
        voice = Mock()
        voice.events = queue.Queue()
        control = Controller(alerts, voice)
        control.begin_check(0, b"photo")
        voice.events.put(VoiceEvent("speech", control.monitor.incident_id, 2, "i am not okay", .95))
        control.tick(2)
        wait(alerts.futures, timeout=2)
        self.assertEqual(self.client.call_count, 3)

    def test_audio_failure_keeps_timeout_active(self):
        alerts = self.dispatcher(False)
        voice = Mock()
        voice.events = queue.Queue()
        control = Controller(alerts, voice)
        control.begin_check(0)
        voice.events.put(VoiceEvent("error", control.monitor.incident_id, 1, "Microphone denied"))
        control.tick(1)
        self.assertEqual(control.monitor.state, State.CHECKING)
        control.tick(61)
        self.assertEqual(control.monitor.state, State.ALERTED)


if __name__ == "__main__":
    unittest.main()
