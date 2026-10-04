from concurrent.futures import wait
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib import error, parse
from xml.etree import ElementTree

from granny.alerts import AlertDispatcher
from granny.calls import TwilioClient, TwilioError, load_config, phone_number, save_config, validate_config
from granny.controller import Controller
from granny.core import State
from granny.web import Dashboard

CONFIG = {"account_sid": "AC" + "a" * 32, "auth_token": "b" * 32,
          "from_number": "+14155550100", "recipients": [
              {"number": "+16045550101", "name": "Test one"},
              {"number": "+16045550102", "name": "Test two"}]}
CALL = {"sid": "CA" + "c" * 32, "status": "queued"}


class CallClientTests(unittest.TestCase):
    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.client = TwilioClient(self.config)

    def test_emergency_short_foreign_and_malformed_numbers_are_blocked(self):
        for number in ("911", "112", "+1911", "9-1-1", "+112", "+442079460000", None,
                       "tel:911", "+16045550101;911", "+16045550101#911"):
            with self.subTest(number=number), self.assertRaises(TwilioError):
                phone_number(number)
        self.assertEqual(phone_number("(604) 555-0101"), "+16045550101")

    def test_unselected_recipient_never_reaches_api(self):
        with patch.object(self.client, "api") as api, self.assertRaises(TwilioError):
            self.client.create_call("+16045550103", "incident", "Help")
        api.assert_not_called()

    def test_call_starts_with_help_request_and_has_bounded_duration(self):
        with patch.object(self.client, "api", return_value=CALL) as api:
            result = self.client.create_call("+16045550101", "incident", "Person requested help")
        self.assertEqual(result, CALL)
        method, fields = api.call_args.args
        self.assertEqual(method, "Calls")
        xml = ElementTree.fromstring(fields["Twiml"])
        self.assertTrue(xml.find("Say").text.startswith("Your loved one has asked for help."))
        self.assertNotIn("demo", xml.find("Say").text.lower())
        self.assertNotIn("fall", xml.find("Say").text.lower())
        self.assertEqual(xml.find("Say").get("voice"), "Polly.Joanna-Neural")
        self.assertIsNotNone(xml.find("Hangup"))
        self.assertEqual(fields["TimeLimit"], 60)
        self.assertEqual(fields["Timeout"], 20)
        self.assertNotIn("Url", fields)
        self.assertNotIn("Record", fields)

    def test_silence_call_explains_possible_fall_without_claiming_an_injury(self):
        with patch.object(self.client, "api", return_value=CALL) as api:
            self.client.create_call("+16045550101", "incident", "No clear response before the deadline")
        speech = ElementTree.fromstring(api.call_args.args[1]["Twiml"]).find("Say").text
        self.assertTrue(speech.startswith("Your loved one may have fallen."))
        self.assertIn("didn't get a clear response", speech)
        for claim in ('demo', 'unconscious', 'injured', 'cannot get up'):
            self.assertNotIn(claim, speech.lower())

    def test_setup_call_does_not_claim_a_fall(self):
        with patch.object(self.client, "api", return_value=CALL) as api:
            self.client.create_call("+16045550101", "test", "Setup", setup_test=True)
        speech = ElementTree.fromstring(api.call_args.args[1]["Twiml"]).find("Say").text
        self.assertIn("No fall or emergency has been detected", speech)

    def test_api_uses_tls_post_form_and_fixed_twilio_host(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(CALL).encode())
        with patch("granny.calls.request.build_opener", return_value=opener):
            self.client.create_call("+16045550101", "incident", "Help")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, f"https://api.twilio.com/2010-04-01/Accounts/{CONFIG['account_sid']}/Calls.json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(parse.parse_qs(req.data.decode())["To"], ["+16045550101"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)

    def test_timeout_is_not_retried_or_leaked(self):
        opener = Mock()
        opener.open.side_effect = error.URLError("secret " + self.config["auth_token"])
        with patch("granny.calls.request.build_opener", return_value=opener), self.assertRaises(TwilioError) as caught:
            self.client.create_call("+16045550101", "incident", "Help")
        self.assertIn("may have been created", str(caught.exception))
        self.assertNotIn(self.config["auth_token"], str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_api_errors_report_code_without_secret_body(self):
        opener = Mock()
        opener.open.side_effect = error.HTTPError("https://api.twilio.com", 400, "Bad request", {},
                                                 io.BytesIO(json.dumps({"code": 21219, "message": self.config["auth_token"]}).encode()))
        with patch("granny.calls.request.build_opener", return_value=opener), self.assertRaises(TwilioError) as caught:
            self.client.create_call("+16045550101", "incident", "Help")
        self.assertIn("21219", str(caught.exception))
        self.assertNotIn(self.config["auth_token"], str(caught.exception))

    def test_trial_recipient_must_be_verified(self):
        def api(resource=""):
            return {"": {"type": "Trial", "status": "active"},
                    "IncomingPhoneNumbers": {"incoming_phone_numbers": [
                        {"phone_number": CONFIG["from_number"], "capabilities": {"voice": True}}]},
                    "OutgoingCallerIds": {"outgoing_caller_ids": [{"phone_number": "+16045550101"}]}}[resource]
        with patch.object(self.client, "api", side_effect=api), self.assertRaisesRegex(TwilioError, "not verified"):
            self.client.check()

    def test_private_config_round_trip_and_recipient_deduplication(self):
        self.config["recipients"].append(self.config["recipients"][0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(self.config, path)
            result = load_config(path)
            self.assertEqual(len(result["recipients"]), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_empty_recipients_cannot_enable_calls(self):
        self.config["recipients"] = []
        with self.assertRaises(TwilioError):
            validate_config(self.config, require_recipients=True)


class CombinedAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.telegram = Mock(return_value={"message_id": 1})
        self.calls = Mock(return_value=CALL)
        self.telegram_config = {"token": "0:FAKE", "recipients": [{"id": str(index)} for index in (1, 2, 3)]}

    def dispatcher(self, telegram=True, calls=True):
        dispatcher = AlertDispatcher(self.directory.name, telegram=telegram, config=self.telegram_config,
                                     client=self.telegram, calls=calls, call_config=CONFIG, call_client=self.calls)
        self.addCleanup(dispatcher.close)
        return dispatcher

    def test_all_five_notifications_can_start_concurrently(self):
        barrier = threading.Barrier(5)
        def telegram(*args):
            barrier.wait(timeout=2)
            return {"message_id": 1}
        def call(*args):
            barrier.wait(timeout=2)
            return CALL
        self.telegram.side_effect = telegram
        self.calls.side_effect = call
        alerts = self.dispatcher()
        alerts.submit("parallel", b"photo", "Person requested help").result(timeout=4)
        self.assertEqual(self.telegram.call_count, 3)
        self.assertEqual(self.calls.call_count, 2)
        messages = []
        while not alerts.events.empty():
            messages.append(alerts.events.get_nowait()[1])
        self.assertIn("Telegram accepted 3/3", messages[-1])
        self.assertIn("Twilio accepted 2/2", messages[-1])
        with sqlite3.connect(alerts.database) as database:
            deliveries = database.execute("SELECT channel, status, provider_id FROM deliveries").fetchall()
        self.assertEqual(len(deliveries), 5)
        self.assertTrue(all(row[1] == "accepted" for row in deliveries))
        self.assertEqual([row[2] for row in deliveries if row[0] == "calls"], [CALL["sid"]] * 2)

    def test_one_failed_call_does_not_block_telegram_or_other_call(self):
        self.calls.side_effect = [TwilioError("Twilio returned HTTP 400, Twilio code 21219."), CALL]
        alerts = self.dispatcher()
        alerts.submit("failure", b"photo", "Help").result(timeout=3)
        self.assertEqual(self.telegram.call_count, 3)
        self.assertEqual(self.calls.call_count, 2)
        messages = [alerts.events.get_nowait()[1] for _ in range(alerts.events.qsize())]
        self.assertIn("Twilio accepted 1/2", messages[-1])

    def test_slow_telegram_does_not_delay_call_requests_or_status(self):
        release = threading.Event()
        self.telegram.side_effect = lambda *args: release.wait(timeout=3)
        alerts = self.dispatcher()
        future = alerts.submit("slow", None, "Help")
        try:
            _incident, message = alerts.events.get(timeout=2)
            self.assertIn("Twilio accepted 2/2", message)
            self.assertFalse(future.done())
        finally:
            release.set()
            future.result(timeout=3)

    def test_duplicate_incident_does_not_repeat_calls_after_restart(self):
        alerts = self.dispatcher(telegram=False)
        alerts.submit("once", None, "Help").result(timeout=3)
        self.assertIsNone(self.dispatcher(telegram=False).submit("once", None, "Help"))
        self.assertEqual(self.calls.call_count, 2)
        self.telegram.assert_not_called()

    def test_default_dry_run_never_reads_credentials_or_places_calls(self):
        with patch("granny.alerts.load_call_config", side_effect=AssertionError("Must stay offline")):
            alerts = self.dispatcher(telegram=False, calls=False)
            alerts.submit("dry", None, "Help").result(timeout=3)
        self.calls.assert_not_called()
        self.telegram.assert_not_called()

    def test_reassurance_cancels_both_channels(self):
        alerts = self.dispatcher()
        controller = Controller(alerts)
        controller.begin_check(0, b"photo")
        controller.respond("i am okay", 1)
        self.assertEqual(controller.monitor.state, State.COOLDOWN)
        self.assertEqual(alerts.futures, [])
        self.telegram.assert_not_called()
        self.calls.assert_not_called()

    def test_silence_reaches_both_channels(self):
        alerts = self.dispatcher()
        controller = Controller(alerts)
        controller.begin_check(0, b"photo")
        controller.tick(16)
        self.assertEqual(controller.monitor.state, State.CHECKING)
        self.assertEqual(alerts.futures, [])
        controller.tick(45)
        wait(alerts.futures, timeout=3)
        controller.tick(46)
        self.assertEqual(controller.monitor.state, State.ALERTED)
        self.assertEqual(self.calls.call_count, 2)
        self.assertEqual(self.telegram.call_count, 3)
        self.assertIn("2/2", controller.alert_status)

    def test_dashboard_reports_call_mode_without_secrets(self):
        alerts = self.dispatcher(telegram=False)
        controller = Controller(alerts)
        dashboard = Dashboard()
        dashboard.publish(controller, 1, False, "", 10, None)
        status = dashboard.snapshot()
        self.assertIs(status["calls"], True)
        self.assertIs(status["telegram"], False)
        self.assertNotIn(CONFIG["auth_token"], json.dumps(status))
        self.assertNotIn(CONFIG["recipients"][0]["number"], json.dumps(status))


if __name__ == "__main__":
    unittest.main()
