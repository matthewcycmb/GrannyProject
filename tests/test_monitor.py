from types import SimpleNamespace
import unittest

from granny.core import Monitor, Pose, Response, State, Timing, classify_response, acknowledges_update
from granny.vision import pose_geometry

STANDING = Pose(.55, .30, .90, .76, 0)
FLOOR = Pose(.85, .83, .87, .14, 85)


def observe_for(monitor, pose, start, seconds):
    actions = []
    for step in range(round(seconds * 10) + 1):
        actions.extend(monitor.observe(pose, start + step / 10))
    return actions


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor()
        self.monitor.calibrate(STANDING)

    def test_calibration_requires_a_full_upright_body(self):
        for pose in (None, FLOOR, Pose(.5, .3, .6, .3, 0)):
            with self.subTest(pose=pose), self.assertRaises(ValueError):
                Monitor().calibrate(pose)

    def test_no_detection_before_calibration(self):
        monitor = Monitor()
        self.assertEqual(observe_for(monitor, FLOOR, 0, 30), [])
        self.assertEqual(monitor.state, State.UNCALIBRATED)

    def test_normal_sitting_bending_and_kneeling_do_not_trigger(self):
        for pose in (STANDING, Pose(.65, .4, .9, .7, 10),
                     Pose(.55, .65, .9, .5, 70), Pose(.7, .5, .9, .55, 10)):
            with self.subTest(pose=pose):
                self.assertEqual(observe_for(self.monitor, pose, 0, 12), [])
                self.assertEqual(self.monitor.state, State.MONITORING)

    def test_rapid_drop_requires_sustained_floor_position(self):
        self.monitor.observe(STANDING, 0)
        self.assertEqual(observe_for(self.monitor, FLOOR, .5, 2.9), [])
        actions = self.monitor.observe(FLOOR, 3.6)
        self.assertEqual([a.kind for a in actions], ["prompt"])
        self.assertEqual(self.monitor.state, State.CHECKING)

    def test_person_already_down_uses_longer_threshold(self):
        ambiguous = Pose(.85, .73, .87, .4, 60)
        self.assertEqual(observe_for(self.monitor, ambiguous, 0, 9.9), [])
        self.assertEqual(self.monitor.observe(ambiguous, 10.1)[0].kind, "prompt")

    def test_recovery_before_confirmation_cancels_candidate(self):
        self.monitor.observe(STANDING, 0)
        observe_for(self.monitor, FLOOR, .2, 1.0)
        self.monitor.observe(STANDING, 1.3)
        self.assertEqual(self.monitor.state, State.MONITORING)

    def test_missing_frames_do_not_count_as_continuous_floor_evidence(self):
        self.monitor.observe(FLOOR, 0)
        self.assertEqual(self.monitor.observe(FLOOR, 15), [])
        self.assertEqual(self.monitor.state, State.POSSIBLE_FALL)

    def test_missing_camera_alone_does_not_trigger(self):
        self.assertEqual(self.monitor.observe(None, 100), [])
        self.assertEqual(self.monitor.state, State.MONITORING)

    def test_tracking_loss_after_candidate_starts_voice_check(self):
        observe_for(self.monitor, FLOOR, 0, 2.1)
        self.assertEqual(self.monitor.observe(None, 3.2)[0].kind, "prompt")

    def test_silence_repeats_with_listening_pauses_then_alerts_once(self):
        incident = self.monitor.begin_check(0)[0].incident_id
        self.monitor.prompt_finished(3, incident)
        self.assertEqual(self.monitor.deadline, 48)
        self.assertEqual(self.monitor.tick(7.99), [])
        self.assertEqual(self.monitor.tick(8)[0].kind, "repeat")
        self.assertEqual(self.monitor.tick(9), [])  # Do not queue prompts during playback.
        self.monitor.prompt_finished(19, incident)
        self.assertEqual(self.monitor.deadline, 48)  # Repeats never extend the deadline.
        self.assertEqual(self.monitor.tick(24)[0].kind, "repeat")
        self.monitor.prompt_finished(35, incident)
        self.assertEqual(self.monitor.tick(45), [])  # Final seconds stay open for replies.
        self.monitor.observe(None, 47)
        self.assertEqual(self.monitor.tick(48)[0].kind, "alert")
        self.assertEqual(self.monitor.tick(100), [])

    def test_broken_speaker_cannot_postpone_alert_forever(self):
        self.monitor.begin_check(0)
        self.assertEqual(self.monitor.tick(59), [])
        self.assertEqual(self.monitor.tick(60)[0].kind, "alert")

    def test_answer_after_old_short_deadline_still_cancels(self):
        incident = self.monitor.begin_check(0)[0].incident_id
        self.monitor.prompt_finished(5, incident)
        self.assertEqual(self.monitor.respond("i am okay", .95, 30, incident)[0].kind, "cancel")
        self.assertEqual(self.monitor.tick(50), [])

    def test_okay_cancels_and_cooldown_rearms(self):
        incident = self.monitor.begin_check(0)[0].incident_id
        self.assertEqual(self.monitor.respond("I'm okay", .95, 5, incident)[0].kind, "cancel")
        self.assertEqual(self.monitor.state, State.COOLDOWN)
        self.assertEqual(self.monitor.tick(20), [])
        self.monitor.tick(36)
        self.assertEqual(self.monitor.state, State.MONITORING)

    def test_help_escalates_immediately(self):
        incident = self.monitor.begin_check(0)[0].incident_id
        self.assertEqual(self.monitor.respond("i am not okay", .95, 2, incident)[0].kind, "alert")

    def test_stale_speech_from_previous_incident_is_ignored(self):
        previous = self.monitor.begin_check(0)[0].incident_id
        self.monitor.reset()
        current = self.monitor.begin_check(1)[0].incident_id
        self.assertNotEqual(previous, current)
        self.assertEqual(self.monitor.respond("i am okay", 1, 2, previous), [])
        self.assertEqual(self.monitor.state, State.CHECKING)

    def test_late_okay_cannot_recall_an_alert(self):
        incident = self.monitor.begin_check(0)[0].incident_id
        self.assertEqual(self.monitor.respond("i am okay", 1, 61, incident)[0].kind, "alert")
        self.assertEqual(self.monitor.respond("i am okay", 1, 62, incident), [])


