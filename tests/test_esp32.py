"""Use ESP32-style HTTP chunked MJPEG; no actual hardware or notifications."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import granny_esp32 as esp
from test_camera import MJPEGFixture, wait_until
from test_snapshot_camera import SnapshotFixture
from test_udp_camera import UDPFixture
from granny.camera import Camera


class ESP32Tests(unittest.TestCase):
    def test_camera_outage_does_not_block_starting_voice_backup_and_dashboard(self):
        with patch.object(esp, 'check_stream', side_effect=RuntimeError('unavailable')), \
                patch.object(esp.os, 'execv') as launch:
            esp.main(['--stream-url', 'udp://camera:82'])
            launch.assert_called_once()
            self.assertIn('--web', launch.call_args.args[1])
            self.assertIn('udp://camera:82', launch.call_args.args[1])

    def test_udp_video_passes_check_and_url_is_valid(self):
        server = UDPFixture()
        self.addCleanup(server.close)
        self.assertEqual(esp.stream_url(server.url), server.url)
        report = esp.check_stream(server.url, sample_seconds=1)
        self.assertEqual((report['width'], report['height']), (640, 480))
        self.assertGreaterEqual(report['frames'], 4)
        for url in ('udp://camera', 'udp://camera:82/path', 'udp://user:password@camera:82', 'udp://camera:82?key=1'):
            with self.assertRaises(ValueError):
                esp.stream_url(url)

    def test_snapshot_check_can_save_without_starting_alerts(self):
        server = SnapshotFixture()
        self.addCleanup(server.close)
        with patch.object(esp.os, 'execv') as launch, patch.object(esp, 'save_url') as save:
            self.assertEqual(esp.main(['--stream-url', server.url, '--check', '--save', '--calls']), 0)
            save.assert_called_once_with(server.url)
            launch.assert_not_called()

    def fixture(self, **kwargs):
        server = MJPEGFixture(image=np.full((480,640,3),80,dtype=np.uint8),chunked=True,**kwargs)
        self.addCleanup(server.close)
        return server

    def test_chunked_esp32_video_passes_real_capture_check(self):
        server=self.fixture()
        report=esp.check_stream(server.url,sample_seconds=.3)
        self.assertEqual((report['width'],report['height']),(640,480))
        self.assertGreaterEqual(report['frames'],4)
        self.assertGreater(report['fps'],5)

    def test_chunked_stall_is_not_a_healthy_camera(self):
        server=self.fixture(initially_stalled=True)
        with self.assertRaisesRegex(RuntimeError,'No stable camera stream'):
            esp.check_stream(server.url,sample_seconds=.3,timeout=4)

    def test_chunked_disconnect_invalidates_live_frames(self):
        server=self.fixture()
        camera=Camera(server.url)
        camera.start()
        self.addCleanup(camera.close)
        self.assertTrue(wait_until(lambda: camera.latest()[2]>=5))
        server.disconnect.set()
        self.assertTrue(wait_until(lambda: bool(camera.latest()[3]),timeout=3))
        self.assertIsNone(camera.latest()[0])
        self.assertTrue(camera.latest()[3])

    def test_saved_stream_url_is_private_and_rejects_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'esp32.json'
            esp.save_url('http://192.168.1.50:81/stream',path)
            self.assertEqual(esp.saved_url(path),'http://192.168.1.50:81/stream')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            for invalid in ('file:///etc/passwd','http://admin:secret@camera/stream','http://camera/','http://camera:70000/stream','http://camera:0/stream'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    esp.stream_url(invalid)

    def test_check_mode_never_launches_detection_or_notifications(self):
        server=self.fixture()
        with patch.object(esp.os,'execv') as launch:
            self.assertEqual(esp.main(['--stream-url',server.url,'--check','--telegram','--calls']),0)
            launch.assert_not_called()

    def test_failed_check_neither_saves_url_nor_launches_app(self):
        with patch.object(esp,'check_stream',side_effect=RuntimeError('unavailable')), \
                patch.object(esp,'save_url') as save, patch.object(esp.os,'execv') as launch:
            self.assertEqual(esp.main(['--stream-url','http://camera:81/stream','--save']),1)
            save.assert_not_called()
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
