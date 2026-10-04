# GOOUUU ESP32-S3 camera for Granny

The connected board was identified on 2026-10-04 as **GOOUUU ESP32-S3-CAM V1.5**
with an **OV2640** camera from its labels/photos. A read-only esptool query through
the **TTL USB-C port** confirmed ESP32-S3 revision 0.2, **16 MB flash** and
**8 MB embedded PSRAM**. The current Mac serial port is `/dev/cu.usbserial-10`.
The adjacent OTG port is not used for this upload setup.

This firmware provides 640×480 JPEG video through `udp://CAMERA_IP:82`, with
legacy MJPEG at `http://CAMERA_IP:81/stream`, individual JPEGs at `/capture`, and
camera details at `http://CAMERA_IP/status`. Granny detection, speech, Telegram
and Twilio continue on the Mac. No additional ESP board or Raspberry Pi is needed.
The Mac requests up to 5 UDP images per second; the legacy stream also has a 5 FPS
ceiling. Both use JPEG quality 24 to limit hotspot traffic. UDP sends one image
per request in 1,200-byte pieces. Request IDs and capture timestamps prevent
mixing frames or accepting old responses. Missing or corrupt images are discarded;
the one-second freshness limit still stops detection when video is unavailable.
TCP sends are immediate; `/status` includes capture/send timing and sent-frame counts.

## Configure and build

From the GrannyProject folder:

```sh
python3 esp32/setup_wifi.py
pio run --project-dir esp32
```

Enter the same 2.4 GHz Wi-Fi network used by the Mac. The WPA/WPA2 password is
hidden during entry. The generated `include/secrets.h` has owner-only permissions
and is ignored by Git. Build output in `.pio/` is also ignored and contains the
Wi-Fi credentials; keep these artifacts private. Use a trusted local network
where devices can reach each other: the camera stream has no authentication.

