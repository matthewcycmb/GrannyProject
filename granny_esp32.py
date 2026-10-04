"""Check an ESP32 JPEG camera, then launch the existing Mac Granny dashboard."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

from granny.camera import Camera

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / '.granny' / 'esp32.json'


def stream_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https', 'udp'} or not url.hostname or url.port == 0
                or url.username or url.password or url.fragment
                or (url.scheme != 'udp' and not url.path.strip('/'))
                or (url.scheme == 'udp' and (not url.port or url.path or url.query))
                or any(character.isspace() for character in value)):
            raise ValueError
        return value
    except ValueError:
        raise ValueError('Use the full camera stream URL, for example http://192.168.1.50:81/stream (no passwords).') from None


def saved_url(path=CONFIG):
    try:
        return stream_url(json.loads(Path(path).read_text())['stream_url'])
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Provide --stream-url http://CAMERA_IP:81/stream. Add --save after the camera is ready.') from None


def save_url(value, path=CONFIG):
    value = stream_url(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({'stream_url': value}, output)
            output.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_stream(value, sample_seconds=3, timeout=8):
    """Require a continuing stream of fresh decoded frames, not just one JPEG."""
    camera = Camera(stream_url(value))
    camera.start()
    start = time.monotonic()
    first_at = None
    first_sequence = 0
    try:
        while time.monotonic() - start < timeout:
            now = time.monotonic()
            frame, received, sequence, error = camera.latest()
            if not camera.thread.is_alive() or (first_at is not None and (error or now - received > 1)):
                break
            if frame is not None and not error and now - received < 1:
                if first_at is None:
                    first_at, first_sequence = now, sequence
                observed = now - first_at
                if observed >= sample_seconds and sequence - first_sequence >= 4:
                    return {'width': frame.shape[1], 'height': frame.shape[0],
                            'frames': sequence - first_sequence,
                            'fps': round((sequence - first_sequence) / observed, 1)}
            time.sleep(.03)
        raise RuntimeError('No stable camera stream. Check camera power, the stream URL, and that the Mac can reach the camera on the same network. Close other stream viewers and try again.')
    finally:
        camera.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stream-url', help='Granny udp://CAMERA_IP:82, /capture endpoint or MJPEG http://CAMERA_IP:81/stream')
    parser.add_argument('--check', action='store_true', help='Check video only; no detection, voice, calls or messages')
    parser.add_argument('--save', action='store_true', help='Remember the URL locally after a successful check')
    args, app_args = parser.parse_known_args(argv)
    if any(arg == '--source' or arg.startswith('--source=') for arg in app_args):
        parser.error('Use --stream-url to select the ESP32 camera.')
    try:
        value = stream_url(args.stream_url) if args.stream_url else saved_url()
        try:
            report = check_stream(value)
        except RuntimeError:
            if args.check or args.save:
                raise
            report = None
            print('Camera is not ready yet. Starting the dashboard with camera reconnection and the Help button available.', flush=True)
        if report:
            print(f"Camera verified: {report['width']} × {report['height']}, {report['fps']} received frames/sec.", flush=True)
            if report['fps'] < 4:
                print('The stream is below 4 FPS. Improve the Wi-Fi signal or lower the camera resolution before the demo.', flush=True)
        if args.save:
            save_url(value)
            print('Camera URL saved locally.', flush=True)
        if args.check:
            return 0
        # Replace this launcher so Ctrl+C and camera shutdown retain normal behavior.
        os.execv(sys.executable, [sys.executable, str(ROOT / 'granny_app.py'), '--web', '--source', value, *app_args])
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'ESP32 startup: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
