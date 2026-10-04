"""Exercise complete-frame assembly and recovery using real local UDP sockets."""
import socket
import threading
import time
import unittest

import cv2
import numpy as np

from granny.camera import Camera
from granny.udp_camera import REQUEST, REQUEST_MAGIC, HEADER, FRAME_MAGIC, CHUNK
from test_camera import wait_until


class UDPFixture:
    def __init__(self, image=None):
        self.disconnect = threading.Event()
        self.stop = threading.Event()
        self.omit = False
        self.repeat = False
        self.corrupt = False
        self.reverse = False
        self.wrong_nonce = False
        self.inconsistent = False
        image = np.full((480, 640, 3), 80, dtype=np.uint8) if image is None else image
        self.jpeg = cv2.imencode('.jpg', image)[1].tobytes()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.1)
        self.url = f'udp://127.0.0.1:{self.socket.getsockname()[1]}'
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                request, peer = self.socket.recvfrom(100)
                if len(request) != REQUEST.size:
                    continue
                magic, nonce = REQUEST.unpack(request)
                if magic != REQUEST_MAGIC or self.disconnect.is_set():
                    continue
                stamp = 123000000 if self.repeat else time.monotonic_ns() // 1000
                jpeg = b'\xff\xd8broken\xff\xd9' if self.corrupt else self.jpeg
                offsets = list(range(0, len(jpeg), CHUNK))
                if self.reverse:
                    offsets.reverse()
                for offset in offsets:
                    if self.omit and offset == CHUNK:
                        continue
                    timestamp = stamp + (1 if self.inconsistent and offset == CHUNK else 0)
                    packet = HEADER.pack(FRAME_MAGIC, (nonce - 1) & 0xffffffff if self.wrong_nonce else nonce,
                                         len(jpeg), offset, timestamp // 1000000, timestamp % 1000000)
                    packet += jpeg[offset:offset + CHUNK]
                    self.socket.sendto(packet, peer)
                    if self.reverse:  # Duplicate, reordered packets are harmless.
                        self.socket.sendto(packet, peer)
            except (OSError, ValueError):
                pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.socket.close()


class UDPTests(unittest.TestCase):
    def camera(self, **settings):
        fixture = UDPFixture()
        for key, value in settings.items():
            setattr(fixture, key, value)
        camera = Camera(fixture.url)
        self.addCleanup(fixture.close)
        self.addCleanup(camera.close)
        camera.start()
        return camera, fixture

    def test_reordered_duplicate_chunks_make_one_fresh_complete_frame(self):
        camera, _ = self.camera(reverse=True)
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 3))
        frame, received, _, error = camera.latest()
        self.assertEqual(frame.shape, (480, 640, 3))
        self.assertLess(time.monotonic() - received, .3)
        self.assertEqual(error, '')

    def test_incomplete_frames_are_discarded_and_later_frames_recover(self):
        camera, fixture = self.camera(omit=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 0)
        fixture.omit = False
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 2))

    def test_old_request_datagrams_cannot_be_used_for_a_new_frame(self):
        camera, _ = self.camera(wrong_nonce=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 0)

    def test_chunks_from_different_capture_timestamps_cannot_be_mixed(self):
        camera, _ = self.camera(inconsistent=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 0)

    def test_repeated_camera_timestamp_cannot_keep_a_frozen_frame_live(self):
        camera, _ = self.camera(repeat=True)
        self.assertTrue(wait_until(lambda: camera.latest()[2] == 1))
        self.assertTrue(wait_until(lambda: camera.latest()[0] is None))
        self.assertEqual(camera.latest()[2], 1)

    def test_outage_stops_new_observations_and_recovers(self):
        camera, fixture = self.camera()
        self.assertTrue(wait_until(lambda: camera.latest()[2] >= 2))
        fixture.disconnect.set()
        self.assertTrue(wait_until(lambda: camera.latest()[0] is None))
        sequence = camera.latest()[2]
        self.assertEqual(camera.latest()[1], 0)
        fixture.disconnect.clear()
        self.assertTrue(wait_until(lambda: camera.latest()[2] > sequence))
        self.assertEqual(camera.latest()[3], '')

    def test_corrupt_frame_is_rejected(self):
        camera, _ = self.camera(corrupt=True)
        self.assertTrue(wait_until(lambda: 'reconnecting' in camera.latest()[3].lower()))
        self.assertEqual(camera.latest()[2], 0)

    def test_shutdown_is_bounded_during_an_outage(self):
        camera, fixture = self.camera()
        fixture.disconnect.set()
        started = time.monotonic()
        camera.close()
        self.assertFalse(camera.thread.is_alive())
        self.assertLess(time.monotonic() - started, .8)


if __name__ == '__main__':
    unittest.main()
