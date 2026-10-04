"""Verify the complete app through a Pi-style MJPEG stream and its HTTP controls.

Uses the real pose model, a public sample image and fake notification providers.
Does not use your camera/microphone, load real credentials, or contact anyone.
All incident photos and delivery records are isolated in a temporary directory.
"""
import argparse
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import cv2
import granny_app

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
from test_camera import MJPEGFixture
from test_snapshot_camera import SnapshotFixture
from test_udp_camera import UDPFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--esp32', action='store_true', help='Use ESP32-style HTTP chunked MJPEG')
    parser.add_argument('--snapshot', action='store_true', help='Use ESP32 complete-JPEG requests')
    parser.add_argument('--udp', action='store_true', help='Use ESP32 requested UDP JPEG frames')
    args = parser.parse_args()
    sample = cv2.imread(str(ROOT / ".granny/test-media/pose.jpg"))
    if sample is None:
        raise RuntimeError("Run verify_runtime.py first to prepare the public sample image.")
    sample = cv2.resize(sample, (640, round(sample.shape[0] * 640 / sample.shape[1])))
    fixture = UDPFixture(image=sample) if args.udp else SnapshotFixture(image=sample) if args.snapshot else MJPEGFixture(image=sample, chunked=args.esp32)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    report = {"source": "local simulated ESP32 UDP frames" if args.udp else "local simulated ESP32 snapshots" if args.snapshot else "local simulated ESP32 chunked MJPEG stream" if args.esp32 else "local simulated Pi MJPEG stream",
              "checks": [], "real_notifications_sent": 0}
    failures = []
    call_config = {"account_sid": "AC" + "a" * 32, "auth_token": "b" * 32,
                   "from_number": "+14155550100", "recipients": [
                       {"number": "+16045550101", "name": "Test one"},
                       {"number": "+16045550102", "name": "Test two"}]}
    telegram_config = {"token": "0:FAKE", "recipients": [{"id": str(i)} for i in (1, 2, 3)]}

    def request(path, fields=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            body = json.dumps(fields) if fields is not None else None
            headers = {"Content-Type": "application/json", "X-Granny-Token": token} if token else {}
            connection.request("POST" if fields is not None else "GET", path, body, headers)
            response = connection.getresponse()
            content = response.read()
            assert response.status == 200, (response.status, content[:200])
            return json.loads(content) if path != "/api/frame.jpg" else content
        finally:
            connection.close()

    def until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            try:
                state = request("/api/status")
                if predicate(state):
                    return state
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.04)
        raise AssertionError(f"Timed out waiting for dashboard state: {state}")

    def action(name):
        state = request("/api/status")
        request("/api/action", {"action": name, "incident_id": state["incident_id"]}, state["control_token"])

    def passed(name):
        report["checks"].append(name)
        print("PASS:", name, flush=True)

    def exercise():
        try:
            state = until(lambda state: state.get("frame_available") and state.get("camera") == "Body tracked", 15)
            assert state["telegram"] and state["calls"]
            assert request("/api/frame.jpg").startswith(b"\xff\xd8")
            passed("Real pose tracking and JPEG preview through a network camera source")
            # Calibration now requires a recent stable standing window.
            time.sleep(1.6)
            action("calibrate")
            until(lambda state: state["state_key"] == "MONITORING")
            action("simulate")
            until(lambda state: state["state_key"] == "CHECKING")
            action("okay")
            until(lambda state: state["state_key"] == "COOLDOWN")
            assert calls.call_count == 0 and telegram.call_count == 0
            passed("Browser calibration and reassurance cancel both notification channels")
            action("reset")
            action("simulate")
            action("help")
            until(lambda state: "Twilio accepted 2/2" in state["alerts"] and "Telegram accepted 3/3" in state["alerts"])
            assert calls.call_count == 2 and telegram.call_count == 3
            passed("Help triggers two mock calls and three mock photo messages")
            action("reset")
            action("simulate")
            until(lambda state: state["state_key"] == "CHECKING")
            fixture.disconnect.set()
            state = until(lambda state: not state["frame_available"] and "stream" in state["camera"].lower())
            assert state["state_key"] == "CHECKING"
            passed("Disconnected camera is marked unavailable while the response timer continues")
            until(lambda state: state["state_key"] == "ALERTED" and "Twilio accepted 2/2" in state["alerts"]
                  and "Telegram accepted 3/3" in state["alerts"], 48)
            assert calls.call_count == 4 and telegram.call_count == 6
            assert all(call.args[1] == "sendPhoto" and call.args[3][0].startswith(b"\xff\xd8")
                       for call in telegram.call_args_list)
            passed("Silence still alerts after stream loss, preserving the incident photo")
            action("reset")
            until(lambda state: state["state_key"] == "MONITORING" and not state["frame_available"])
            fixture.disconnect.clear()
            until(lambda state: state["state_key"] == "MONITORING" and state["frame_available"]
                  and state["camera"] == "Body tracked", 8)
            assert calls.call_count == 4 and telegram.call_count == 6
            passed("Video reconnects automatically without replaying notifications")
            report["mock_call_requests"] = calls.call_count
            report["mock_photo_messages"] = telegram.call_count
        except Exception as exc:
            failures.append(str(exc))

    try:
        with tempfile.TemporaryDirectory(prefix="granny-stream-check-") as directory:
            arguments = ["granny_app.py", "--web", "--port", str(port), "--source", fixture.url,
                         "--no-audio", "--seconds", "65", "--telegram", "--calls"]
            with patch.object(granny_app, "ROOT", Path(directory)), patch.object(sys, "argv", arguments), \
                    patch("granny.calls.load_config", return_value=call_config), \
                    patch("granny.calls.TwilioClient.check", return_value={}), \
                    patch("granny.calls.TwilioClient.create_call", return_value={"sid": "CA" + "c" * 32, "status": "queued"}) as calls, \
                    patch("telegram_setup.load_config", return_value=telegram_config), \
                    patch("telegram_setup.api", return_value={"message_id": 1}) as telegram:
                worker = threading.Thread(target=exercise, daemon=True)
                worker.start()
                result = granny_app.main()
                worker.join(timeout=5)
                assert not worker.is_alive(), "HTTP verification did not finish"
                assert result == 0 and not failures, failures or f"App exited {result}"
        report["status"] = "passed"
        report_name = 'esp32-udp-report.json' if args.udp else 'esp32-snapshot-report.json' if args.snapshot else 'esp32-stream-report.json' if args.esp32 else 'stream-report.json'
        (ROOT / ".granny/test-media" / report_name).write_text(json.dumps(report, indent=2) + "\n")
        print("PASS: full network-camera workflow. No real notifications or persistent incident data.", flush=True)
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