**SFU Guest's browser sign-up cannot be completed by this firmware.** Use a
password-protected 2.4 GHz phone hotspot or an organizer-provided device network,
and connect the Mac to that same network. For an iPhone 12 or later, enable
Personal Hotspot → Allow Others to Join and Maximize Compatibility; Apple's
[compatibility mode uses 2.4 GHz and WPA2](https://support.apple.com/en-ca/guide/security/secfd166f620/web).
Enter that hotspot's name and password in the setup command, not an SFU web login.

A build without `secrets.h` is supported for compilation checks. It stops at boot
with “Wi-Fi not configured” and does not initialize the camera or network.

## Upload and verify

Uploading replaces the program currently on this ESP32. Keep the cable in the
**TTL** USB-C port, below BOOT, on the right when viewing the camera side.

```sh
pio run --project-dir esp32 --target upload --upload-port /dev/cu.usbserial-10
pio device monitor --port /dev/cu.usbserial-10 --baud 115200
```

Press RST if startup output has already passed. Expect PSRAM size, OV2640
initialization, then `GRANNY_STREAM_URL=http://...:81/stream`. The firmware checks
whether the saved network is visible on 2.4 GHz and prints disconnect reason
codes without logging credentials or nearby network names. It retries every
30 seconds until connected, so a hotspot may be switched on after the board.
If it cannot find the saved network (reason 201), check the exact hotspot name,
2.4 GHz compatibility mode, discoverability and distance. Fix credentials and
rebuild/upload if necessary. Exit the monitor with Ctrl+C.

Use the actual IP printed by the board:

```sh
./run_granny_esp32.command --stream-url udp://CAMERA_IP:82 --check --save
```

This verifies several seconds of fresh decoded frames without starting alerts.
Only use one stream viewer at a time. Then use the saved camera for Granny:

```sh
./run_granny_esp32.command --elevenlabs --telegram --calls --listen-calls
```

Open <http://localhost:8000> and recalibrate for the camera position. After a
stream failure, Granny clears stale video and reconnects automatically. After an
IP change, restart with the new URL and repeat the check/save.

## Configuration references

- [Board pinout diagram](https://github.com/zhuhai-esp/ESP32-S3-Goouuu-Cam/blob/main/Documents/ESP32S3CAM-Pin.jpg)
- [Board-project camera pin mapping](https://github.com/zhuhai-esp/ESP32-S3-Goouuu-Cam/blob/main/Goouuu-CAM-WebServer/src/camera_pins.h)
- [Espressif CameraWebServer example](https://github.com/espressif/arduino-esp32/tree/2.0.17/libraries/ESP32/examples/Camera/CameraWebServer)

The board-project reference uses a different sensor; only its board wiring is
used here. Our firmware requires the photographed OV2640 and uses JPEG capture.
PlatformIO is pinned to Espressif32 7.0.1 / Arduino 2.0.17. The generic S3 board
definition is overridden for 16 MB flash and octal PSRAM (`qio_opi`).

Compilation and simulated MJPEG tests do not establish actual camera health.
Camera initialization, Wi-Fi connection, received frame rate and image quality
must be checked after uploading to this board.

Verified on 2026-10-04: firmware compiled successfully both without credentials
and in an isolated dummy-credential build that links the complete camera/Wi-Fi
code (774,357 bytes of program storage). All eight ESP32 stream/configuration
tests passed, including real OpenCV decoding of simulated MJPEG, stalled and
disconnected streams, no-alert check mode, and private credential handling.

The credentialed firmware has now been uploaded through TTL at **115200 baud**;
esptool verified the hash of each uploaded image. Faster 460800/921600 transfers
were unreliable on this connection, so the upload configuration now uses 115200.
Startup confirmed PSRAM and **OV2640 initialization at 640×480**. The camera joined
the configured 2.4 GHz hotspot, and its live feed passed Granny's normal startup
check at **5.3 received frames/second**. A subsequent **25-second run of the real
Granny application processed 136 frames** with the Heavy pose model, without a
stream timeout. Audio, Telegram and calls were disabled during that hardware test.
The report is `.granny/esp32-hardware-report.json`. These measurements describe this
connection, not guaranteed performance or physical fall-detection accuracy.

The original unpaced, larger-JPEG stream paused too long on this hotspot. Immediate
TCP sends, smaller JPEGs and the 8 FPS ceiling improved delivery. Longer runs still
showed intermittent pauses of about two seconds. Granny now allows three seconds
for a read to resume before reconnecting, while rejecting frames older than one second.

After that change, **145 Python tests and the full simulated ESP32 alert workflow
passed**. The simulation verified reassurance, help, the 45-second silence deadline
during an outage, retention of the incident photo and recovery without duplicate alerts.
A real USB reset test also recovered live video **6.45 seconds after reset began**,
without restarting Granny. During the following 12 seconds, 44 of 48 dashboard samples
had fresh video; four were unavailable. Its final average processing rate was 3.4 FPS.
The report is `.granny/esp32-recovery-hardware.json`. All hardware recovery checks had
voice, Telegram and calls disabled. Recovery works, but hotspot delivery is still
intermittent; this is not a guarantee of uninterrupted monitoring or detection accuracy.

The normal dashboard was then opened with the saved ElevenLabs, Telegram and
Twilio settings, remaining uncalibrated with no incident. A later 12-second sample
had fresh video in all 40 checks, and the live image/controls were inspected in Chrome.
This verifies startup and recovery, not a physical fall or delivery of new live alerts.

After reports of flickering, live diagnostics found both genuine stream interruptions
and loss of usable body landmarks. A separate 20-second capture-only check received
148 frames without a one-second gap; direct JPEG reception also showed a brief pause.
These comparisons did not isolate the video decoder as the cause, so its timing and
the one-second stale-frame guard remain unchanged.

The firmware ceiling was subsequently lowered from 8 to **5 FPS**, preserving resolution
and JPEG quality. It compiled/uploaded successfully with verified image hashes.
A **40-second real application run** at the new rate processed about **4.5 FPS**;
190 of 195 status samples had fresh video (97.4%). Five were temporarily stale, and
no decoder timeout was logged. This is one observed run, not proof that the hotspot
will stay reliable. The report is `.granny/test-media/flicker-check/fps5-live-report.json`.

The UI now distinguishes video availability from body tracking. Tracking failures
show a stable **Body tracking paused** message and require one second of steady
recovery before showing Monitoring again. Exact tracking reasons are in Details.
This presentation change does not postpone a response timer or count missing body
observations as evidence. **146 Python tests, 12 UI tests and the complete mocked
ESP32 alert workflow passed**. Hardware tests used no voice, calls or Telegram.
The previous 8 FPS firmware is saved privately at
`.granny/esp32-backups/firmware-before-fps5.bin` (application offset `0x10000`).

Opening the TTL serial monitor may reset this board; avoid using it during the demo.
For actual fall detection, mount the camera steadily, show one person's whole body
and the floor, and recalibrate after moving it.

### Longer hotspot checks and independent voice backup

Repeated interruptions continued after the earlier short checks. Both the camera
and Mac had strong Wi-Fi signal. Direct tests received valid ~12 KB JPEGs but some
took more than two seconds to transfer. New HTTP requests for each picture and
persistent HTTP image requests did not solve the problem; they are not the saved
demo source.

The firmware now also supports requested UDP images on port 82. The client discards
incomplete, corrupt, mixed or repeated captures, and recovers without restarting.
A 120-second real application run received fresh video in **88.4% of 584 status
samples**, averaging **3.4 processed FPS**, with a longest unavailable interval of
**9.49 seconds**. This improves on the two tested HTTP snapshot variants but **does
not establish reliable continuous fall monitoring** on this hotspot.

With the user's approval, AWDL was temporarily disabled for another 120-second run.
All sampled checks confirmed it remained disabled: fresh video was available in
90.8% of 575 samples, with a 4.58-second maximum gap. Disabling it did not eliminate
interruptions. AWDL was automatically restored and its UP state verified.
Reports are in `.granny/test-media/flicker-check/udp-live-report.json` and
`awdl-off-live-report.json`. No real calls or Telegram messages were sent by these tests.

The saved source is now UDP. Startup still validates check/save requests strictly,
but an ordinary launch can open the dashboard and voice backup while the camera
is unavailable. Say **help** or **help me**, or press **Help**, to trigger the family
alerts without waiting for fall detection. If no fresh photo exists, Telegram sends
a text alert. This backup listens on the **Mac microphone**, not the ESP32.

A private backup of the first 1 MB of the previous flash contents is in
`.granny/esp32-backups/20261004-085119-before-granny-first-1MB.bin`, with a SHA-256
checksum and range information in its adjacent JSON file. This covers all four
uploaded images. It is a backup of the replaced region, not all 16 MB of flash;
do not erase the rest of flash when using this backup to restore the old program.
