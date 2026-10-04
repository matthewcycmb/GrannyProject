"""Exercise OpenCV against a real local MJPEG server, including stalled sockets."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

import cv2
import numpy as np

from granny.camera import Camera


class MJPEGFixture:
    def __init__(self, initially_stalled=False, image=None, chunked=False):
        self.stall = threading.Event()
        self.disconnect = threading.Event()
        self.connected = threading.Event()
        self.connections = 0
        if initially_stalled:
            self.stall.set()
        if image is None:
            image = np.full((240, 320, 3), 80, dtype=np.uint8)
        okay, encoded = cv2.imencode(".jpg", image)
        assert okay
        frame = encoded.tobytes()
        boundary = b"123456789000000000000987654321" if chunked else b"frame"
        packet = (b"\r\n--" + boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                  + str(len(frame)).encode() + b"\r\nX-Timestamp: 123.000000\r\n\r\n" + frame + b"\r\n")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if chunked else "HTTP/1.0"
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.connections += 1
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + boundary.decode())
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                fixture.connected.set()
                try:
                    while not fixture.disconnect.is_set():
                        if not fixture.stall.is_set():
                            if chunked:
                                # CameraWebServer sends multipart JPEG data in HTTP chunks.
                                for offset in range(0, len(packet), 512):
                                    part = packet[offset:offset+512]
                                    self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                            else:
                                self.wfile.write(packet)
                            self.wfile.flush()
                        fixture.disconnect.wait(.03)
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/stream.mjpg"

    def close(self):
        self.disconnect.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.01)
    return False


class NetworkCameraTests(unittest.TestCase):
    def camera(self, initially_stalled=False):
        fixture = MJPEGFixture(initially_stalled)
        camera = Camera(fixture.url)
        camera.start()
        # Release the server first, even when a regression assertion fails.
        self.addCleanup(camera.close)
        self.addCleanup(fixture.close)
        return camera, fixture

    def test_mjpeg_decodes_fresh_frames(self):
        camera, _fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5))
        frame, received, sequence, error = camera.latest()
        self.assertEqual(frame.shape, (240, 320, 3))
        self.assertLess(time.monotonic() - received, .5)
        self.assertGreaterEqual(sequence, 5)
        self.assertEqual(error, "")
        camera.close()
        self.assertFalse(camera.thread.is_alive())

    def test_stalled_stream_releases_capture_on_shutdown(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5))
        fixture.stall.set()
        self.assertTrue(wait_until(lambda: time.monotonic() - camera.latest()[1] >= .3))
        camera.close()
        self.assertFalse(camera.thread.is_alive(), "A stalled Pi socket left the capture worker running")

    def test_stream_that_never_delivers_a_frame_times_out(self):
        camera, fixture = self.camera(initially_stalled=True)
        self.assertTrue(fixture.connected.wait(3))
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower(), timeout=5),
                        "An unresponsive stream must become unavailable before retrying")
        self.assertEqual(camera.latest()[2], 0)
        self.assertIn("stream", camera.latest()[3].lower())
        camera.close()
        self.assertFalse(camera.thread.is_alive())

    def test_disconnect_reports_unavailable_instead_of_new_frames(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5))
        fixture.disconnect.set()
        self.assertTrue(wait_until(lambda: bool(camera.latest()[3]), timeout=3))
        frame, _received, _sequence, error = camera.latest()
        self.assertIsNone(frame)
        self.assertTrue(error, "A disconnected stream must not look live")

    def test_stream_reconnects_with_new_frames_after_connection_returns(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5))
        fixture.disconnect.set()
        self.assertTrue(wait_until(lambda: bool(camera.latest()[3]), timeout=3))
        frame, received, sequence, error = camera.latest()
        self.assertIsNone(frame)
        self.assertEqual(received, 0)
        self.assertIn('reconnecting', error.lower())
        fixture.disconnect.clear()
        self.assertTrue(wait_until(lambda: camera.latest()[2] > sequence and not camera.latest()[3], timeout=6),
                        'A returning stream should recover without restarting Granny')
        frame, received, _sequence, error = camera.latest()
        self.assertIsNotNone(frame)
        self.assertLess(time.monotonic() - received, .5)
        self.assertEqual(error, '')

    def test_brief_hotspot_pause_resumes_without_reopening_connection(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5))
        fixture.stall.set()
        self.assertTrue(wait_until(lambda: time.monotonic() - camera.latest()[1] >= .2))
        sequence = camera.latest()[2]
        time.sleep(2)
        # The app's one-second freshness limit makes this unavailable to
        # detection, but a short Wi-Fi pause need not tear down the socket.
        self.assertGreater(time.monotonic() - camera.latest()[1], 1)
        self.assertEqual(camera.latest()[3], '')
        fixture.stall.clear()
        self.assertTrue(wait_until(lambda: camera.latest()[2] > sequence, timeout=2))
        self.assertEqual(fixture.connections, 1)

    def test_initially_unavailable_camera_can_come_online(self):
        camera, fixture = self.camera(initially_stalled=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower(), timeout=5))
        self.assertIsNone(camera.latest()[0])
        fixture.stall.clear()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 5, timeout=6))
        self.assertEqual(camera.latest()[3], '')


if __name__ == "__main__":
    unittest.main()
