#!/usr/bin/env bash
# Run on Raspberry Pi OS as the normal SSH user: bash pi/install_camera.sh
set -euo pipefail

prepare_only=0
if [[ "${1:-}" == --prepare-only ]]; then
  prepare_only=1
elif [[ $# -ne 0 ]]; then
  echo 'Usage: bash pi/install_camera.sh [--prepare-only]' >&2
  exit 2
fi

project_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
pi_user="$(id -un)"
if [[ "$pi_user" == root || ! "$pi_user" =~ ^[a-z_][a-z0-9_-]*\$?$ ]]; then
  echo 'Run this installer as your normal Pi login user, not root.' >&2
  exit 1
fi
if [[ ! -r /proc/device-tree/model ]] || ! grep -q 'Raspberry Pi' /proc/device-tree/model; then
  echo 'This installer must run on the Raspberry Pi, not the Mac.' >&2
  exit 1
fi
if [[ ! -f "$project_dir/pi_camera_server.py" ]]; then
  echo 'Copy pi_camera_server.py and the pi directory together before installing.' >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends python3-picamera2
if ! command -v rpicam-hello >/dev/null; then
  sudo apt-get install -y --no-install-recommends rpicam-apps
fi

python3 - "$prepare_only" <<'PY'
import sys
from picamera2 import Picamera2
cameras = Picamera2.global_camera_info()
if not cameras:
    if sys.argv[1] == '1':
        print('No camera detected yet. Installing the service without starting capture.')
    else:
        raise SystemExit('No camera detected. Shut down/unplug the Pi, check the CAMERA ribbon connector, then retry.')
for camera in cameras:
    print('Detected camera:', camera.get('Model', 'unknown'))
PY

sudo install -d -m 755 /opt/granny-camera
sudo install -m 644 "$project_dir/pi_camera_server.py" /opt/granny-camera/pi_camera_server.py
unit_file="$(mktemp)"
trap 'rm -f "$unit_file"' EXIT
cat > "$unit_file" <<EOF
[Unit]
Description=Granny Project Raspberry Pi camera
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$pi_user
WorkingDirectory=/opt/granny-camera
ExecStart=/usr/bin/python3 -u /opt/granny-camera/pi_camera_server.py --host 127.0.0.1 --port 8001
Restart=on-failure
RestartSec=3
TimeoutStopSec=10
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$unit_file" /etc/systemd/system/granny-camera.service
sudo systemctl daemon-reload
sudo systemctl enable granny-camera.service
if [[ "$prepare_only" == 1 ]]; then
  echo 'Camera software installed and enabled for next boot. Hardware verification is still pending.'
  exit 0
fi
sudo systemctl restart granny-camera.service

python3 - <<'PY'
import time
from urllib.request import urlopen
for attempt in range(20):
    try:
        with urlopen('http://127.0.0.1:8001/stream.mjpg', timeout=3) as response:
            chunk = response.read(8192)
        if b'\xff\xd8' in chunk and b'Content-Type: image/jpeg' in chunk:
            print('PASS: Pi camera service is producing JPEG frames.')
            break
    except OSError:
        pass
    time.sleep(.5)
else:
    raise SystemExit('Camera did not become ready. Check: sudo journalctl -u granny-camera -n 40 --no-pager')
PY
systemctl is-active granny-camera.service
echo 'Camera service installed and enabled at boot. Access it through the Mac SSH tunnel.'
