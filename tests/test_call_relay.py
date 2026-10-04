import asyncio
import base64
import queue
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from granny.call_audio import CallAudioPlayer, decode_mulaw
from granny.call_relay import AudioRequest, CallRelay
from granny.calls import TwilioClient, TwilioError
from granny.controller import Controller
from granny.core import State
from granny.delivery import DeliveryProgress
from test_calls import CONFIG, CALL


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TwilioClient(CONFIG)
        self.client.api = Mock(return_value=CALL)
        self.player = Mock()
        self.relay = CallRelay(CONFIG, "https://calls.example.test", lambda: self.player, self.client)
        self.progress = DeliveryProgress(queue.Queue())
        self.number = CONFIG["recipients"][0]["number"]
        self.progress.begin("incident", [], CONFIG["recipients"])
        self.relay.create_call(self.number, "incident", "Person requested help", self.progress)
        self.session = next(iter(self.relay.sessions.values()))
        self.http = TestClient(TestServer(self.relay.app()))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        self.relay.mute()

    def row(self):
        return self.progress.rows["incident"][("calls", self.number)]

    def fields(self, **extra):
        return dict(AccountSid=CONFIG["account_sid"], CallSid=CALL["sid"], To=self.number, **extra)

    def headers(self, path, fields=None):
        signature = self.relay.validator.compute_signature(self.relay.public_url + path, fields or {})
        return {"X-Twilio-Signature": signature}

    async def post(self, path, fields):
        return await self.http.post(path, data=fields, headers=self.headers(path, fields))

    def path(self, suffix):
        return f"/call/{self.session.token}/{suffix}"

    async def test_calls_remain_allowlisted_and_use_both_tracks_without_recording(self):
        with self.assertRaises(TwilioError):
            self.relay.create_call("911", "incident", "test", self.progress)
        with self.assertRaises(TwilioError):
            self.relay.create_call("+16045550999", "incident", "test", self.progress)
        self.assertEqual(self.client.api.call_count, 1)
        fields = self.client.api.call_args.args[1]
        root = ET.fromstring(fields["Twiml"])
        self.assertEqual(root.find("Start/Stream").get("track"), "both_tracks")
        self.assertTrue(root.find("Start/Stream").get("url").startswith("wss://"))
        self.assertEqual(root.find("Gather").get("actionOnEmptyResult"), "true")
        self.assertNotIn("Record", fields)
        self.assertEqual(fields["TimeLimit"], 120)
        self.assertIn("played live", root.find("Say").text)
        self.assertTrue(root.find("Say").text.startswith("Matthew has asked for help"))
        self.assertNotIn('demo', ' '.join(root.itertext()).lower())
        self.assertTrue(all(say.get('voice') == 'Polly.Joanna-Neural' for say in root.iter('Say')))
        self.assertNotIn('MachineDetection', fields)
        self.assertNotIn('AsyncAmdStatusCallback', fields)
        self.assertNotIn('your loved one', ' '.join(root.itertext()).lower())

    async def test_silence_call_explains_fall_then_asks_for_confirmation(self):
        root = ET.fromstring(self.relay.initial_xml(self.session, "No clear response before the deadline"))
        self.assertTrue(root.find('Say').text.startswith('Matthew may have fallen.'))
        self.assertIn("didn't get a clear response", root.find('Say').text)
        self.assertIn("Press 1", root.find('Gather/Say').text)
        self.assertEqual(root.find('Gather').get('input'), 'dtmf')
        self.assertEqual(root.find('Gather').get('numDigits'), '1')
        self.assertEqual(root.find('Gather').get('timeout'), '12')
        self.assertIn("press 1 to confirm", ' '.join(root.itertext()).lower())
        self.assertIn("press 2", ' '.join(root.itertext()).lower())
        self.assertNotIn('demo', ' '.join(root.itertext()).lower())

    async def test_fall_and_cannot_get_up_reach_the_call_then_keypad_confirms(self):
        from granny.core import Monitor
        monitor = Monitor()
        monitor.begin_check(0)
        action = monitor.respond("I can't get up", 1, 1, monitor.incident_id)[0]
        root = ET.fromstring(self.relay.initial_xml(self.session, action.reason))
        self.assertIn("Matthew may have fallen and says he can't get up", root.find('Say').text)
        self.assertIn('Press 1 to confirm', root.find('Gather/Say').text)
        self.assertEqual(root.find('Gather').get('input'), 'dtmf')
        response = await self.post(self.path('reply/0'), self.fields(Digits='1'))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.row()['confirmation'], 'coming')

    async def test_no_button_opens_voice_option_then_returns_to_noise_safe_keypad(self):
        response = await self.post(self.path('reply/0'), self.fields())
        xml = ET.fromstring(await response.text())
        self.assertEqual(xml.find('Gather').get('input'), 'speech dtmf')
        self.assertEqual(xml.find('Gather').get('speechTimeout'), '2')
        response = await self.post(self.path('reply/1'), self.fields(SpeechResult='unrelated room conversation'))
        xml = ET.fromstring(await response.text())
        self.assertEqual(xml.find('Gather').get('input'), 'dtmf')
        response = await self.post(self.path('reply/2'), self.fields(Digits='1'))
        self.assertEqual(self.row()['confirmation'], 'coming')

    async def test_trial_mode_preserves_confirmation_without_unsupported_stream(self):
        self.relay.stream_audio = False
        root = ET.fromstring(self.relay.initial_xml(self.session, "Test"))
        self.assertIsNone(root.find("Start"))
        self.assertIsNotNone(root.find("Gather"))
        self.assertNotIn("played live", root.find("Say").text)
        response = await self.http.get(self.path("audio"), headers=self.headers(self.path("audio")))
        self.assertEqual(response.status, 404)
        response = await self.post(self.path("reply/0"), self.fields(SpeechResult="Yes, I am coming"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.row()["confirmation"], "coming")

    async def test_unsigned_callbacks_and_dashboard_are_unavailable(self):
        response = await self.http.post(self.path("reply/0"), data=self.fields(SpeechResult="Yes, I am coming"))
        self.assertEqual(response.status, 403)
        self.assertEqual(self.row()["confirmation"], "pending")
        for path in ("/api/status", "/api/frame.jpg", "/", "/.env"):
            self.assertEqual((await self.http.get(path)).status, 404)

    async def test_wrong_account_call_recipient_and_query_rejected(self):
        for key, value in (("AccountSid", "AC" + "d" * 32), ("CallSid", "CA" + "e" * 32),
                           ("To", "+16045550999")):
            fields = self.fields(SpeechResult="Yes, I am coming")
            fields[key] = value
            response = await self.post(self.path("reply/0"), fields)
            self.assertEqual(response.status, 403)
        response = await self.post(self.path("reply/0") + "?a=b", self.fields(SpeechResult="Yes, I am coming"))
        self.assertEqual(response.status, 403)
        self.assertEqual(self.row()["confirmation"], "pending")

    async def test_verbal_commitment_confirms_and_callback_retry_is_idempotent(self):
        await self.post(self.path("reply/0"), self.fields())
        fields = self.fields(SpeechResult="Yes I'm coming", Confidence="0.93")
        response = await self.post(self.path("reply/1"), fields)
        first = await response.text()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.row()["confirmation"], "coming")
        self.assertIn("let Matthew know you're coming", first)
        self.assertNotIn("press", first.lower())
        replay = await self.post(self.path("reply/1"), fields)
        self.assertEqual(await replay.text(), first)

    async def test_uncertain_or_low_confidence_reply_repeats_speech_and_keypad_options(self):
        for index, fields in enumerate((self.fields(SpeechResult="Maybe I can come"),
                                        self.fields(SpeechResult="Yes", Confidence="0.2"))):
            response = await self.post(self.path(f"reply/{index}"), fields)
            text = await response.text()
            self.assertEqual(self.row()["confirmation"], "pending")
            self.assertIn("press 1", text.lower())

    async def test_keypad_one_confirms_despite_conflicting_room_noise(self):
        fields = self.fields(Digits="1", SpeechResult="No I cannot come", Confidence="0.1")
        response = await self.post(self.path("reply/0"), fields)
        first = await response.text()
        self.assertEqual(self.row()["confirmation"], "coming")
        self.assertIn("let Matthew know you're coming", first)
        self.assertEqual(await (await self.post(self.path("reply/0"), fields)).text(), first)

    async def test_keypad_two_declines_despite_conflicting_room_noise(self):
        await self.post(self.path("reply/0"), self.fields(Digits="2", SpeechResult="Yes I'm coming"))
        self.assertEqual(self.row()["confirmation"], "unavailable")
        self.client.api.reset_mock()
        await self.post(self.path('machine'), self.machine_fields())
        self.client.api.assert_not_called()
        self.assertEqual(self.row()['confirmation'], 'unavailable')

    async def test_other_keys_never_confirm_and_repeat_the_options(self):
        for index, digit in enumerate(('3', '0', '12')):
            response = await self.post(self.path(f"reply/{index}"), self.fields(Digits=digit, SpeechResult="Yes"))
            self.assertNotEqual(self.row()["confirmation"], "coming")
            if index < 2:
                self.assertIn('press 1', (await response.text()).lower())

    async def test_no_and_negation_never_claim_someone_is_coming(self):
        response = await self.post(self.path("reply/0"), self.fields(SpeechResult="Yes but I cannot come"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.row()["confirmation"], "unavailable")
        self.assertIn("you can't come", await response.text())

    async def test_silence_is_bounded_and_never_confirmed(self):
        for index in range(3):
            response = await self.post(self.path(f"reply/{index}"), self.fields())
            self.assertEqual(response.status, 200)
        self.assertEqual(self.row()["confirmation"], "no-response")
        self.assertIsNone(ET.fromstring(await response.text()).find("Gather"))

    async def test_real_status_and_out_of_order_updates_do_not_regress(self):
        for sequence, status in ((1, "ringing"), (2, "in-progress"), (0, "initiated"), (3, "completed")):
            response = await self.post(self.path("status"), self.fields(CallStatus=status, SequenceNumber=str(sequence)))
            self.assertEqual(response.status, 204)
        self.assertEqual(self.row()["state"], "completed")
        self.assertEqual(self.row()["confirmation"], "no-response")
        self.progress.update("incident", "calls", self.number, state="queued", detail="old response")
        self.assertEqual(self.row()["state"], "completed")

    async def test_unknown_or_skipped_round_is_rejected(self):
        response = await self.post(self.path("reply/2"), self.fields(SpeechResult="Yes, I am coming"))
        self.assertEqual(response.status, 409)
        self.assertEqual(self.row()["confirmation"], "pending")

    def machine_fields(self, answered='machine_start'):
        return {'AccountSid': CONFIG['account_sid'], 'CallSid': CALL['sid'],
                'AnsweredBy': answered, 'MachineDetectionDuration': '2100'}

    async def test_machine_guess_cannot_end_a_live_call_before_keypad_prompt(self):
        self.client.api.reset_mock()
        await self.post(self.path('machine'), self.machine_fields())
        self.client.api.assert_not_called()
        self.assertEqual(self.row()['confirmation'], 'pending')
        response = await self.post(self.path('reply/0'), self.fields(Digits='1'))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.row()['confirmation'], 'coming')

    async def start_stream(self, account=None):
        path = self.path("audio")
        socket = await self.http.ws_connect(path, headers=self.headers(path))
        await socket.send_json({"event": "start", "start": {
            "accountSid": account or CONFIG["account_sid"], "callSid": CALL["sid"],
            "streamSid": "MZ" + "a" * 32,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}})
        return socket

    async def test_stream_plays_both_tracks_after_main_controller_silences_voice(self):
        socket = await self.start_stream()
        gate = await asyncio.to_thread(self.relay.events.get, True, 2)
        self.assertIsInstance(gate, AudioRequest)
        gate.allowed = True
        gate.done.set()
        for track in ("inbound", "outbound"):
            await socket.send_json({"event": "media", "streamSid": "MZ" + "a" * 32,
                "media": {"track": track, "timestamp": "0", "chunk": "1", "payload": "/w=="}})
        await socket.send_json({"event": "stop"})
        await socket.receive(timeout=2)
        self.player.start.assert_called_once()
        self.assertEqual(self.player.add.call_count, 2)
        self.player.close.assert_called_once()
        self.assertEqual(self.row()["audio"], "ended")

    async def test_foreign_stream_never_opens_speaker(self):
        socket = await self.start_stream("AC" + "d" * 32)
        await socket.receive(timeout=2)
        self.player.start.assert_not_called()
        self.assertEqual(self.row()["audio"], "unavailable")

    async def test_muted_stream_does_not_open_speaker(self):
        self.relay.mute("incident")
        socket = await self.start_stream()
        await socket.send_json({"event": "stop"})
        await socket.receive(timeout=2)
        self.player.start.assert_not_called()