class SpeechRulesTests(unittest.TestCase):
    def test_update_acknowledgement_is_separate_from_reassurance(self):
        for text in ("okay", "OK.", "Thank you", "Stop voice", "I'm okay"):
            self.assertTrue(acknowledges_update(text, .9))
        for text in ("I'm not okay", "no", "help", "okay no", "the TV said okay", ""):
            self.assertFalse(acknowledges_update(text, .9))
        self.assertFalse(acknowledges_update("okay", .5))
        self.assertTrue(acknowledges_update("okay", .72))  # Real short-word Vosk fixture.
        self.assertEqual(classify_response("i am okay", .72), Response.UNKNOWN)
        self.assertFalse(acknowledges_update("okay", float("nan")))
        self.assertEqual(classify_response("okay", 1), Response.UNKNOWN)

    def test_explicit_safe_phrase_only(self):
        for phrase in ("I'm OK", "I am okay.", "im fine", "I’m okay"):
            self.assertEqual(classify_response(phrase, .9), Response.OK)
        for phrase in ("yes", "okay", "the tv says i am okay", "i am okay unk", "", "maybe"):
            self.assertEqual(classify_response(phrase, 1), Response.UNKNOWN)
        self.assertEqual(classify_response("i am okay", .5), Response.UNKNOWN)

    def test_negations_and_help_take_priority(self):
        for phrase in ("I'm not okay", "not okay", "no", "help me", "I can't get up", "i am okay no help"):
            self.assertEqual(classify_response(phrase, .9), Response.HELP)


class GeometryTests(unittest.TestCase):
    def landmarks(self):
        result = [SimpleNamespace(x=.5, y=.15, visibility=1, presence=1) for _ in range(33)]
        for index, x, y in ((11, .4, .3), (12, .6, .3), (23, .45, .55),
                            (24, .55, .55), (25, .45, .7), (26, .55, .7),
                            (27, .45, .9), (28, .55, .9)):
            result[index].x, result[index].y = x, y
        return result

    def test_full_body_calibration_and_missing_feet(self):
        points = self.landmarks()
        self.assertTrue(pose_geometry(points, 640, 480).upright)
        points[27].visibility = points[28].visibility = .1
        pose = pose_geometry(points, 640, 480)
        self.assertIsNone(pose.ankle_y)
        self.assertFalse(pose.upright)

    def test_invalid_coordinates_are_rejected(self):
        points = self.landmarks()
        points[23].x = points[24].x = float("nan")
        self.assertIsNone(pose_geometry(points, 640, 480))


if __name__ == "__main__":
    unittest.main()
