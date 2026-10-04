"""Open the verified Pi SSH camera tunnel, then run the Mac dashboard."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".granny" / "pi.json"
CAMERA_URL = "http://127.0.0.1:8001/stream.mjpg"


def settings():
    try:
        config = json.loads(CONFIG.read_text())
        host = str(ipaddress.IPv4Address(config["host"]))
        user = config["user"]
        if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
            raise ValueError("Invalid Pi username")
        identity = ROOT / ".granny" / "pi_ed25519"
        if not identity.is_file():
            raise ValueError("Pi SSH key missing")
        return host, user, identity
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Pi setup is incomplete. Verify the SSH identity, install the camera service, and save the Pi connection first.") from None


def stop(process, interrupt=False):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT if interrupt else signal.SIGTERM)
        try:
            process.wait(timeout=10 if interrupt else 3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def check_camera(tunnel):
    from granny.camera import Camera
    # First allow SSH to bind its forwarding socket before OpenCV's bounded open.
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if tunnel.poll() is not None:
            raise RuntimeError("Could not open the Pi SSH connection. Check the cable, power and saved SSH identity.")
        try:
            with socket.create_connection(("127.0.0.1", 8001), timeout=.3):
                break
        except OSError:
            time.sleep(.1)
    camera = Camera(CAMERA_URL)
    camera.start()
    try:
        until = time.monotonic() + 10
        while time.monotonic() < until:
            frame, received, sequence, error = camera.latest()
            if frame is not None and sequence >= 5 and not error and time.monotonic() - received < 1:
                print(f"Pi camera verified: {frame.shape[1]} × {frame.shape[0]}, live frames received.", flush=True)
                return
            if not camera.thread.is_alive() or tunnel.poll() is not None:
                break
            time.sleep(.05)
        raise RuntimeError("Pi camera feed is unavailable. Check systemctl status granny-camera on the Pi.")
    finally:
        camera.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the Pi camera without running detection or alerts")
    args, app_args = parser.parse_known_args()
    tunnel = app = None
    try:
        host, user, identity = settings()
        with socket.socket() as available:
            try:
                available.bind(("127.0.0.1", 8001))
            except OSError:
                raise RuntimeError("Port 8001 is already in use. Close the previous Pi tunnel before starting another.") from None
        print(f"Connecting to the Pi camera at {host}…", flush=True)
        tunnel = subprocess.Popen([
            "ssh", "-N", "-T", "-i", str(identity), "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-L", "127.0.0.1:8001:127.0.0.1:8001", f"{user}@{host}",
        ])
        check_camera(tunnel)
        if args.check:
            return 0
        app = subprocess.Popen([sys.executable, str(ROOT / "granny_app.py"), "--web",
                                "--source", CAMERA_URL, *app_args], cwd=ROOT)
        # An active response timer must continue if the camera/SSH link later fails.
        # The Camera adapter reports loss and the user can restart after restoring it.
        return app.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Pi startup: {exc}", file=sys.stderr)
        return 1
    finally:
        stop(app, interrupt=True)
        stop(tunnel)


if __name__ == "__main__":
    raise SystemExit(main())
