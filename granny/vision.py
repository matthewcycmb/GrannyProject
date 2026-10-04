"""MediaPipe adapter and confidence-aware body geometry."""
import math
import os
from pathlib import Path
from .core import Pose


CONNECTIONS = ((11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
               (11, 23), (12, 24), (23, 24), (23, 25), (25, 27),
               (24, 26), (26, 28), (27, 31), (28, 32))


def pose_geometry(landmarks, width, height):
    if len(landmarks) != 33 or width <= 0 or height <= 0:
        return None

    def visible(index):
        point = landmarks[index]
        return (math.isfinite(point.x) and math.isfinite(point.y)
                and 0 <= point.x <= 1 and 0 <= point.y <= 1
                and point.visibility >= 0.5 and point.presence >= 0.5)

    def center(indices):
        points = [landmarks[i] for i in indices if visible(i)]
        if not points:
            return None
        return sum(p.x for p in points) / len(points), sum(p.y for p in points) / len(points)

    shoulders, hips, ankles = center((11, 12)), center((23, 24)), center((27, 28))
    if shoulders is None or hips is None:
        return None
    vertical = abs(shoulders[1] - hips[1]) * height
    horizontal = abs(shoulders[0] - hips[0]) * width
    if math.hypot(horizontal, vertical) < 8:
        return None
    ys = [landmarks[i].y for i in (0, 11, 12, 23, 24, 25, 26, 27, 28) if visible(i)]
    return Pose(hips[1], shoulders[1], ankles[1] if ankles else None, max(ys) - min(ys),
                math.degrees(math.atan2(horizontal, vertical)), any(visible(i) for i in range(11)))


class PoseTracker:
    def __init__(self, model_path):
        cache = Path(model_path).parent.parent / "cache" / "matplotlib"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(cache))
        import mediapipe as mp
        self.mp = mp
        options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path),
                                              delegate=mp.tasks.BaseOptions.Delegate.CPU),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=2,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.detector = mp.tasks.vision.PoseLandmarker.create_from_options(options)
        self.last_timestamp = -1

    def process(self, frame, now):
        import cv2
        timestamp = max(self.last_timestamp + 1, int(now * 1000))
        self.last_timestamp = timestamp
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB,
                              data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        result = self.detector.detect_for_video(image, timestamp)
        if not result.pose_landmarks:
            return None, [], "No person detected"
        if len(result.pose_landmarks) > 1:
            return None, result.pose_landmarks[0], "Multiple people: use one person"
        landmarks = result.pose_landmarks[0]
        pose = pose_geometry(landmarks, frame.shape[1], frame.shape[0])
        return pose, landmarks, ("Body tracked" if pose and pose.ankle_y is not None else
                                 "Torso tracked; show feet for calibration" if pose else
                                 "Show your shoulders and hips clearly")

    def close(self):
        self.detector.close()
