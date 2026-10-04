import io
import base64
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib import error, parse

from granny.audio import VoiceCheck, VoiceEvent
from granny.controller import Controller
from granny.core import State
from granny.elevenlabs import (ElevenLabsClient, ElevenLabsError, ScribeSession,
                               load_clips, load_config, prepare_clips, save_config, clip_name)
from granny.speech import PHRASES, Speaker

KEY = "sk_" + "testonly" * 4


class AnnouncementTests(unittest.TestCase):
    def controller(self, telegram=False, calls=False):
        voice = Mock()
        voice.events = queue.Queue()
        alerts = Mock(telegram=telegram, calls=calls)
        alerts.events = queue.Queue()
        control = Controller(alerts, voice)
        control.begin_check(0)
        return control, alerts, voice

    def test_alert_announces_only_enabled_channels_once(self):
        for telegram, calls, phrase in ((False, False, "dry_run"), (True, False, "telegram"),
                                        (False, True, "calls"), (True, True, "both")):
            with self.subTest(phrase=phrase):
                control, alerts, voice = self.controller(telegram, calls)
                order = []
                alerts.submit.side_effect = lambda *_: order.append("submit") or object()
                voice.announce.side_effect = lambda *_, **__: order.append("speak")
                control.respond("help", 1)
                control.respond("help", 2)
                self.assertEqual(order, ["submit", "speak"])
                voice.announce.assert_called_once_with(control.monitor.incident_id, phrase, repeat_until_ack=True)
                self.assertNotIn("dialing 911", PHRASES[phrase].lower())

    def test_cancel_speaks_without_dispatching(self):
        control, alerts, voice = self.controller(True, True)
        control.respond("i am okay", 1)
        self.assertEqual(control.monitor.state, State.COOLDOWN)
        alerts.submit.assert_not_called()
        voice.announce.assert_called_once_with(control.monitor.incident_id, "cancel", repeat_until_ack=False)

    def test_queue_failure_does_not_claim_family_is_being_contacted(self):
        control, alerts, voice = self.controller(True, True)
        alerts.submit.side_effect = OSError("Disk unavailable")
        control.respond("help", 1)
        voice.announce.assert_called_once_with(control.monitor.incident_id, "failed", repeat_until_ack=True)
        self.assertIn("could not be queued", control.alert_status)

    def test_speaker_failure_cannot_suppress_notifications(self):
        control, alerts, voice = self.controller(True, True)
        voice.announce.side_effect = RuntimeError("Speaker failed")
        control.respond("help", 1)
        alerts.submit.assert_called_once()
        self.assertEqual(control.monitor.state, State.ALERTED)

    def test_cloud_final_transcript_keeps_negation_and_has_no_fake_confidence(self):
        control, alerts, voice = self.controller(True, True)
        voice.events.put(VoiceEvent("speech", control.monitor.incident_id, 1,
                                    "I'm not okay.", None, "ElevenLabs"))
        control.tick(1)
        self.assertEqual(control.monitor.state, State.ALERTED)
        self.assertIn("final transcript", control.heard)
        self.assertNotIn("100%", control.heard)
        alerts.submit.assert_called_once()

    def test_silence_still_alerts_after_cloud_error_and_stale_event_is_ignored(self):
        control, alerts, voice = self.controller()
        voice.events.put(VoiceEvent("error", control.monitor.incident_id, 1, "Cloud down"))
        control.tick(61)
        alerts.submit.assert_called_once()
        old = control.monitor.incident_id
        control.reset()
        reset_audio_status = control.audio_status
        voice.events.put(VoiceEvent("announcement", old, 62, "Old alert speech"))
        control.tick(62)
        self.assertEqual(control.audio_status, reset_audio_status)

    def test_voice_acknowledgement_stops_repetition_without_recalling_or_resending_alert(self):
        control, alerts, voice = self.controller(True, True)
        control.respond("help", 1)
        self.assertTrue(control.voice_repeating)
        voice.events.put(VoiceEvent("acknowledged", control.monitor.incident_id, 2, "okay", .95))
        control.tick(2)
        self.assertFalse(control.voice_repeating)
        self.assertEqual(control.monitor.state, State.ALERTED)
        self.assertIn("remain active", control.audio_status)
        self.assertIn("okay", control.heard)
        alerts.submit.assert_called_once()
        voice.events.put(VoiceEvent("announcement", control.monitor.incident_id, 3, "Stale playback"))
        control.tick(3)
        self.assertIn("remain active", control.audio_status)

    def test_stop_voice_cannot_silence_a_check_or_reset_a_sent_alert(self):
        control, alerts, voice = self.controller(True, True)
        self.assertFalse(control.stop_voice())
        control.respond("help", 1)
        self.assertTrue(control.stop_voice())
        self.assertFalse(control.stop_voice())
        self.assertEqual(control.monitor.state, State.ALERTED)
        alerts.submit.assert_called_once()
        control.reset()
        self.assertFalse(control.voice_repeating)


