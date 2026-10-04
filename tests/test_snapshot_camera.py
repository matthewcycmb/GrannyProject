"""Real HTTP JPEG responses: integrity, freshness, outages and shutdown."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

import cv2
import numpy as np

from granny.camera import Camera
from test_camera import wait_until


class SnapshotFixture:
    def __init__(self, image=None):
        self.disconnect = threading.Event()
        self.stall = threading.Event()
        self.connected = threading.Event()
        self.closing = threading.Event()
        self.connections = 0
        self.client_ports = set()
        self.repeat = False
        self.invalid = False
        self.truncated = False
        self.drip = False
        self.delay = 0
        if image is None:
            image = np.full((480, 640, 3), 80, dtype=np.uint8)
        okay, encoded = cv2.imencode('.jpg', image)
        assert okay
        self.jpeg = encoded.tobytes()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.connections += 1
                fixture.client_ports.add(self.client_address[1])
                fixture.connected.set()
                try:
                    if fixture.disconnect.is_set():
                        self.send_error(503)
                        return
                    stamp = '123.0' if fixture.repeat else str(time.monotonic_ns())
                    jpeg = b'\xff\xd8broken\xff\xd9' if fixture.invalid else fixture.jpeg
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(jpeg)))
                    self.send_header('X-Timestamp', stamp)
                    self.end_headers()
                    while fixture.stall.is_set() and not fixture.closing.wait(.02):
                        pass
                    fixture.closing.wait(fixture.delay)
                    if fixture.drip:
                        for byte in jpeg:
                            if fixture.closing.wait(.08):
                                break
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    else:
                        self.wfile.write(jpeg[:-10] if fixture.truncated else jpeg)
                    if fixture.truncated:
                        self.close_connection = True
                except (OSError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/capture'

    def close(self):
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SnapshotTests(unittest.TestCase):
    def camera(self, **settings):
        fixture = SnapshotFixture()
        for key, value in settings.items():
            if isinstance(getattr(fixture, key), threading.Event):
                getattr(fixture, key).set()
            else:
                setattr(fixture, key, value)
        camera = Camera(fixture.url)
        self.addCleanup(fixture.close)
        self.addCleanup(camera.close)
        camera.start()
        return camera, fixture

    def test_complete_jpegs_decode_and_include_transfer_time_in_age(self):
        camera, fixture = self.camera(delay=.15)
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 3))
        frame, received, _, error = camera.latest()
        self.assertEqual(frame.shape, (480, 640, 3))
        self.assertEqual(error, '')
        self.assertGreaterEqual(time.monotonic() - received, .14)
        self.assertLess(time.monotonic() - received, .4)
        self.assertGreaterEqual(fixture.connections, 3)
        self.assertEqual(len(fixture.client_ports), 1, 'Healthy requests should reuse one connection')

    def test_repeated_camera_capture_is_not_published_as_fresh(self):
        camera, _fixture = self.camera(repeat=True)
        self.assertTrue(wait_until(lambda: camera.latest()[2] == 1))
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 1)
        self.assertIsNone(camera.latest()[0])

    def test_truncated_jpeg_is_rejected_and_next_complete_image_recovers(self):
        camera, fixture = self.camera(truncated=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 0)
        fixture.truncated = False
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 2))
        self.assertEqual(camera.latest()[3], '')

    def test_corrupt_jpeg_cannot_become_a_live_frame(self):
        camera, _fixture = self.camera(invalid=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertIsNone(camera.latest()[0])
        self.assertEqual(camera.latest()[2], 0)

    def test_outage_clears_live_frame_then_recovers_without_restart(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 2))
        fixture.disconnect.set()
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertIsNone(camera.latest()[0])
        sequence = camera.latest()[2]
        fixture.disconnect.clear()
        self.assertTrue(wait_until(lambda: camera.latest()[2] > sequence))
        self.assertEqual(camera.latest()[3], '')

    def test_stalled_response_has_bounded_shutdown(self):
        camera, fixture = self.camera(stall=True)
        self.assertTrue(fixture.connected.wait(2))
        started = time.monotonic()
        camera.close()
        self.assertFalse(camera.thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(camera.latest()[2], 0)

    def test_slow_drip_exceeding_deadline_is_discarded(self):
        camera, _fixture = self.camera(drip=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower(), timeout=2))
        self.assertEqual(camera.latest()[2], 0)
        camera.close()
        self.assertFalse(camera.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
