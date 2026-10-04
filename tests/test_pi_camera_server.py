"""Test the Pi HTTP stream with real JPEGs; actual Picamera2 needs Pi hardware."""
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

import cv2
import numpy as np

from granny.camera import Camera
from pi_camera_server import Frames, make_server
from test_camera import wait_until


class PiStreamTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        self.server = make_server(("127.0.0.1", 0), self.frames)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.stop = threading.Event()
        okay, jpeg = cv2.imencode(".jpg", np.full((480, 640, 3), (25, 110, 220), dtype=np.uint8))
        assert okay
        self.jpeg = jpeg.tobytes()

        def publish():
            while not self.stop.is_set():
                self.frames.write(self.jpeg)
                self.stop.wait(1 / 15)

        self.publisher = threading.Thread(target=publish, daemon=True)
        self.publisher.start()

    def tearDown(self):
        self.stop.set()
        self.publisher.join(timeout=2)
        self.frames.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def camera(self):
        camera = Camera(self.url + "/stream.mjpg")
        camera.start()
        self.addCleanup(camera.close)
        return camera

    def test_two_clients_can_decode_and_reconnect(self):
        first = self.camera()
        second = self.camera()
        self.assertTrue(wait_until(lambda: min(first.latest()[2], second.latest()[2]) >= 3))
        frame = first.latest()[0]
        self.assertEqual(frame.shape, (480, 640, 3))
        np.testing.assert_allclose(frame[100, 100], (25, 110, 220), atol=3)
        first.close()
        self.assertFalse(first.thread.is_alive())
        third = self.camera()
        self.assertTrue(wait_until(lambda: third.latest()[2] >= 3))
        self.assertEqual(second.latest()[3], "")

    def test_stalled_camera_does_not_rebroadcast_an_old_frame(self):
        camera = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 3))
        self.stop.set()
        self.publisher.join(timeout=2)
        self.assertTrue(wait_until(lambda: bool(camera.latest()[3]), timeout=4))
        self.assertIsNone(camera.latest()[0])
        self.assertEqual(camera.latest()[1], 0)
        self.assertIn('reconnecting', camera.latest()[3].lower())
        camera.close()
        self.assertFalse(camera.thread.is_alive())

    def test_preview_works_and_local_files_are_not_served(self):
        with urlopen(self.url + "/", timeout=3) as response:
            self.assertIn(b'/stream.mjpg', response.read())
        for path in ("/.granny/telegram.json", "/pi_camera_server.py", "/../../etc/passwd"):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.url + path, timeout=3)
            self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
