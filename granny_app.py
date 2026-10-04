"""Mac webcam fall-response demo. Run with .venv/bin/python granny_app.py."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="0", help="Camera index, video file, MJPEG URL or Granny ESP32 UDP/JPEG URL")
    parser.add_argument("--pose-model", choices=("lite", "full", "heavy"), default="heavy",
                        help="Pose model: heavy prioritizes landmark detail; full is a faster alternative")
    parser.add_argument("--telegram", action="store_true", help="Send DEMO alerts to saved Telegram contacts")
    parser.add_argument("--calls", action="store_true", help="Place DEMO Twilio calls to selected recipients (uses credit)")
    parser.add_argument("--listen-calls", action="store_true", help="Ask family to confirm and play one call at a time on this Mac; starts an ngrok tunnel")
    parser.add_argument("--no-audio", action="store_true", help="Use keyboard responses instead of microphone/speaker")
    parser.add_argument("--voice", choices=("mac", "elevenlabs"), default="mac", help="Spoken voice; ElevenLabs clips must be prepared first")
    parser.add_argument("--recognition", choices=("local", "elevenlabs"), default="local", help="ElevenLabs sends response-window microphone audio to its API")
    parser.add_argument("--elevenlabs", action="store_true", help="Use both ElevenLabs voice and realtime recognition")
    parser.add_argument("--mic-device", type=int, help="Optional sounddevice input device index")
    parser.add_argument("--seconds", type=float, default=0, help="Stop after this duration; 0 runs until Q")
    parser.add_argument("--headless", action="store_true", help="Run without a window for integration checks")
    parser.add_argument("--simulate-check", action="store_true", help="Start a clearly labeled manual demo check")
    parser.add_argument("--save-preview", type=Path, help="Save the final dashboard image for inspection")
    parser.add_argument("--web", action="store_true", help="Open the dashboard on localhost instead of a desktop window")
    parser.add_argument("--port", type=int, default=8000, help="Local web dashboard port (default: 8000)")
    args = parser.parse_args()
    if args.elevenlabs:
        args.voice = args.recognition = "elevenlabs"
    if args.listen_calls and not args.calls:
        parser.error("--listen-calls requires --calls")
    if args.seconds < 0:
        parser.error("--seconds must be nonnegative")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.headless and not args.seconds and not args.web:
        parser.error("--headless requires a positive --seconds limit")
    try:
        import cv2
        from granny.alerts import AlertDispatcher
        from granny.audio import VoiceCheck
        from granny.camera import Camera
        from granny.calls import TwilioClient, load_config as load_call_config
        from granny.controller import Controller
        from granny.core import State
        from granny.display import render
        from granny.vision import PoseTracker
        from setup_models import POSE_MODEL, SPEECH_MODEL
        POSE_MODEL = POSE_MODEL.with_name(f"pose_landmarker_{args.pose_model}.task")
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}. Use .venv/bin/python and install requirements.txt.", file=sys.stderr)
        return 1
    if not POSE_MODEL.exists() or (not args.no_audio and not (SPEECH_MODEL / "am/final.mdl").exists()):
        print("Models missing. Run: .venv/bin/python setup_models.py", file=sys.stderr)
        return 1
    tracker = voice = alerts = camera = web = relay = tunnel = None
    dashboard = None
    web_frame = None
    desktop = not args.headless and not args.web
    preview = None
    try:
        call_config = None
        if args.calls:
            call_config = load_call_config(require_recipients=True)
            call_account = TwilioClient(call_config).check()
        if args.listen_calls:
            from granny.call_relay import CallRelay
            from granny.tunnel import CallTunnel
            relay = CallRelay(call_config, stream_audio=call_account["account_type"] != "Trial")
            relay.start()
            tunnel = CallTunnel()
            relay.set_public_url(tunnel.start(relay.port))
            if relay.stream_audio:
                print("Listen-only family calls ON. Signed call endpoint connected; dashboard stays local.", flush=True)
            else:
                print("Family confirmation ON. Twilio Trial blocks live call audio; upgrade the account for Mac playback.", flush=True)
        print("Loading local models...", flush=True)
        tracker = PoseTracker(POSE_MODEL)
        if not args.no_audio:
            from granny.speech import Speaker
            clips, cloud_key = None, None
            if args.voice == "elevenlabs" or args.recognition == "elevenlabs":
                from granny.elevenlabs import load_config as load_voice_config, load_clips
                clips = load_clips() if args.voice == "elevenlabs" else None
                cloud_key = load_voice_config()["api_key"] if args.recognition == "elevenlabs" else None
            voice = VoiceCheck(SPEECH_MODEL, args.mic_device, Speaker(clips), cloud_key)
            print(f"Spoken voice: {args.voice}. Recognition: {args.recognition}.", flush=True)
            if cloud_key:
                print("Response-window microphone audio is sent to ElevenLabs; API usage charges apply.", flush=True)
        alerts = AlertDispatcher(ROOT / ".granny", telegram=args.telegram, calls=args.calls,
                                 call_config=call_config, call_relay=relay)
        controller = Controller(alerts, voice)
        if args.web:
            from granny.web import Dashboard, DashboardServer, camera_jpeg
            dashboard = Dashboard()
            web = DashboardServer(dashboard, args.port)
            web.start()
            print(f"Open {web.url} in your browser. Stop the server with Ctrl+C.", flush=True)
        camera = Camera(args.source)
        camera.start()
        if desktop:
            cv2.namedWindow("Granny Project", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("Granny Project", 1160, 780)
        print("Ready. Telegram " + ("ON: demo messages to saved recipients." if args.telegram else "OFF: dry run."), flush=True)
        print("Twilio calls " + ("ON: demo calls to selected recipients." if args.calls else "OFF."), flush=True)
        start = time.monotonic()
        last_inference = start - 1
        last_sequence = -1
        last_pose = None
        landmarks = []
        jpeg = None
        processed = 0
        simulated = False
        previous_state = None
        while True:
            now = time.monotonic()
            frame, received, sequence, error = camera.latest()
            stale = frame is None or now - received > 1.0 or bool(error)
            camera_status = error or ("Camera frames are stale; monitoring unavailable" if stale else "")
            if frame is not None and not stale and sequence != last_sequence and now - last_inference >= .08:
                last_sequence, last_inference = sequence, now
                last_pose, landmarks, status = tracker.process(frame, now)
                okay, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                jpeg = encoded.tobytes() if okay else None
                controller.observe(last_pose, now, jpeg, status)
                if dashboard:
                    web_frame = camera_jpeg(frame, landmarks, controller.monitor.calibration)
                processed += 1
            elif stale:
                last_pose, landmarks = None, []
                jpeg = None
                web_frame = None
                controller.observe(None, now, unavailable=camera_status)
            if dashboard:
                dashboard.apply_commands(controller, last_pose, now, jpeg)
            if args.simulate_check and not simulated and now - start >= 1:
                controller.begin_check(now, jpeg)
                simulated = True
            controller.tick(now)
            if controller.monitor.state != previous_state:
                previous_state = controller.monitor.state
                print("State:", previous_state.value, flush=True)
            fps = processed / max(.1, now - start)
            if dashboard:
                dashboard.publish(controller, now, args.telegram, camera_status, fps, web_frame)
            if desktop or args.save_preview:
                preview = render(frame, landmarks, controller, now, args.telegram, camera_status, fps)
            if desktop:
                cv2.imshow("Granny Project", preview)
                key = cv2.waitKey(1) & 0xff
                if key in (27, ord("q")) or cv2.getWindowProperty("Granny Project", cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key == ord("c"):
                    try:
                        controller.monitor.calibrate_recent(now)
                        controller.alert_status = "Standing position calibrated"
                    except ValueError as exc:
                        controller.alert_status = str(exc)
                elif key == ord("f"):
                    controller.begin_check(now, jpeg)
                elif key == ord("o"):
                    controller.respond("i am okay", now)
                elif key in (ord("h"), ord("H"), ord("l"), ord("L")):
                    controller.request_help(now, source="Keyboard")
                elif key == ord("r"):
                    controller.reset()
                elif key == ord("s"):
                    controller.stop_voice()
            if args.seconds and now - start >= args.seconds:
                break
            time.sleep(.01)
        print(json.dumps({"pose_frames_processed": processed, "state": controller.monitor.state.value,
                          "camera": controller.monitor.sensor_status, "telegram_enabled": args.telegram,
                          "calls_enabled": args.calls,
                          "audio": controller.audio_status, "alerts": controller.alert_status}))
        if args.save_preview and preview is not None:
            args.save_preview.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(args.save_preview), preview):
                raise RuntimeError("Could not save dashboard preview")
        return 0 if processed else 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Could not run the demo: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if web:
            web.close()
        if voice:
            voice.stop()
        if camera:
            camera.close()
        if tracker:
            tracker.close()
        if alerts:
            alerts.close()
        if relay:
            relay.close()
        if tunnel:
            tunnel.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    sys.exit(main())
