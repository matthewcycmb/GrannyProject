import http.client
import json
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from granny.alerts import AlertDispatcher
from granny.controller import Controller
from granny.core import Monitor, State, Timing
from granny.web import Command, Dashboard, DashboardServer
from test_monitor import STANDING


class DashboardCommandTests(unittest.TestCase):
    def setUp(self):
        self.alerts = Mock()
        self.alerts.events = queue.Queue()
        self.controller = Controller(self.alerts)
        self.dashboard = Dashboard()

    def command(self, action, incident=None, now=1, pose=STANDING, expired=False):
        command = Command(action, self.controller.monitor.incident_id if incident is None else incident)
        if expired:
            command.expires = time.monotonic() - 1
        self.dashboard.commands.put(command)
        self.dashboard.apply_commands(self.controller, pose, now, b"incident photo")
        self.assertTrue(command.done.is_set())
        return command.result[0]

    def test_calibrate_then_browser_reassurance_cancels(self):
        for i in range(11):
            self.controller.observe(STANDING, i / 10)
        self.assertEqual(self.command("calibrate"), 200)
        self.assertEqual(self.controller.monitor.state, State.MONITORING)
        self.command("simulate")
        self.assertEqual(self.command("okay", now=2), 200)
        self.assertEqual(self.controller.monitor.state, State.COOLDOWN)
        self.assertEqual(self.controller.heard, "Browser: i am okay")
        self.alerts.submit.assert_not_called()

    def test_bad_calibration_shows_actionable_error(self):
        self.assertEqual(self.command("calibrate", pose=None), 409)
        self.assertEqual(self.controller.monitor.state, State.UNCALIBRATED)

    def test_help_uses_incident_photo_and_cannot_repeat(self):
        self.command("simulate")
        self.assertEqual(self.command("help", now=2), 200)
        self.assertEqual(self.command("help", now=3), 409)
        self.assertEqual(self.command("simulate", now=3), 409)
        self.alerts.submit.assert_called_once_with(self.controller.monitor.incident_id,
                                                  b"incident photo", "Possible fall; person requested help")

    def test_old_tab_cannot_reset_or_cancel_new_incident(self):
        self.command("simulate")
        old = self.controller.monitor.incident_id
        self.command("reset")
        self.command("simulate", now=2)
        for action in ("okay", "help", "reset", "calibrate", "simulate"):
            self.assertEqual(self.command(action, incident=old, now=3), 409)
        self.assertEqual(self.controller.monitor.state, State.CHECKING)

    def test_expired_commands_do_not_run_later(self):
        self.assertEqual(self.command("simulate", expired=True), 503)
        self.assertEqual(self.controller.monitor.state, State.UNCALIBRATED)

    def test_stop_voice_keeps_alert_latched_and_does_not_send_again(self):
        voice = Mock()
        voice.events = queue.Queue()
        self.controller.voice = voice
        self.command("simulate")
        self.assertEqual(self.command("stop_voice"), 409)
        self.command("help", now=2)
        self.assertTrue(self.controller.voice_repeating)
        self.assertEqual(self.command("stop_voice", now=3), 200)
        self.assertFalse(self.controller.voice_repeating)
        self.assertEqual(self.controller.monitor.state, State.ALERTED)
        self.assertEqual(self.command("stop_voice", now=4), 409)
        self.alerts.submit.assert_called_once()

    def test_monitor_stall_marks_status_and_frame_unavailable(self):
        self.dashboard.publish(self.controller, 1, False, "", 10, b"jpeg")
        self.assertTrue(self.dashboard.snapshot()["running"])
        self.dashboard.updated -= 4
        self.assertFalse(self.dashboard.snapshot()["running"])
        self.assertIsNone(self.dashboard.camera_frame())

    def test_live_camera_and_body_tracking_have_separate_availability(self):
        self.controller.monitor.calibrate(STANDING)
        for message in ('No person detected', 'Multiple people: use one person',
                        'Show your shoulders and hips clearly'):
            self.controller.observe(None, 1, unavailable=message)
            self.dashboard.publish(self.controller, 1, False, '', 7, b'jpeg')
            status = self.dashboard.snapshot()
            self.assertTrue(status['frame_available'])
            self.assertFalse(status['tracking_available'])
            self.assertEqual(status['state_key'], 'MONITORING')
        self.controller.observe(STANDING, 2)
        self.dashboard.publish(self.controller, 2, False, '', 7, b'jpeg')
        self.assertTrue(self.dashboard.snapshot()['tracking_available'])
        self.dashboard.publish(self.controller, 3, False, 'Camera frames are stale', 7, None)
        self.assertFalse(self.dashboard.snapshot()['tracking_available'])
        self.assertFalse(self.dashboard.snapshot()['frame_available'])


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Mock(side_effect=AssertionError("No Telegram calls allowed"))
        self.alerts = AlertDispatcher(self.directory.name, client=self.client)
        self.addCleanup(self.alerts.close)
        self.controller = Controller(self.alerts, monitor=Monitor(Timing(response_seconds=.15)))
        self.dashboard = Dashboard()
        self.server = DashboardServer(self.dashboard, port=0)
        self.server.start()
        self.addCleanup(self.server.close)
        self.port = self.server.server.server_port
        self.stop = threading.Event()

        def run_monitor():
            while not self.stop.is_set():
                now = time.monotonic()
                self.dashboard.apply_commands(self.controller, STANDING, now, b"jpeg")
                self.controller.tick(now)
                self.dashboard.publish(self.controller, now, False, "", 10, b"jpeg")
                self.stop.wait(.005)

        self.worker = threading.Thread(target=run_monitor)
        self.worker.start()
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        self.stop.set()
        self.worker.join(timeout=2)

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def post(self, action, incident="", **headers):
        return self.request("/api/action", "POST", json.dumps({"action": action, "incident_id": incident}),
                            {"Content-Type": "application/json", "X-Granny-Token": self.dashboard.token,
                             **headers})

    def test_assets_status_and_frame_served_without_exposing_files(self):
        for path in ("/", "/style.css", "/app.js", "/steps.js", "/api/status", "/api/frame.jpg"):
            code, body, headers = self.request(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/.granny/telegram.json", "/../telegram_setup.py", "/requirements.txt"):
            self.assertEqual(self.request(path)[0], 404)
        status = json.loads(self.request("/api/status")[1])
        self.assertFalse(status["telegram"])
        self.assertNotIn("recipients", status)

    def test_http_trigger_timeout_and_dry_run_delivery(self):
        self.assertEqual(self.post("simulate")[0], 200)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = json.loads(self.request("/api/status")[1])
            if "recorded" in status.get("alerts", ""):
                break
            self.stop.wait(.01)
        self.assertEqual(status["state_key"], "ALERTED")
        self.assertEqual(status["alerts"], "Demo alert recorded. Telegram is OFF.")
        self.client.assert_not_called()
        self.assertEqual(self.post("reset", status["incident_id"])[0], 200)

    def test_immediate_help_http_action_skips_voice_check_and_does_not_repeat(self):
        self.assertEqual(self.post("help")[0], 200)
        self.assertEqual(self.controller.monitor.state, State.ALERTED)
        self.assertIsNone(self.controller.monitor.deadline)
        incident = self.controller.monitor.incident_id
        self.assertEqual(self.post("help", incident)[0], 409)
        self.assertEqual(self.controller.monitor.incident_id, incident)
        self.client.assert_not_called()

    def test_cross_origin_requests_are_rejected(self):
        self.assertEqual(self.post("simulate", Origin="https://unrelated.example")[0], 403)
        self.assertEqual(self.request("/api/frame.jpg", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/status", headers={"Host": "rebind.example"})[0], 403)
        self.assertEqual(self.post("simulate", **{"X-Granny-Token": "wrong"})[0], 403)
        self.assertEqual(self.controller.monitor.state, State.UNCALIBRATED)

    def test_invalid_actions_are_rejected(self):
        for action in ("enable_telegram", "shutdown", [], None):
            self.assertEqual(self.post(action)[0], 400)
        self.assertEqual(self.request("/api/action")[0], 404)
        self.assertEqual(self.controller.monitor.state, State.UNCALIBRATED)


if __name__ == "__main__":
    unittest.main()