class AudioTests(unittest.TestCase):
    def test_mulaw_extremes_and_two_track_mix(self):
        decoded = decode_mulaw(base64.b64encode(bytes([255, 127, 0, 128])).decode())
        self.assertEqual(decoded.tolist(), [0, 0, -32124, 32124])
        player = CallAudioPlayer()
        loud = base64.b64encode(bytes([128]) * 160).decode()
        player.add("inbound", 0, 1, loud)
        player.add("outbound", 0, 1, loud)
        player.ready_at = 0
        output = bytearray(320)
        player._output(output, 160, None, None)
        self.assertEqual(np.frombuffer(output, dtype=np.int16).tolist(), [32767] * 160)
        player.close()

    def test_bad_audio_and_duplicate_packets(self):
        with self.assertRaises(ValueError):
            decode_mulaw("not base64")
        player = CallAudioPlayer()
        payload = base64.b64encode(bytes([128]) * 160).decode()
        player.add("inbound", 0, 1, payload)
        player.add("inbound", 0, 1, payload)
        self.assertEqual(player.frames[0][0], 32124)
        with self.assertRaises(ValueError):
            player.add("microphone", 0, 2, payload)
        player.close()

    def test_audio_player_opens_output_only(self):
        player = CallAudioPlayer()
        with patch("sounddevice.RawOutputStream") as output, patch("sounddevice.RawInputStream") as microphone:
            player.start()
            player.close()
        output.assert_called_once()
        microphone.assert_not_called()


class CallCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.relay = Mock()
        self.relay.events = queue.Queue()
        self.alerts = Mock(call_relay=self.relay, events=queue.Queue(), progress_events=queue.Queue())
        self.voice = Mock(events=queue.Queue())
        self.control = Controller(self.alerts, self.voice)
        self.control.monitor.incident_id = "incident"
        self.control.monitor.state = State.ALERTED
        self.control.voice_repeating = True

    def test_live_call_stops_local_voice_and_mute_does_not_cancel_alert(self):
        gate = AudioRequest("incident", "token")
        self.relay.events.put(gate)
        self.control.tick(0)
        self.assertTrue(gate.allowed)
        self.voice.stop.assert_called_once()
        self.assertFalse(self.control.voice_repeating)
        self.assertTrue(self.control.call_listening)
        self.assertTrue(self.control.stop_call_audio())
        self.relay.mute.assert_called_once_with("incident")
        self.assertEqual(self.control.monitor.state, State.ALERTED)
        second = AudioRequest("incident", "newtoken")
        self.relay.events.put(second)
        self.control.tick(1)
        self.assertFalse(second.allowed)

    def test_stale_call_after_reset_cannot_interrupt_new_check(self):
        self.control.reset()
        self.control.begin_check(1)
        gate = AudioRequest("incident", "token")
        self.relay.events.put(gate)
        self.control.tick(2)
        self.assertFalse(gate.allowed)
        self.assertFalse(self.control.call_listening)

    def test_delivery_status_is_scoped_to_current_incident(self):
        self.alerts.progress_events.put(("old", [{"channel": "calls", "state": "completed"}]))
        self.alerts.progress_events.put(("incident", [{"channel": "calls", "state": "ringing"}]))
        self.control.tick(1)
        self.assertEqual(self.control.deliveries, [{"channel": "calls", "state": "ringing"}])

    def test_confirmation_updates_voice_only_after_explicit_commitment(self):
        self.alerts.progress_events.put(("incident", [{"channel": "calls", "state": "in-progress", "confirmation": "pending"}]))
        self.control.tick(1)
        self.voice.announce.assert_not_called()
        self.alerts.progress_events.put(("incident", [{"channel": "calls", "state": "in-progress", "confirmation": "coming"}]))
        self.control.tick(2)
        self.control.tick(3)
        self.voice.announce.assert_called_once_with("incident", "family_confirmed", repeat_until_ack=True)

    def test_failed_calls_report_unconfirmed_without_restart_after_stop(self):
        self.alerts.progress_events.put(("incident", [{"channel": "calls", "state": "no-answer", "confirmation": "no-response"}]))
        self.control.tick(1)
        self.voice.announce.assert_called_once_with("incident", "family_unconfirmed", repeat_until_ack=True)
        self.control.stop_voice()
        self.alerts.progress_events.put(("incident", [{"channel": "calls", "state": "completed", "confirmation": "coming"}]))
        self.control.tick(2)
        self.assertEqual(self.voice.announce.call_count, 1)


if __name__ == "__main__":
    unittest.main()
