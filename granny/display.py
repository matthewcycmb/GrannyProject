"""Native OpenCV dashboard; all rendering is local."""
import math
import textwrap
import cv2
import numpy as np
from .core import State
from .vision import CONNECTIONS

BACKGROUND = (27, 25, 22)
PANEL = (42, 39, 34)
TEXT = (241, 244, 242)
MUTED = (162, 168, 164)
TEAL = (169, 214, 88)
AMBER = (80, 193, 249)
RED = (113, 116, 247)


def label(image, text, x, y, scale=.55, color=TEXT, thickness=1):
    text = text.encode("ascii", errors="replace").decode()
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def paragraph(image, text, x, y, width=33, color=MUTED):
    for line in textwrap.wrap(text, width=width):
        label(image, line, x, y, .48, color)
        y += 22
    return y


def render(frame, landmarks, controller, now, telegram, camera_status="", fps=0):
    canvas = np.full((780, 1160, 3), BACKGROUND, dtype=np.uint8)
    monitor = controller.monitor
    label(canvas, "Granny Project", 24, 45, 1.05, TEXT, 2)
    label(canvas, "FALL RESPONSE DEMO", 25, 73, .47, MUTED)
    color = (RED if monitor.state == State.ALERTED else AMBER
             if monitor.state in {State.POSSIBLE_FALL, State.CHECKING} else TEAL)
    cv2.rectangle(canvas, (875, 22), (1136, 66), PANEL, -1)
    label(canvas, "TELEGRAM " + ("ON - TEST ONLY" if telegram else "OFF - DRY RUN"),
          891, 50, .5, AMBER if telegram else TEAL)
    cv2.rectangle(canvas, (24, 105), (760, 657), (13, 14, 13), -1)
    if frame is not None:
        video = frame.copy()
        height, width = video.shape[:2]
        if not camera_status:
            for a, b in CONNECTIONS:
                if landmarks and min(landmarks[a].visibility, landmarks[b].visibility) >= .5:
                    start = (int(landmarks[a].x * width), int(landmarks[a].y * height))
                    end = (int(landmarks[b].x * width), int(landmarks[b].y * height))
                    cv2.line(video, start, end, TEAL, 2, cv2.LINE_AA)
                    cv2.circle(video, start, 3, TEXT, -1)
        if monitor.calibration:
            floor = int(monitor.calibration.floor_y * height)
            for x in range(0, width, 24):
                cv2.line(video, (x, floor), (min(x + 12, width), floor), AMBER, 2)
        ratio = min(736 / width, 552 / height)
        video = cv2.resize(video, (int(width * ratio), int(height * ratio)))
        vx = 24 + (736 - video.shape[1]) // 2
        vy = 105 + (552 - video.shape[0]) // 2
        canvas[vy:vy + video.shape[0], vx:vx + video.shape[1]] = video
    if camera_status:
        cv2.rectangle(canvas, (36, 115), (748, 188), PANEL, -1)
        paragraph(canvas, camera_status, 48, 142, width=72, color=AMBER)
    cv2.rectangle(canvas, (784, 105), (1136, 657), PANEL, -1)
    label(canvas, "STATUS", 804, 137, .45, MUTED)
    paragraph(canvas, monitor.state.value, 804, 174, width=26, color=color)
    if monitor.state == State.CHECKING and monitor.deadline:
        label(canvas, f"{max(0, math.ceil(monitor.deadline - now))}s", 804, 247, 1.8, AMBER, 2)
        label(canvas, "until escalation", 804, 278, .48, MUTED)
    else:
        instruction = ("Watching for a sustained floor posture." if monitor.calibration
                       else "Press C while standing, with your whole body visible.")
        paragraph(canvas, monitor.reason or instruction, 804, 219, width=31)
    label(canvas, "CAMERA", 804, 321, .43, MUTED)
    paragraph(canvas, monitor.sensor_status, 804, 350, width=32, color=TEXT)
    label(canvas, "VOICE CHECK", 804, 409, .43, MUTED)
    paragraph(canvas, controller.audio_status, 804, 438, width=32, color=TEXT)
    paragraph(canvas, controller.heard, 804, 500, width=32, color=TEAL)
    label(canvas, "NOTIFICATIONS", 804, 560, .43, MUTED)
    paragraph(canvas, textwrap.shorten(controller.alert_status, width=100, placeholder="..."),
              804, 589, width=34, color=TEXT)
    label(canvas, f"{fps:.1f} pose frames/sec", 28, 685, .45, MUTED)
    label(canvas, "C  Calibrate    F  Simulate    O  I'm OK    H  Help    S  Stop voice    R  Reset    Q  Quit",
          24, 722, .55, TEXT)
    label(canvas, "Demo prototype: one person, fixed camera, visible floor. No emergency calls.",
          24, 756, .40, MUTED)
    label(canvas, "PHONE CALLS " + ("ON - TEST ONLY" if controller.alerts.calls else "OFF"),
          850, 756, .45, AMBER if controller.alerts.calls else MUTED)
    return canvas
