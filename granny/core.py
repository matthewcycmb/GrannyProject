"""Pure posture rules and timed incident state machine, independent of hardware."""
from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
import re
import uuid
from statistics import median


class State(str, Enum):
    UNCALIBRATED = "Calibrate standing"
    MONITORING = "Monitoring"
    POSSIBLE_FALL = "Possible fall"
    CHECKING = "Checking response"
    ALERTED = "Alert triggered"
    COOLDOWN = "Alert cancelled"


class Response(str, Enum):
    OK = "okay"
    HELP = "help"
    UNKNOWN = "unclear"


def normalize_speech(text):
    text = text.lower().replace("’", "'")
    text = re.sub(r"\bi'm\b|\bim\b", "i am", text)
    text = re.sub(r"\bcan't\b", "cannot", text)
    return " ".join(re.findall(r"[a-z]+", text))


def acknowledges_update(text, confidence=1.0):
    # Silencing an already-dispatched update is reversible and does not cancel
    # help. Short acknowledgements score below the stricter reassurance threshold.
    return (math.isfinite(confidence) and confidence >= .60
            and normalize_speech(text) in {"okay", "ok", "i am okay", "i am ok", "thank you",
                                           "okay thank you", "ok thank you", "stop voice", "stop speaking"})


def classify_response(text, confidence=1.0):
    text = normalize_speech(text)
    words = set(text.split())
    if not math.isfinite(confidence):
        return Response.UNKNOWN
    if words & {"no", "not", "help", "cannot", "hurt", "pain"}:
        return Response.HELP if confidence >= 0.55 else Response.UNKNOWN
    if confidence >= 0.80 and text in {
        "i am okay", "i am ok", "i am fine", "i am okay thank you", "i am ok thank you"
    }:
        return Response.OK
    return Response.UNKNOWN


@dataclass(frozen=True)
class Pose:
    hip_y: float
    shoulder_y: float
    ankle_y: float | None
    height: float
    torso_angle: float
    head_visible: bool = True

    @property
    def upright(self):
        return (self.head_visible and self.ankle_y is not None and self.torso_angle <= 30 and self.height >= 0.35
                and self.ankle_y - self.hip_y >= 0.18
                and self.hip_y - self.shoulder_y >= 0.10)


@dataclass(frozen=True)
class Calibration:
    hip_y: float
    floor_y: float
    height: float

    @classmethod
    def from_pose(cls, pose):
        if pose is None or not pose.upright:
            raise ValueError("Stand upright with your head and feet visible, then press C.")
        return cls(pose.hip_y, pose.ankle_y, pose.height)

    def near_floor(self, pose):
        low_hips = pose.hip_y >= self.floor_y - 0.22 * self.height
        low_shoulders = pose.shoulder_y >= self.floor_y - 0.32 * self.height
        # A small bounding box alone also describes someone sitting on the floor.
        # Require a tilted torso, or a strongly compressed torso as well as body.
        low_shape = (pose.torso_angle >= 55 or
                     (pose.height <= .40 * self.height and
                      abs(pose.hip_y - pose.shoulder_y) <= .12 * self.height))
        return low_hips and low_shoulders and low_shape


@dataclass(frozen=True)
class Timing:
    fallen_seconds: float = 3.0
    clear_down_seconds: float = 4.0
    already_down_seconds: float = 10.0
    response_seconds: float = 45.0
    prompt_allowance: float = 15.0
    repeat_seconds: float = 5.0
    cooldown_seconds: float = 30.0
    tracking_grace: float = 1.0


@dataclass(frozen=True)
class Action:
    kind: str
    incident_id: str
    reason: str = ""


