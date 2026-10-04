"""Loopback-only dashboard; HTTP threads never mutate the detection controller."""
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit

from .core import State

ASSETS = Path(__file__).resolve().parent / "static"
ACTIONS = {"calibrate", "simulate", "okay", "help", "reset", "stop_voice", "stop_call_audio"}


@dataclass
class Command:
    action: str
    incident_id: str
    expires: float = field(default_factory=lambda: time.monotonic() + 3)
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple = (503, "Monitor is not responding. Check its current status before retrying.")


class Dashboard:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.commands = queue.Queue(maxsize=16)
        self.lock = threading.Lock()
        self.frame = None
        self.updated = 0
        self.data = {"running": False, "state": "Starting", "state_key": "STARTING"}

    def publish(self, controller, now, telegram, camera_status, fps, frame):
        monitor = controller.monitor
        data = {
            "running": True, "state": monitor.state.value, "state_key": monitor.state.name,
            "reason": monitor.reason, "incident_id": monitor.incident_id,
            "remaining_seconds": max(0, math.ceil(monitor.deadline - now)) if monitor.deadline else None,
            "camera": camera_status or monitor.sensor_status,
            "audio": controller.audio_status if controller.voice or controller.call_listening else "Button mode: use I’m okay or Help",
            "heard": controller.heard, "alerts": controller.alert_status,
            "telegram": telegram, "calibrated": bool(monitor.calibration),
            "calls": controller.alerts.calls,
            "voice_repeating": controller.voice_repeating,
            "voice_backup_ready": controller.backup_ready,
            "voice_enabled": controller.voice is not None,
            "call_listening": controller.call_listening,
            "listen_calls": bool(getattr(getattr(controller.alerts, "call_relay", None), "stream_audio", False)),
            "confirm_calls": bool(getattr(controller.alerts, "call_relay", None)),
            "deliveries": controller.deliveries,
            "fps": round(fps, 1), "frame_available": frame is not None,
            "tracking_available": (frame is not None and not camera_status
                                   and monitor.sensor_status == "Body tracked"),
        }
        with self.lock:
            self.data, self.frame, self.updated = data, frame, time.monotonic()

    def snapshot(self):
        with self.lock:
            data = dict(self.data)
            data["running"] = data["running"] and time.monotonic() - self.updated < 3
        data["control_token"] = self.token
        return data

    def camera_frame(self):
        with self.lock:
            return self.frame if time.monotonic() - self.updated < 3 else None

    def apply_commands(self, controller, pose, now, photo):
        # Only the monitor thread calls this, serializing browser and voice actions.
        for _ in range(16):
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            try:
                if time.monotonic() >= command.expires:
                    continue
                monitor = controller.monitor
                if command.incident_id != monitor.incident_id:
                    raise ValueError("The incident changed. Review the current status and try again.")
                if command.action == "calibrate":
                    if pose is None:
                        raise ValueError("Stand still with your head and feet visible, then calibrate again.")
                    monitor.calibrate_recent(now)
                    controller.alert_status = "Standing position calibrated"
                elif command.action == "simulate":
                    if monitor.state in {State.CHECKING, State.ALERTED}:
                        raise ValueError("Finish or reset this incident before starting another check.")
                    controller.begin_check(now, photo)
                elif command.action == "help":
                    if monitor.state == State.ALERTED:
                        raise ValueError("This alert has already started.")
                    controller.request_help(now, source="Browser")
                elif command.action == "okay":
                    if monitor.state != State.CHECKING:
                        raise ValueError("There is no active response check.")
                    controller.respond("i am okay", now,
                                       source="Browser")
                elif command.action == "reset":
                    controller.reset()
                elif command.action == "stop_voice":
                    if not controller.stop_voice():
                        raise ValueError("There is no repeated alert update to stop.")
                elif command.action == "stop_call_audio":
                    if not controller.stop_call_audio():
                        raise ValueError("There is no call audio playing on this Mac.")
                command.result = (200, "Action applied")
            except ValueError as exc:
                command.result = (409, str(exc).replace("press C", "click Calibrate"))
            finally:
                command.done.set()


def camera_jpeg(frame, landmarks, calibration):
    """Draw landmarks on a copy; incident photos keep the original camera image."""
    import cv2
    from .vision import CONNECTIONS
    video = frame.copy()
    height, width = video.shape[:2]
    for a, b in CONNECTIONS:
        if landmarks and min(landmarks[a].visibility, landmarks[b].visibility) >= .5:
            start = (int(landmarks[a].x * width), int(landmarks[a].y * height))
            end = (int(landmarks[b].x * width), int(landmarks[b].y * height))
            cv2.line(video, start, end, (175, 220, 93), 2, cv2.LINE_AA)
    if calibration:
        floor = int(calibration.floor_y * height)
        for x in range(0, width, 24):
            cv2.line(video, (x, floor), (min(x + 12, width), floor), (99, 190, 246), 2)
    okay, encoded = cv2.imencode(".jpg", video, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return encoded.tobytes() if okay else None


class DashboardServer:
    def __init__(self, dashboard, port=8000):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, body, content_type="application/json"):
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; "
                                 "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def allowed(self):
                port = self.server.server_port
                hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
                origin = self.headers.get("Origin")
                if (self.headers.get("Host") not in hosts
                        or self.headers.get("Sec-Fetch-Site") == "cross-site"
                        or (origin and origin not in {f"http://{host}" for host in hosts})):
                    self.reply(403, {"error": "Open the dashboard directly on localhost."})
                    return False
                return True

            def do_GET(self):
                if not self.allowed():
                    return
                path = urlsplit(self.path).path
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/steps.js": ("steps.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path == "/api/status":
                    self.reply(200, dashboard.snapshot())
                elif path == "/api/frame.jpg":
                    frame = dashboard.camera_frame()
                    self.reply(200 if frame else 503, frame or b"Camera unavailable", "image/jpeg")
                elif path in assets:
                    name, mime = assets[path]
                    self.reply(200, (ASSETS / name).read_bytes(), mime)
                elif path == "/favicon.ico":
                    self.reply(204, b"")
                else:
                    self.reply(404, {"error": "Not found"})

            def do_POST(self):
                if not self.allowed():
                    return
                if self.path != "/api/action":
                    self.reply(404, {"error": "Not found"})
                    return
                if not secrets.compare_digest(self.headers.get("X-Granny-Token", ""), dashboard.token):
                    self.reply(403, {"error": "Refresh the dashboard before using its controls."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 or self.headers.get_content_type() != "application/json":
                        raise ValueError
                    data = json.loads(self.rfile.read(length))
                    if (not isinstance(data, dict) or not isinstance(data.get("action"), str)
                            or data["action"] not in ACTIONS or not isinstance(data.get("incident_id"), str)):
                        raise ValueError
                except (ValueError, UnicodeDecodeError):
                    self.reply(400, {"error": "Invalid action"})
                    return
                command = Command(data["action"], data["incident_id"])
                try:
                    dashboard.commands.put_nowait(command)
                except queue.Full:
                    self.reply(503, {"error": "Monitor is busy. Check its status before retrying."})
                    return
                command.done.wait(3.2)
                status, message = command.result
                self.reply(status, {"message" if status == 200 else "error": message})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://localhost:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()
