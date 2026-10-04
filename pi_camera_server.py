#!/usr/bin/env python3
"""Pi-only camera stream. Run with Raspberry Pi OS system Python, not the Mac venv."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import sys
import threading


class Frames(io.BufferedIOBase):
    """Keep only the latest encoded frame; never record video to disk."""

    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.stopped = False

    def write(self, data):
        with self.condition:
            self.frame = bytes(data)
            self.sequence += 1
            self.condition.notify_all()
        return len(data)

    def next_frame(self, previous):
        with self.condition:
            ready = self.condition.wait_for(
                lambda: self.stopped or self.sequence != previous, timeout=2
            )
            if not ready or self.stopped:
                return None, previous
            return self.frame, self.sequence

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


def make_server(address, frames):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.connection.settimeout(5)
            if self.path == "/":
                page = (b'<!doctype html><title>Granny Pi camera</title>'
                        b'<h1>Granny Pi camera</h1><img src="/stream.mjpg" '
                        b'alt="Live Pi camera" style="max-width:100%">')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                return
            if self.path != "/stream.mjpg":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            previous = 0
            try:
                while True:
                    frame, previous = frames.next_frame(previous)
                    if frame is None:
                        return
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode("ascii") + b"\r\n\r\n"
                        + frame + b"\r\n"
                    )
                    self.wfile.flush()
            except OSError:
                # A disconnected browser/Mac must not stop the Pi camera.
                return

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; default uses an SSH tunnel")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        from picamera2 import Picamera2
        from picamera2.encoders import MJPEGEncoder
        from picamera2.outputs import FileOutput
        from libcamera import controls
    except ImportError:
        print("Run this on the Raspberry Pi with python3. Install its camera library with:\n"
              "sudo apt install -y python3-picamera2 --no-install-recommends", file=sys.stderr)
        return 1

    frames = Frames()
    camera = server = None
    recording = False
    try:
        server = make_server((args.host, args.port), frames)
        camera = Picamera2()
        camera.configure(camera.create_video_configuration(
            main={"size": (640, 480)}, controls={"FrameRate": 15}
        ))
        if "AfMode" in camera.camera_controls:
            camera.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        camera.start_recording(MJPEGEncoder(), FileOutput(frames))
        recording = True
        print(f"Pi camera ready at http://{args.host}:{args.port}/stream.mjpg", flush=True)
        print("Keep this Terminal open. Ctrl+C stops the camera.", flush=True)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Pi camera could not start: {exc}\n"
              "Check rpicam-hello --list-cameras and close other camera apps.", file=sys.stderr)
        return 1
    finally:
        frames.stop()
        if server is not None:
            server.server_close()
        if camera is not None:
            try:
                if recording:
                    camera.stop_recording()
            finally:
                camera.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