class Monitor:
    def __init__(self, timing=None):
        self.timing = timing or Timing()
        self.state = State.UNCALIBRATED
        self.calibration = None
        self.incident_id = ""
        self.reason = ""
        self.sensor_status = "Waiting for camera"
        self.deadline = None
        self._history = deque()
        self._down_since = None
        self._rapid_drop = False
        self._last_visible = None
        self._last_observation = None
        self._check_started = None
        self._listening_since = None
        self._repeat_at = None
        self._cooldown_until = 0.0
        self._calibration_samples = deque()
        self._down_evidence = 0.0
        self._clear_down_evidence = 0.0
        self._previous_down = False
        self._not_down_since = None

    def calibrate(self, pose):
        if self.state in {State.CHECKING, State.ALERTED}:
            raise ValueError("Finish or reset the current incident before recalibrating.")
        self.calibration = Calibration.from_pose(pose)
        self.reset()

    def calibrate_recent(self, now):
        """Use stable recent standing observations, never a single camera frame."""
        rows = [(at, pose) for at, pose in self._calibration_samples if now - at <= 1.5]
        samples = [pose for _, pose in rows if pose is not None and pose.upright]
        message = "Stand still with your head and feet visible for two seconds, then calibrate again."
        if (len(samples) < 5 or len(samples) < .8 * len(rows) or not rows
                or now - rows[-1][0] > .35 or rows[-1][0] - rows[0][0] < .6
                or rows[-1][1] is None or not rows[-1][1].upright):
            raise ValueError(message)
        def stable(values, tolerance):
            ordered = sorted(values)
            trim = max(1, len(ordered) // 10)
            return ordered[-trim-1] - ordered[trim] <= tolerance
        if (not stable([p.ankle_y for p in samples], .04)
                or not stable([p.hip_y for p in samples], .05)
                or not stable([p.height for p in samples], .08)):
            raise ValueError(message)
        self.calibrate(Pose(*(median(getattr(p, name) for p in samples)
                              for name in ("hip_y", "shoulder_y", "ankle_y", "height", "torso_angle"))))

    def reset(self):
        self.state = State.MONITORING if self.calibration else State.UNCALIBRATED
        self.incident_id = ""
        self.reason = ""
        self.deadline = None
        self._history.clear()
        self._down_since = None
        self._rapid_drop = False
        self._last_visible = None
        self._last_observation = None
        self._listening_since = None
        self._repeat_at = None
        self._down_evidence = 0.0
        self._clear_down_evidence = 0.0
        self._previous_down = False
        self._not_down_since = None

    def begin_check(self, now, reason="Manual demo trigger"):
        if self.state in {State.CHECKING, State.ALERTED}:
            return []
        self.incident_id = uuid.uuid4().hex
        self.state = State.CHECKING
        self.reason = reason
        self._check_started = now
        self._listening_since = None
        self._repeat_at = None
        # This cap also handles a broken speaker or worker that never reports ready.
        self.deadline = now + self.timing.prompt_allowance + self.timing.response_seconds
        return [Action("prompt", self.incident_id, reason)]

    def request_help(self, now):
        """Explicit help bypasses posture, calibration and the response timer."""
        if self.state == State.ALERTED:
            return []
        if self.state != State.CHECKING:
            self.incident_id = uuid.uuid4().hex
            self._check_started = now
        return self._alert("Person requested help")

    def prompt_finished(self, now, incident_id):
        if self.state != State.CHECKING or incident_id != self.incident_id:
            return
        if self._listening_since is None:
            self._listening_since = now
            self.deadline = min(self.deadline, now + self.timing.response_seconds)
        self._repeat_at = now + self.timing.repeat_seconds

    def respond(self, text, confidence, now, incident_id):
        if self.state != State.CHECKING or incident_id != self.incident_id:
            return []
        if now >= self.deadline:
            return self._alert("No clear response before the deadline")
        response = classify_response(text, confidence)
        if response == Response.HELP:
            return self._alert("Person requested help")
        if response == Response.OK:
            self.state = State.COOLDOWN
            self.reason = "Person said I am okay"
            self._cooldown_until = now + self.timing.cooldown_seconds
            self._history.clear()
            self._down_since = None
            self.deadline = None
            return [Action("cancel", self.incident_id, self.reason)]
        return []

    def _alert(self, reason):
        self.state = State.ALERTED
        self.reason = reason
        self.deadline = None
        return [Action("alert", self.incident_id, reason)]

    def tick(self, now):
        if self.state == State.COOLDOWN and now >= self._cooldown_until:
            self.reset()
        if self.state != State.CHECKING:
            return []
        if now >= self.deadline:
            return self._alert("No clear response before the deadline")
        if self._repeat_at is not None and now >= self._repeat_at:
            self._repeat_at = None
            # Leave the final seconds available for an answer instead of starting
            # a prompt that would be interrupted by the alert deadline.
            if self.deadline - now > self.timing.prompt_allowance:
                return [Action("repeat", self.incident_id)]
        return []

    def observe(self, pose, now, unavailable="Body not fully visible"):
        self._calibration_samples.append((now, pose))
        while self._calibration_samples and now - self._calibration_samples[0][0] > 2:
            self._calibration_samples.popleft()
        self.sensor_status = "Body tracked" if pose else unavailable
        if self.state in {State.UNCALIBRATED, State.CHECKING, State.ALERTED, State.COOLDOWN}:
            return []
        if pose is None:
            if (self.state == State.POSSIBLE_FALL and self._last_visible is not None
                    and now - self._last_visible >= self.timing.tracking_grace
                    and (self._down_evidence >= 2 or
                         (self._rapid_drop and self._down_evidence >= .6))):
                return self.begin_check(now, "Tracking lost after a possible fall")
            self._history.clear()
            self._previous_down = False
            return []
        elapsed = 0 if self._last_observation is None else max(0, now - self._last_observation)
        if self._last_observation is not None and now - self._last_observation > self.timing.tracking_grace:
            self._history.clear()
            self._down_since = None
            self._rapid_drop = False
            self._down_evidence = 0
            self._clear_down_evidence = 0
            self._previous_down = False
        self._last_observation = now
        self._last_visible = now
        while self._history and now - self._history[0][0] > 1.25:
            self._history.popleft()
        if not self.calibration.near_floor(pose):
            self._previous_down = False
            if self._not_down_since is None:
                self._not_down_since = now
            if (self._down_since is not None and not pose.upright
                    and now - self._not_down_since < .35):
                return []
            self._down_since = None
            self._down_evidence = 0
            self._clear_down_evidence = 0
            self._rapid_drop = False
            self.state = State.MONITORING
            self.reason = ""
            self._history.append((now, pose))
            return []
        if self._down_since is None:
            self._down_since = now
            self._down_evidence = 0
            self._clear_down_evidence = 0
            self._rapid_drop = any(
                previous.upright and pose.hip_y - previous.hip_y > 0.23 * self.calibration.height
                for _, previous in self._history
            )
        elif self._previous_down or (self._not_down_since is not None and elapsed <= .35):
            self._down_evidence += min(elapsed, .35)
            if (pose.torso_angle >= 70 and
                    abs(pose.hip_y - pose.shoulder_y) <= .12 * self.calibration.height):
                self._clear_down_evidence += min(elapsed, .35)
        self._not_down_since = None
        self._previous_down = True
        self.state = State.POSSIBLE_FALL
        threshold = self.timing.fallen_seconds if self._rapid_drop else self.timing.already_down_seconds
        if self._clear_down_evidence + 1e-6 >= self.timing.clear_down_seconds:
            return self.begin_check(now, "Person lying near the floor")
        if self._down_evidence + 1e-6 >= threshold:
            return self.begin_check(now, "Possible fall" if self._rapid_drop else "Person remains near floor")
        clear = (pose.torso_angle >= 70 and
                 abs(pose.hip_y - pose.shoulder_y) <= .12 * self.calibration.height)
        progress, target = (self._clear_down_evidence, self.timing.clear_down_seconds) if clear and not self._rapid_drop else (self._down_evidence, threshold)
        self.reason = f"Near-floor posture: {progress:.1f} of {target:.0f} seconds observed"
        return []
