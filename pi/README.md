# Pi camera service

The Pi runs the camera service; the Mac runs the existing Granny detector, voice,
dashboard and notification integrations. The camera listens only on Pi loopback.
The Mac receives the feed through an SSH tunnel.

Once the connection has been provisioned, use the Mac launcher from the project:

```sh
./run_granny_pi.command --check
./run_granny_pi.command --elevenlabs --telegram --calls --listen-calls
```

The first command checks actual Pi frames and exits without contacting anyone.
The second starts the SSH tunnel and full Mac dashboard together. Stop it with
Ctrl+C to close both. With no notification flags the launcher runs a dry demo.
It reads the verified username/address from `.granny/pi.json` and uses the private
project SSH key `.granny/pi_ed25519`; neither file belongs in source control.
SSH host checking stays enabled. If the SD card is replaced or reinstalled, verify
the new host identity before updating the saved key.

After SSH access and the device identity have been verified, copy this directory
and `pi_camera_server.py` to `~/GrannyProject` on the Pi. Then run as the Pi user:

```sh
cd ~/GrannyProject
bash pi/install_camera.sh
```

The installer uses the Raspberry Pi OS camera packages, checks for a detected
camera, installs `/opt/granny-camera/pi_camera_server.py`, and enables the
`granny-camera` system service. It does not copy API keys or the Mac environment.

If the camera is not attached yet, `bash pi/install_camera.sh --prepare-only`
installs and enables the service without starting capture or claiming that the
hardware works. Shut down and unplug the Pi before attaching the ribbon. On the
next boot the service starts automatically; then use the Mac launcher's `--check`.

Inspect or stop it on the Pi:

```sh
systemctl status granny-camera --no-pager
sudo journalctl -u granny-camera -n 40 --no-pager
sudo systemctl stop granny-camera
sudo systemctl disable granny-camera   # also prevent the next automatic start
```

Start the Mac tunnel, substituting the verified Pi username/address:

```sh
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:8001:127.0.0.1:8001 PI_USER@PI_IP
```

Keep it open. In another Mac Terminal:

```sh
./run_granny_web.command --source http://127.0.0.1:8001/stream.mjpg \
  --elevenlabs --telegram --calls --listen-calls
```

Open http://localhost:8000 and calibrate with the full body visible. For a practice
session without contacting anyone, omit `--telegram --calls --listen-calls`.
Voice and microphone remain on the Mac. Raspberry Pi capture has to be verified
on the physical hardware; the generated-frame tests alone cannot establish that.
