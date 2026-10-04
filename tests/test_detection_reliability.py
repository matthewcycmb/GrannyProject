"""Replay noisy posture observations at the actual monitor boundary."""
import unittest
from dataclasses import replace

from granny.core import Monitor, Pose, State
from granny.vision import pose_geometry
from test_monitor import STANDING, FLOOR, observe_for


class ReliabilityTests(unittest.TestCase):
    def test_clear_lying_posture_prompts_in_four_seconds_without_a_rapid_fall(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        self.assertEqual(observe_for(monitor, FLOOR, 0, 3.9), [])
        self.assertEqual(monitor.observe(FLOOR, 4.0)[0].kind, 'prompt')

    def test_brief_horizontal_pose_does_not_bypass_confirmation(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        self.assertEqual(observe_for(monitor, FLOOR, 0, 2), [])
        self.assertEqual(observe_for(monitor, STANDING, 2.1, 6), [])
        self.assertEqual(monitor.state, State.MONITORING)

    def test_calibration_uses_recent_stable_frames_not_a_single_outlier(self):
        monitor = Monitor()
        for i in range(15):
            monitor.observe(replace(STANDING, ankle_y=.90 + (i % 3 - 1) * .003), i * .1)
        monitor.observe(replace(STANDING, ankle_y=.96), 1.5)
        monitor.calibrate_recent(1.5)
        self.assertAlmostEqual(monitor.calibration.floor_y, .90, delta=.01)

    def test_calibration_rejects_stale_or_incomplete_samples(self):
        monitor = Monitor()
        monitor.observe(STANDING, 0)
        with self.assertRaises(ValueError):
            monitor.calibrate_recent(3)

    def test_one_bad_landmark_frame_does_not_erase_sustained_floor_posture(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        actions = []
        for i in range(120):
            # Low-confidence geometry can momentarily look less horizontal.
            pose = replace(FLOOR, torso_angle=40, height=.40) if i % 9 == 8 else FLOOR
            actions += monitor.observe(pose, i / 10)
        self.assertEqual([a.kind for a in actions], ['prompt'])

    def test_single_spurious_low_frame_then_tracking_loss_does_not_trigger(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        monitor.observe(FLOOR, 0)
        self.assertEqual(monitor.observe(None, 1.2), [])

    def test_partial_feet_do_not_disable_a_clear_horizontal_floor_pose(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        pose = replace(FLOOR, ankle_y=None)
        self.assertEqual([a.kind for a in observe_for(monitor, pose, 0, 4.5)], ['prompt'])
        with self.assertRaises(ValueError):
            Monitor().calibrate(pose)

    def test_landmark_to_monitor_path_accepts_hidden_feet_on_floor(self):
        from test_monitor import GeometryTests
        points = GeometryTests().landmarks()
        for i in range(33):
            points[i].visibility = points[i].presence = 0
        for indices, x, y in (((11,12),.25,.83), ((23,24),.6,.85)):
            for i in indices:
                points[i].x, points[i].y = x,y
                points[i].visibility = points[i].presence = .95
        pose = pose_geometry(points,640,480)
        self.assertIsNotNone(pose)
        self.assertIsNone(pose.ankle_y)
        monitor=Monitor()
        monitor.calibrate(STANDING)
        self.assertEqual([a.kind for a in observe_for(monitor,pose,0,4.5)],['prompt'])

    def test_calibration_rejects_moving_person_and_hidden_head(self):
        monitor=Monitor()
        for i in range(15):
            monitor.observe(replace(STANDING,hip_y=.4+i*.02),i/10)
        with self.assertRaises(ValueError):
            monitor.calibrate_recent(1.4)
        with self.assertRaises(ValueError):
            monitor.calibrate(replace(STANDING,head_visible=False))

    def test_floor_sitting_with_upright_torso_is_not_a_fall(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        sitting = Pose(.82, .66, .9, .32, 15)
        self.assertEqual(observe_for(monitor, sitting, 0, 20), [])

    def test_horizontal_body_far_above_floor_is_not_a_floor_incident(self):
        monitor = Monitor()
        monitor.calibrate(STANDING)
        self.assertEqual(observe_for(monitor, Pose(.4,.39,.42,.12,85), 0, 20), [])


if __name__ == '__main__':
    unittest.main()