class ElevenLabsClientTests(unittest.TestCase):
    def test_tts_uses_fixed_tls_host_header_and_pcm_output(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(bytes(32000))
        with patch("granny.elevenlabs.request.build_opener", return_value=opener):
            wav = ElevenLabsClient(KEY).synthesize("voice123", PHRASES["check"])
        self.assertTrue(wav.startswith(b"RIFF"))
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.elevenlabs.io/v1/text-to-speech/voice123?output_format=pcm_16000")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data)["text"], PHRASES["check"])
        self.assertEqual(json.loads(req.data)["voice_settings"]["speed"], .85)
        self.assertEqual(req.get_header("Xi-api-key"), KEY)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_changed_voice_speed_cannot_reuse_a_faster_cached_clip(self):
        slower = clip_name("voice123", PHRASES["check"])
        with patch.dict("granny.elevenlabs.VOICE_SETTINGS", {"speed": 1.0}):
            self.assertNotEqual(slower, clip_name("voice123", PHRASES["check"]))

    def test_errors_never_expose_the_key_or_retry(self):
        for failure in (error.URLError(KEY), error.HTTPError("https://api.elevenlabs.io", 401, KEY, {}, io.BytesIO(KEY.encode()))):
            opener = Mock()
            opener.open.side_effect = failure
            with patch("granny.elevenlabs.request.build_opener", return_value=opener):
                with self.assertRaises(ElevenLabsError) as caught:
                    ElevenLabsClient(KEY).voices()
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_invalid_voice_id_cannot_change_request_destination(self):
        client = ElevenLabsClient(KEY)
        with patch.object(client, "_request") as request, self.assertRaises(ElevenLabsError):
            client.synthesize("../../evil?key=leak", "test")
        request.assert_not_called()

    def test_private_configuration_and_cached_speech_work_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = {"api_key": KEY, "voice_id": "voice123"}
            save_config(config, folder / "config.json")
            self.assertEqual((folder / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_config(folder / "config.json", env_path=folder / ".env", environ={}), config)
            with patch.object(ElevenLabsClient, "_request", return_value=bytes(32000)) as api:
                prepare_clips(config, folder / "cache", progress=lambda _: None)
                self.assertEqual(api.call_count, len(PHRASES))
                api.reset_mock()
                prepare_clips(config, folder / "cache", progress=lambda _: None)
                api.assert_not_called()
            clips = load_clips(folder / "cache")
            self.assertEqual(set(clips), set(PHRASES))
            self.assertNotIn(KEY, (folder / "cache/voice.json").read_text())

    def test_scribe_uses_encrypted_header_auth_and_only_final_raw_text(self):
        connection = Mock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.side_effect = lambda *_: connection.close()
        connection.recv.side_effect = [json.dumps(item) for item in (
            {"message_type": "partial_transcript", "text": "I'm okay"},
            {"message_type": "committed_transcript", "text": "I'm not okay"},
            {"message_type": "edited_transcript", "edited_text": "I'm okay"},
            {"message_type": "quota_exceeded", "error": "Do not expose " + KEY},
        )]
        with patch("websockets.sync.client.connect", return_value=context) as connect:
            session = ScribeSession(KEY)
        url = connect.call_args.args[0]
        self.assertTrue(url.startswith("wss://api.elevenlabs.io/"))
        self.assertNotIn(KEY, url)
        self.assertEqual(connect.call_args.kwargs["additional_headers"]["xi-api-key"], KEY)
        self.assertTrue(connect.call_args.kwargs["ssl"].check_hostname)
        self.assertEqual(parse.parse_qs(parse.urlsplit(url).query)["commit_strategy"], ["vad"])
        self.assertIsNone(session.receive())
        self.assertEqual(session.receive(), "I'm not okay")
        self.assertIsNone(session.receive())
        with self.assertRaises(ElevenLabsError) as caught:
            session.receive()
        self.assertNotIn(KEY, str(caught.exception))
        session.close()
        connection.close.assert_called_once()

    def test_bad_cached_audio_falls_back_to_mac_speech(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as clip:
            speaker = Speaker({"check": clip.name})
            with patch.object(speaker, "_play", side_effect=[RuntimeError("Broken clip"), None]) as play:
                speaker.speak("check", threading.Event())
            self.assertEqual(play.call_args_list[0].args[0][0], "/usr/bin/afplay")
            self.assertEqual(play.call_args_list[1].args[0][0], "/usr/bin/say")

    def test_realtime_transport_sends_pcm_and_receives_final_text(self):
        from websockets.sync.client import connect as real_connect
        from websockets.sync.server import serve
        captured = []

        def handle(connection):
            connection.send(json.dumps({"message_type": "session_started"}))
            message = json.loads(connection.recv(timeout=2))
            captured.append(message)
            connection.send(json.dumps({"message_type": "partial_transcript", "text": "I am okay"}))
            connection.send(json.dumps({"message_type": "committed_transcript", "text": "I am not okay"}))
            try:
                connection.recv(timeout=2)
            except Exception:
                pass

        with serve(handle, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.socket.getsockname()[1]

            def local_connection(_url, **_kwargs):
                return real_connect(f"ws://127.0.0.1:{port}", proxy=None, open_timeout=2, close_timeout=.2)

            session = None
            try:
                with patch("websockets.sync.client.connect", side_effect=local_connection):
                    session = ScribeSession(KEY)
                session.send(bytes(3200))
                received = []
                deadline = time.monotonic() + 2
                while not received and time.monotonic() < deadline:
                    text = session.receive()
                    if text:
                        received.append(text)
                self.assertEqual(received, ["I am not okay"])
                self.assertEqual(captured[0]["message_type"], "input_audio_chunk")
                self.assertEqual(base64.b64decode(captured[0]["audio_base_64"]), bytes(3200))
                self.assertEqual(captured[0]["sample_rate"], 16000)
            finally:
                if session:
                    session.close()
                server.shutdown()
                thread.join(timeout=2)


class VoiceWorkerTests(unittest.TestCase):
    def test_repeated_updates_listen_locally_and_negation_does_not_silence_them(self):
        order = []

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs["callback"]

            def __enter__(self):
                order.append("mic open")
                self.capture(bytes(3200), 1600, None, None)
                return self

            def __exit__(self, *_args):
                order.append("mic closed")

        speaker = Mock()
        speaker.speak.side_effect = lambda *_: order.append("speak")
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.side_effect = [json.dumps({"text": text, "result": [{"conf": .95}]})
                                        for text in ("i am not okay", "okay")]
        with patch("vosk.Model"), patch("granny.audio.new_recognizer", return_value=recognizer), \
                patch("sounddevice.RawInputStream", Microphone), \
                patch("granny.elevenlabs.ScribeSession") as cloud:
            voice = VoiceCheck("unused", speaker=speaker, cloud_key=KEY, acknowledgement_seconds=.05)
            try:
                voice.announce("incident", "both", repeat_until_ack=True)
                voice._thread.join(timeout=3)
                self.assertFalse(voice._thread.is_alive())
                self.assertEqual(order, ["speak", "mic open", "mic closed"] * 2)
                events = list(voice.events.queue)
                self.assertEqual([item.text for item in events if item.kind == "acknowledged"], ["okay"])
                cloud.assert_not_called()
            finally:
                voice.stop()

    def test_stop_interrupts_repeated_playback_without_another_prompt(self):
        playing = threading.Event()
        speaker = Mock()

        def speak(_phrase, stop):
            playing.set()
            stop.wait(5)

        speaker.speak.side_effect = speak
        with patch("vosk.Model"), patch("sounddevice.RawInputStream") as microphone:
            voice = VoiceCheck("unused", speaker=speaker)
            try:
                voice.announce("incident", "both", repeat_until_ack=True)
                self.assertTrue(playing.wait(2))
                voice.stop()
                self.assertFalse(voice._thread.is_alive())
                speaker.speak.assert_called_once()
                microphone.assert_not_called()
            finally:
                voice.stop()

    def test_check_keeps_microphone_open_across_repeats_and_accepts_later_reply(self):
        order = []
        microphones = []

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs["callback"]
                microphones.append(self)

            def __enter__(self):
                order.append("mic open")
                return self

            def __exit__(self, *_args):
                order.append("mic closed")

        speaker = Mock()
        speaker.speak.side_effect = lambda *_: order.append("speak")
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.return_value = json.dumps({"text": "help", "result": [{"conf": .95}]})
        with patch("vosk.Model"), patch("granny.audio.new_recognizer", return_value=recognizer), \
                patch("sounddevice.RawInputStream", Microphone):
            voice = VoiceCheck("unused", speaker=speaker)
            try:
                voice.start("incident")
                deadline = time.monotonic() + 3
                repeated = heard = False
                while time.monotonic() < deadline:
                    event = voice.events.get(timeout=1)
                    if event.kind == "ready" and not repeated:
                        repeated = True
                        voice.repeat()
                    elif event.kind == "ready" and repeated:
                        microphones[0].capture(bytes(1600), 800, None, None)
                    elif event.kind == "speech":
                        self.assertEqual(event.text, "help")
                        heard = True
                        break
                self.assertTrue(heard)
                voice.stop()
                self.assertEqual(order, ["mic open", "speak", "speak", "mic closed"])
            finally:
                voice.stop()

    def test_cloud_failure_keeps_microphone_local_and_announcements_close_it(self):
        events = []
        closed = threading.Event()

        class Microphone:
            def __init__(self, **kwargs):
                self.capture = kwargs["callback"]

            def __enter__(self):
                events.append("mic open")
                self.capture(bytes(3200), 1600, None, None)
                self.capture(bytes(3200), 1600, None, None)
                return self

            def __exit__(self, *_args):
                events.append("mic closed")
                closed.set()

        speaker = Mock()
        speaker.speak.side_effect = lambda phrase, _stop: events.append("speak " + phrase)
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = True
        recognizer.Result.return_value = json.dumps({"text": "help", "result": [{"conf": .9}]})
        cloud = Mock()
        cloud.send.side_effect = OSError("Disconnected")
        with patch("vosk.Model"), patch("granny.audio.new_recognizer", return_value=recognizer), \
                patch("sounddevice.RawInputStream", Microphone), \
                patch("granny.elevenlabs.ScribeSession", return_value=cloud):
            voice = VoiceCheck("unused", speaker=speaker, cloud_key=KEY)
            try:
                voice.start("incident")
                received = []
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    item = voice.events.get(timeout=1)
                    received.append(item)
                    if (any(item.kind == "speech" for item in received) and
                            any("Cloud recognition unavailable" in item.text for item in received)):
                        break
                self.assertTrue(any(item.kind == "speech" and item.text == "help" for item in received))
                self.assertTrue(any("Cloud recognition unavailable" in item.text for item in received))
                voice.announce("incident", "dry_run")
                voice._thread.join(timeout=2)
                self.assertTrue(closed.is_set())
                self.assertLess(events.index("mic open"), events.index("speak check"))
                self.assertLess(events.index("mic closed"), events.index("speak dry_run"))
                self.assertEqual(cloud.send.call_count, 1)
            finally:
                voice.stop()


if __name__ == "__main__":
    unittest.main()
