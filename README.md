# Granny Project — Mac demo

Local webcam → pose tracking → suspected fall → spoken response check → Telegram photos and optional Twilio demo calls.
The Raspberry Pi is not needed for this stage. Phone calls are OFF by default. Emergency calls are disabled.

## Run

The environment and local models have been installed in this workspace:

```sh
./run_granny.command
```

This starts with **Telegram and phone calls OFF**. The display and voice check work, and alerts are recorded locally.
Allow camera and microphone access if macOS asks. In the default mode, input audio is processed locally and is not saved.

1. Place the Mac on a stable surface with one person's head, feet, and surrounding floor visible.
2. Stand upright and still in the monitored area for two seconds, then press **C** to calibrate.
3. Press **F** to rehearse the voice check without acting out a fall.
4. Say **“help”** during the check, including while the prompt plays. Say **“I am okay”** or **“no”** during a quiet pause.
5. **O** and **H** provide clearly identified keyboard responses when needed.
6. **R** resets/re-arms monitoring; **Q** or Escape quits.

When ready to send actual **TEST ONLY** Telegram messages to the saved recipients:

```sh
./run_granny.command --telegram
```

Use the existing `telegram_setup.py` commands to manage recipients. One recipient is enough for a rehearsal;
select three accounts for the full messaging demo. Do not paste tokens into chat or commit `.granny/`.

## Localhost browser dashboard

Quit the desktop version first so it releases the camera. In Terminal:

```sh
cd /Users/matthewchan/Grannyproject/GrannyProject
./run_granny_web.command
```

Open **http://localhost:8000** in Chrome or Safari. Keep Terminal open; **Ctrl+C** in Terminal stops
the server and monitoring. Closing the browser tab does not stop monitoring. No new packages are needed.

- **Calibrate (5s)**: click, then step back during the countdown and stand still with head, feet, and floor visible for its final two seconds. Calibration uses several stable frames and rejects incomplete or moving poses.
- **Simulate fall**: rehearse the spoken check without acting out a fall.
- **I’m okay / Help**: button responses during an active check; speaking still works.
- **Stop voice**: silence repeated updates after an alert without resetting or recalling the alert.
- **Reset**: reset/re-arm after a demo. Previously sent messages cannot be recalled.

The browser shows the camera with pose tracking, incident countdown, voice and alert status, and session
activity. The server uses the Mac's camera, microphone and speaker; the browser does not capture them.
The **Right now** panel summarizes **Voice check**, **Notify family**, and **Call family**. A tick means
the check finished, Telegram accepted the message, or a call connected/finished, respectively. Pending
work, failures, and partial delivery have different marks. A call tick does not mean someone confirmed
they are coming; that response appears separately. Expand **Details** for the full camera, tracking,
voice and delivery text. **Video connected** only means fresh camera frames are arriving. If the body
cannot be tracked, the main status says **Body tracking paused**. It requires one second of steady
tracking before showing Monitoring again; detection and alert timers themselves are not delayed.
Camera outages show **Camera interrupted**. Active response checks and alerts remain visible during an outage.
This server is bound to **127.0.0.1**, accessible only on this Mac. It is not a public hosting service.
Telegram and Twilio tokens stay on the server; the dashboard never receives them. Routine frames are not saved.

Telegram is **OFF** by default. To send actual **TEST ONLY** alerts to the existing saved contacts:

```sh
./run_granny_web.command --telegram
```

If port 8000 is already in use, run `./run_granny_web.command --port 8001` and open
http://localhost:8001 instead. `--no-audio` allows a quiet demo using the response buttons.
`--source` also works here for a video file or a future Pi camera stream.

Tests of the HTTP server use a temporary local port, simulated camera data, and isolated dry-run alerts.
They cover browser actions, stale incident responses, timeouts, disconnection, and blocked cross-site requests.

### Rehearse now using the Mac webcam

Start `./run_granny_web.command`, open http://localhost:8000, allow camera/microphone access, and keep
the Mac volume audible. Frame one person's full body and the floor, click **Calibrate (5s)**, and use the
countdown to step back and stand upright. Wait until the dashboard says **Monitoring**.
This launch sends no messages or calls. After each case, click **Reset** before starting the next check.

| Test | Expected result |
| --- | --- |
| Simulate fall; wait for the prompt to finish; say “I am okay” | Alert cancelled; no outgoing notifications |
| Simulate fall; say “help” during or after the prompt | Alert recorded locally through the fast local keyword path |
| Simulate fall; stay silent | Prompt repeats with listening pauses; alert 45 seconds after the first prompt finishes |
| Simulate fall; say “I am not okay” | Escalation, not cancellation |
| Stand, walk, sit normally, and briefly bend down | Monitoring should continue; note any false alarms |
| With monitoring armed, slowly lie down on a mat and stay visible near the floor | Clear horizontal floor posture starts a check after about 4 observed seconds; ambiguous floor posture needs about 10 seconds |

Use controlled movement; do not deliberately fall. **Simulate fall** verifies the response workflow;
the last two cases assess real camera behavior in your room. Recalibrate whenever the camera moves.
Try the voice cases from the participant's actual distance and with typical room noise. Check the
recognized text and voice status in the dashboard if a phrase is missed.

Once the dry run works and your test recipients are ready, stop the app with Ctrl+C and restart it with
`./run_granny_web.command --telegram --calls`. Simulate once and say “help”. This sends real TEST ONLY
photo messages and places real Twilio demo calls. Confirm the photo arrives on Telegram and answer the
phone to hear the demo message; accepted API requests alone do not establish delivery. Reassurance in
a separate check should send neither. Wait after the alert to verify it does not repeat until reset.
Emergency calls remain disabled. If you enable only `--telegram`, the rehearsal sends photos without calls.

### Physical demonstration with all alerts enabled

Run `./run_granny_web.command --elevenlabs --telegram --calls` and open http://localhost:8000.
Keep the Mac on a stable surface, with your whole body visible both standing and lying sideways on a mat.
Include the floor in the frame, keep the camera angle fixed, and leave the Mac awake with its volume audible.
Click **Calibrate (5s)**, step back to the demonstration spot, and stand still until **Monitoring** appears.

Lower yourself onto the mat in a controlled way and remain visible near the calibrated floor. Clear
horizontal posture needs about 4 observed seconds; an ambiguous low posture needs about 10 seconds.
The detector shows its observed duration under **Possible fall**, then starts **Checking response**.
Say **“Help me”** during the check to trigger the alerts, or stay silent until the
response countdown expires. “I am okay” cancels the alert. Telegram and Twilio requests are submitted
concurrently while the Mac speaks the family-alert update; phone ringing and photo arrival can differ.

After a completed incident, get up and click **Reset** to re-arm. Recalibrate after moving the Mac or changing
the screen angle. If detection does not start, check for **Body tracked** or **Torso tracked** and confirm
shoulders and hips remain visible while lying down. Feet are required for calibration, but an otherwise
clear floor pose can be tracked with hidden feet. Do not use **Simulate fall** for this physical-detection check; that
button starts the response workflow directly. Stop the app with **Ctrl+C** in its Terminal.

## ElevenLabs voice and reply recognition

The **voice backup** listens locally for **“help”** or **“help me”** while Granny is
waiting, monitoring or recovering from a camera outage. It works before calibration
and starts family alerts as soon as the command is recognized, without a fall or a
45-second wait. The dashboard shows **Voice backup ON** only after the microphone
opens. Its Help button also works without an active fall check.

Standby audio stays on the Mac and is not recorded or sent to ElevenLabs. During
an existing response check, the original local urgent-help path remains active.
After dispatch, repeated help does not send another alert; use Reset to rearm.
Granny suspends the standby listener during its own announcements and family-call
audio, and resumes it after cancellation. If the camera has no fresh image,
Telegram sends a text alert saying the image is unavailable; calls still start.
Both notification channels need internet access. `--no-audio` disables voice backup.

Spoken feedback now follows every response check, including with the default Mac voice:

- Reassurance: “Okay. I have cancelled the alert.”
- Dry run: “Demo alert triggered. No messages or calls are being sent.”
- Both channels enabled: “Your alert has started. I am calling your family and sending them a photo and message.”
- A failure to queue the alert: “I could not start the family alert. Please use another way to call for help.”

The speech reflects the enabled channels and describes an attempt to contact family, not confirmed delivery.
It never announces an actual 911 call. Announcements do not hold up provider delivery, and a speaker failure
does not suppress notifications. Reset stops playback. `--no-audio` disables local check-in speech and microphone access; omit `--listen-calls` as well for a fully silent session.

The Mac speaks more slowly: ElevenLabs uses speed **0.85**, and the fallback Mac voice uses 135 words per
minute. The check-in alternates spoken prompts with **5-second listening pauses**, up to a **45-second
response window after the first prompt**. The final seconds are left open for a reply. Say “I am okay” to
cancel, or “help”/“no” to alert. During a response check, **“help” also works while the prompt plays**.
A continuous local keyword decoder handles help without waiting for the network; final replies use
ElevenLabs when enabled. Say reassurance or “no” in a quiet pause. The short prompt is “Are you okay?”;
its microphone audio is excluded from cloud transcription, and it cannot cancel its own check.
The microphone and cloud connection stay open between check-in repeats to avoid reconnection delays.

After an alert starts, its spoken update repeats with 5-second listening pauses until you say **“okay”**,
**“thank you”**, or **“stop voice”**, press **Stop voice**, or reset the incident. This acknowledgement uses
local speech recognition, even when ElevenLabs is enabled. It does not send microphone audio to the cloud,
cancel a dispatched alert, or re-arm monitoring. Calls and messages are submitted **once per incident**;
repeated speech does not resend them. “I am not okay” does not silence the update. In the desktop window,
press **S** to stop the repeated voice. The cancellation confirmation plays once.

To use an ElevenLabs voice and **Scribe v2 Realtime** recognition, create an API key with Text to Speech,
Speech to Text, and Voices read access in your ElevenLabs account. Put it in the project's private `.env`
file on the **Mac** (replace the placeholder below with your key):

```dotenv
ELEVENLABS_API_KEY=your_actual_key_here
```

The `.env` file is Git-ignored. If it is missing on a fresh checkout, copy `.env.example` to `.env` and run
`chmod 600 .env`. Then select a voice and generate its cached phrases:

```sh
.venv/bin/python elevenlabs_setup.py setup
```

Choose a numbered voice. Setup reads the key from `.env`, saves only the voice choice in
`.granny/elevenlabs.json`, and generates nine short fixed phrases using API credit.
`--voice-id YOUR_VOICE_ID` can select a known voice directly.
If generation is interrupted, `.venv/bin/python elevenlabs_setup.py prepare` resumes missing phrases.

An exported `ELEVENLABS_API_KEY` takes priority over `.env`; `.env` takes priority over a previously saved
key. Blank entries are ignored. Quotes and comments are supported; shell commands and variable expansion
are not. Restart the app after changing the key. If neither environment source contains a key, setup asks
at a hidden prompt and stores it privately in `.granny/elevenlabs.json` instead. Do not paste keys into chat.
Twilio keeps using its existing `.granny/twilio.json` configuration; `.env` does not enable notifications.

Run a local playback check, then launch with both ElevenLabs features:

```sh
.venv/bin/python elevenlabs_setup.py test-voice
./run_granny_web.command --elevenlabs
```

Open http://localhost:8000 and test **Simulate fall**, “I am okay”, “I am not okay”, “help”, and silence.
Wait for the dashboard to say **Listening (ElevenLabs)** before answering. Verify recognition from the
actual participant distance and with room noise; the integration alone does not establish better accuracy.
If permission, quota or connection errors occur, the dashboard identifies the offline fallback. Repeat your
answer if requested. The existing alert deadline continues throughout failures and is never extended by the cloud.

ElevenLabs recognition sends **microphone audio only during response checks** to ElevenLabs. API charges
and your account's retention settings apply; no claim of zero provider retention is made. The app does not
save microphone recordings locally. The key stays on the Mac and is never served to the browser or Pi.
Only final, unedited transcripts can trigger actions. Exact reassurance and negation rules still apply;
partial results and AI-edited transcripts are ignored. Scribe does not provide a confidence percentage,
so the dashboard labels its result as a final transcript instead of displaying an invented score.

The generated voice clips play locally, including without internet; missing/broken playback falls back to
the Mac voice. For cached ElevenLabs speech with **local-only** recognition, use:

```sh
./run_granny_web.command --voice elevenlabs
```

For real demo family notifications as well, use `./run_granny_web.command --elevenlabs --telegram --calls`.
The ElevenLabs option itself does not enable Telegram or calls. Twilio calls retain their existing demo voice;
ElevenLabs changes the voice heard by the participant beside the Mac.

References: [ElevenLabs speech synthesis](https://elevenlabs.io/docs/api-reference/text-to-speech/convert),
[realtime transcription](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime),
[voice speed control](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/can-i-change-the-pace-of-the-voice).

## Listen to family calls on the Mac

Start the complete demo:

```sh
./run_granny_web.command --elevenlabs --telegram --calls --listen-calls
```

Open http://localhost:8000. The camera is at the top; **Family alerts** shows each recipient's progress.
A help reply starts notifications immediately. Otherwise the check-in repeats after each **5-second
listening pause**, with an alert **45 seconds after the first prompt finishes**. Repeats never extend
that deadline; a failed initial prompt is capped at 60 seconds from the check starting.

With `--listen-calls`, an automated phone conversation asks the contact whether they can come.
They can speak yes/no, but **pressing 1** is required to mark **confirmed they can come**; **2** means
unavailable. Verbal yes leads to a keypad confirmation request. Three unanswered/unclear attempts end
the call without a confirmation. This is a scripted confirmation conversation using Twilio's voice,
not a free-form ElevenLabs AI agent. A commitment is not proof of arrival.

On an upgraded Twilio account, the Mac plays both sides of the first connected call. The repeated local update stops when the call
stream starts; the Mac microphone remains off. **Mute call audio** mutes the Mac while the phone call
continues. Reset also mutes old calls. Multiple contacts can be called concurrently, but overlapping
conversations are not played together; another call already being skipped does not automatically
switch to the speaker. Each contact's confirmation still appears in Family alerts.

Twilio's current [trial rules](https://www.twilio.com/docs/usage/trials/try-out-voice#blocked-verbs)
block `<Stream>`. On a **Trial** account the app therefore enables the interactive confirmation call
without live playback, labels that limitation in Family alerts, and excludes the unsupported stream
from the call. A confirmed response updates the local spoken announcement. After upgrading Twilio,
restart with the same command to enable listening. If Twilio plays its trial greeting, follow its
keypad instruction first, then wait for Granny's separate confirmation question.

This option requires internet and an authenticated `ngrok` installation (`ngrok config add-authtoken`).
The app starts and stops its own tunnel, forwarding only the separate loopback endpoint on port 8765.
Twilio signatures, selected recipients, account/call IDs and short-lived session paths are validated.
The dashboard, camera and controls stay on localhost:8000. No call audio is saved locally; audio
passes through Twilio and ngrok. Calls disclose live playback to the participant. Calls ring for up to
20 seconds and last at most 120 seconds. If audio fails, check the per-contact status; notification
submission is not retried automatically. Omit `--listen-calls` for the original one-way demo message.

## Twilio phone calls

Twilio credentials are stored only in `.granny/twilio.json` (Git-ignored, owner-only file permissions).
If the account has already been connected through Chrome, start with this **read-only** check:

```sh
.venv/bin/python twilio_setup.py check
```

For manual setup, run `.venv/bin/python twilio_setup.py setup` and enter the Account SID, hidden Auth Token,
and Twilio sending number. Then select consenting recipients:

```sh
.venv/bin/python twilio_setup.py contacts
```

The contacts command lists existing **verified** phone numbers. Add and verify additional consenting numbers
in [Twilio Verified Caller IDs](https://console.twilio.com/us1/develop/phone-numbers/manage/verified) first.
Select at least two for the full family-calling demo; one works for initial testing. This app accepts Canadian/US
numbers only, caps the list at five contacts, and blocks emergency/short numbers.

Enable the two communication channels independently:

```sh
./run_granny_web.command --calls             # Real demo calls, no Telegram
./run_granny_web.command --telegram --calls  # Real demo calls and Telegram photo alerts
```

**These flags enable real notifications and Twilio credit/usage charges.** A simulated fall also uses the
enabled channels. Leave both flags out for a practice session with no outgoing messages or calls.
On help/silence, the app submits call and Telegram requests concurrently. Actual ringing times depend on
Twilio's call queue and carriers; exact simultaneous ringing is not guaranteed.

Each call identifies itself as a Granny Project demonstration, explains the help/no-response trigger,
and asks the contact to check on the demo participant. It rings for about 20 seconds before timing out,
has a 60-second call limit, and does not record audio. TwiML is sent directly with the API request,
so no public website, tunnel, or webhook is needed for this outbound demo.

The Family alerts panel tracks each Telegram send and call separately. Calls show dialing, ringing, connected, ended, busy, unanswered, or failed using Twilio status updates. Telegram acceptance does not prove the recipient has read it. A connected call does not prove a person will come. Provider call IDs are
saved in the local `deliveries` table in `.granny/alerts.sqlite3`. Check Twilio Voice logs or run:

```sh
.venv/bin/python twilio_setup.py call-status CA_YOUR_CALL_ID
```

To preview a single setup call, substitute a selected contact's number:

```sh
.venv/bin/python twilio_setup.py test-call +1_YOUR_SELECTED_NUMBER
```

The preview does not call anyone. Add `--place-call` only when that person is ready to receive the call.
Setup calls explicitly say that no fall or emergency has been detected.

Trial accounts require verified recipients and add trial restrictions/announcements. A successful test-credential
API request verifies request acceptance, but does not execute the speech or prove real phone delivery.
See [Twilio test credentials](https://www.twilio.com/docs/iam/test-credentials),
[Voice trial limits](https://www.twilio.com/docs/usage/trials/try-out-voice), and
[call queue behavior](https://www.twilio.com/docs/voice/api/call-resource).

## Behavior and limits

- The first version supports one person and a fixed camera angle. Recalibrate if the camera moves.
- A rapid drop followed by about 3 seconds near the calibrated floor starts a voice check.
- A clear horizontal posture near the floor starts a check after about 4 observed seconds, even without
  a rapid drop. Ambiguous low posture needs about 10 seconds. Brief landmark jitter is tolerated;
  missing frames do not count as evidence. Feet may be hidden after calibration if the torso stays visible.
- The voice check repeats with 5-second listening pauses; unclear speech does not cancel it. The response
  deadline is 45 seconds after the first prompt, capped at 60 seconds from the start if the speaker fails.
- Spoken updates after dispatch repeat until acknowledged or stopped; notifications are never repeated by that speech loop.
- An explicit reassuring phrase cancels the incident and starts a 30-second cooldown.
- “No,” negated reassurance, or a request for help escalates. So does expiration of the response deadline.
- Camera loss alone does not indicate a fall. Tracking loss after sustained fall evidence starts a voice check;
  an active check continues if camera frames stop arriving.
- An alert stays latched until **R** is pressed. Previously sent messages cannot be recalled.
- Alerts have persistent IDs and are not automatically retried after an uncertain network failure.
- A failed recipient does not block other recipients. “Accepted by Telegram” is not proof of human receipt.
- Alert snapshots and the incident ledger are stored under `.granny/`; routine video is not recorded.

These are configurable demo heuristics, not a validated medical fall detector. Sitting, bending and kneeling
are covered by synthetic geometry tests, but performance in your room still needs rehearsal. Low light,
occlusion, floor exercises, other camera angles, and multiple people can cause missed or false detections.
Use recordings or controlled movement onto a mat for calibration; do not deliberately fall to test it.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python verify_runtime.py
.venv/bin/python verify_stream_runtime.py
.venv/bin/python verify_voice_response.py
node --test tests/test_steps.cjs tests/test_readiness.cjs
```

The unit/integration suite uses synthetic poses and mocked notifications, covering timeout, reassurance,
negation, sensor failure, selected recipients, per-recipient failure, and persistent duplicate suppression.
The runtime check runs the actual pose model on a public Google sample and actual Vosk recognition on
macOS-generated speech and silence. It does not record the camera/microphone or send Telegram messages.
`verify_voice_response.py` feeds synthesized PCM through the real local microphone worker, including
help during playback, a word spanning the end of playback, quiet replies, and prompt-echo rejection.
It measures recognition latency on those clips, not recognition accuracy in the user's room.

`verify_stream_runtime.py` runs the actual application against a local MJPEG camera fixture and uses HTTP
dashboard controls to test calibration, reassurance, help, and silence after a stream disconnect. It uses
mock notification providers and temporary incident storage, so no real contacts are called or messaged.
It requires the sample prepared by `verify_runtime.py`. Test reports are saved in `.granny/test-media/`.

The camera regression tests also cover an open connection that stops sending frames and a stream that
never sends its first frame. Expected OpenCV timeout/disconnection warnings appear during these tests.
ESP32 tests use a real local HTTP server with chunked MJPEG, matching CameraWebServer's framing,
including stream checks, disconnects, stalls, and check-only mode without notifications.

### Verification on 2026-10-03

- Latest reliability revision: **134 automated tests passed**, including multi-frame calibration, hidden feet,
  brief landmark jitter, four-second clear floor posture, seated-posture rejection, and local urgent speech
  while the cloud connection is delayed. Existing 5-second pauses, the 45-second deadline, parallel alerts,
  Twilio callbacks, confirmation, duplicate suppression, and Trial fallback checks also passed.
- Seven real Vosk PCM replay cases passed: help during playback, help spanning playback completion,
  help in a quiet pause, negated reassurance, safe reassurance, prompt echo, and unrelated speech.
  Help was recognized within approximately 0.25 seconds after the synthetic phrase ended; these are
  fixture timings, not a guarantee for human speech or noisy rooms.
- A real Mac speaker/microphone check recognized synthetic “Help me” using the local keyword path
  0.91 seconds after playback started. This check used offline recognition and sent no notifications.
  The Mac was initially muted; its original volume and mute settings were restored after testing.
- Live ElevenLabs recognized synthetic help, reassurance, and negated reassurance on one reused
  connection, approximately 0.8–0.9 seconds after each phrase ended. No live microphone audio was
  sent during these cloud checks.
- The Heavy model and complete HTTP camera workflow passed calibration, cancellation, mock calls/photos,
  and escalation after 45 seconds despite stream loss. Latest test reports are in `.granny/test-media/`.
- Chrome UI checks passed for banner removal, cancellation, Help, per-recipient progress, and muting without clearing the alert. Screenshots were visually inspected. Desktop layout has no horizontal overflow.
- A signed synthetic WebSocket stream traveled through the public ngrok endpoint and played both audio tracks on the real Mac speaker. The endpoint rejected dashboard routes. Shutdown was verified.
- A real call connected and ended after 14 seconds without confirmation. No live call audio was received. Current Twilio Trial rules block streaming, so that account now uses confirmation-only mode; actual family confirmation still needs a completed phone test.
- `.env` tests passed for key loading, rotation, existing setup compatibility, and private storage.
- ElevenLabs request handling, real local WebSocket transport, offline fallback, and mode-aware spoken updates passed mocked-provider tests.
- All nine current ElevenLabs clips are cached and validated at speed 0.85, including the shorter prompt.
- A live Mac speaker/microphone check played the slower check-in twice, recognized “Help me” through ElevenLabs, repeated the family update twice, then recognized a local “okay” and stopped speaking. The alert stayed latched; exactly one mock call and one mock photo were submitted. No real contacts were notified by that check.
- Actual pose model: blank frame rejected and a known person tracked across 10 frames.
- Actual speech model: reassurance, negation, help, inability to get up, unrelated speech, and silence passed.
- Mac hardware: webcam frames received; spoken prompt completed; microphone data received; workers closed.
- Complete network-camera app flow passed with mocked parallel Telegram messages and Twilio calls.
- Read-only provider checks passed for the Telegram bot/chat and Twilio account/voice number/recipient.
- A separate Twilio test-credential call request was accepted without placing a real call.
- Live ElevenLabs recognition correctly transcribed synthetic “I am okay,” “I am not okay,” and “Help me” clips and produced the expected response decisions.
- An authorized integrated live test used the Mac camera and microphone: a recorded “Help me” reply was heard through ElevenLabs, the family-alert announcement finished, and the same incident sent one Telegram photo and requested one real Twilio call.
- Telegram accepted the photo; Twilio reported the call to the selected recipient as completed with a 13-second duration. This provider status does not establish that the intended person heard the entire message.
- The user subsequently confirmed receiving the call and Telegram message and hearing the spoken voice.

There is currently one Telegram contact and one verified phone recipient configured. Add two Telegram
contacts and one verified phone recipient to reach the original three-message/two-call demo targets.
Actual Pi hardware, Wi-Fi latency, and in-room fall accuracy remain to be checked. The integrated live test
used a manually simulated incident, so it does not validate physical fall detection. Rehearse reply recognition
with the actual participant at the intended distance and noise level; confirm photo receipt and call audibility
on the recipient's phone. The private live test reports are in `.granny/test-media/`.

To run the full application for a bounded dry run using the video fixture generated by `verify_runtime.py`:

```sh
.venv/bin/python granny_app.py --source .granny/test-media/sample.mp4 \
  --seconds 18 --headless --no-audio --simulate-check
```

## Fresh installation

Python 3.12 on Apple Silicon was used here. Use the pinned dependencies to avoid the native macOS
MediaPipe crash present in newer releases ([upstream issue](https://github.com/google-ai-edge/mediapipe/issues/6356)).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python setup_models.py
```

The default models are [MediaPipe Pose Landmarker Heavy](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker)
and [Vosk small English](https://alphacephei.com/vosk/models). They run offline once downloaded.
Heavy prioritizes landmark detail and measured about 8 pose frames/second on this Mac in the sample-image
benchmark. This throughput measurement does not establish fall-detection accuracy. For slower hardware,
install `setup_models.py --pose-model full` and launch with `--pose-model full` (about 22 frames/second in
the same benchmark). A larger pose model still requires calibration and room testing.

## Raspberry Pi connection

The [Pi service installer and launcher instructions](pi/README.md) automate the
camera service and SSH tunnel after the Pi's SSH identity and login are verified.
Use `./run_granny_pi.command --check` to verify the provisioned camera, then
`./run_granny_pi.command --elevenlabs --telegram --calls --listen-calls` for the full demo.

The intended arrangement is **Pi 4 + camera → local network video stream → Mac detection/dashboard**.
The speaker and microphone currently run on the Mac. The `/usr/bin/say` voice adapter is macOS-specific;
do not copy the Mac virtual environment to the Pi or expect the complete application to run there unchanged.

`pi_camera_server.py` now supplies the Pi side: a 640×480, 15 fps MJPEG stream using Picamera2.
It keeps the latest frame in memory, serves no local files, and defaults to Pi loopback access through SSH.
Only this one file needs copying to the Pi. Actual camera capture still needs checking on your Pi hardware.
Three additional tests passed for this server using generated JPEGs: simultaneous OpenCV clients and
reconnection, stalled-frame handling, and preview/file-access isolation. These do not exercise Pi camera hardware.

### Hackathon setup

1. With the Pi shut down and unplugged, connect the camera ribbon to the Pi 4 connector labelled **CAMERA**.
   Then power it up. See the [official cable installation guide](https://www.raspberrypi.com/documentation/accessories/camera.html#install-a-raspberry-pi-camera).
2. Connect the Pi and Mac to the same network. If venue Wi-Fi prevents devices from connecting to each other,
   use a personal hotspot or a private router that allows local connections. The Mac also needs internet for alerts.
3. These steps assume Raspberry Pi OS is installed. For an empty microSD, use
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to install Raspberry Pi OS (64-bit), setting
   a username, password, Wi-Fi and enabling SSH. Writing the image erases the selected card.

In a **Terminal on the Pi** (or an existing SSH session), run:

```sh
whoami
hostname -I
rpicam-hello --list-cameras
sudo apt update
sudo apt install -y python3-picamera2 --no-install-recommends
sudo systemctl enable --now ssh
```

Note the username from `whoami` and the Pi's local IP address from `hostname -I`. Camera detection must
list a camera before continuing. If `rpicam-hello` is missing on a current OS, install `rpicam-apps` with apt.
If no cameras are listed, shut down/unplug before reseating the ribbon. Close any other camera previews.
These commands use the [current Raspberry Pi camera tools](https://www.raspberrypi.com/documentation/computers/camera_software.html)
and the [recommended apt installation of Picamera2](https://github.com/raspberrypi/picamera2#installation).

In **Mac Terminal 1**, replace `PI_USER` and `PI_IP` below with those two values:

```sh
cd /Users/matthewchan/Grannyproject/GrannyProject
scp pi_camera_server.py PI_USER@PI_IP:pi_camera_server.py
ssh -t -o ExitOnForwardFailure=yes -L 8001:127.0.0.1:8001 PI_USER@PI_IP 'python3 ~/pi_camera_server.py'
```

Enter your Pi password when prompted (typing is hidden). On first connection, SSH asks you to confirm
the Pi's host key; verify you are connecting to your Pi. Keep this Terminal open. When it says
**Pi camera ready**, open http://localhost:8001 on the Mac to check the picture.

In **Mac Terminal 2**, start the app with the Pi feed:

```sh
cd /Users/matthewchan/Grannyproject/GrannyProject
./run_granny_web.command --source http://127.0.0.1:8001/stream.mjpg
```

Open **http://localhost:8000** for the Granny dashboard. Place the camera so a person's full body and
the floor are visible, stand upright and click **Calibrate**, then **Simulate fall**. Wait for the spoken
prompt to finish and say **“I am okay”**. Reset and simulate again, then say **“help”** or wait out the timer.
This first run sends no notifications. Keep the Mac speaker/microphone near the participant.

For actual demo notifications to the saved contacts, stop Terminal 2 with Ctrl+C and restart it with:

```sh
./run_granny_web.command --source http://127.0.0.1:8001/stream.mjpg --telegram --calls
```

This enables real Telegram photo messages and paid/trial Twilio calls, including for a simulated fall.
Emergency calls remain disabled. Keep Terminal 1 running throughout. Stop both with Ctrl+C after the demo.

If the Pi is not ready, use `./run_granny_web.command` with no `--source` for the working Mac webcam demo.
If a port is occupied, close the previous server first. The Pi feed uses port **8001**; the dashboard uses **8000**.

Opening a network camera and a stalled read are each limited to 3 seconds. Frames older than one second
are still rejected, allowing a brief hotspot pause to resume without reopening the socket. A disconnected/stalled
stream is marked unavailable; an already-active response countdown continues and keeps the incident photo.
The app retries the same stream automatically after a disconnect or timeout. Old video is cleared while
unavailable; fresh frames resume after reconnection. Recalibrate if the camera has physically moved.

## ESP32 camera setup

The setup is **ESP32 camera → local Wi-Fi → Mac detection, voice and family alerts**.
The board is now identified as **GOOUUU ESP32-S3-CAM V1.5 with an OV2640 camera**.
A read-only USB query confirmed **16 MB flash and 8 MB PSRAM**. The Mac integration and matching
[camera firmware and private Wi-Fi setup](esp32/README.md) are installed and verified. Use the board's **TTL USB-C
port**, currently `/dev/cu.usbserial-10`, for programming. The Pi is not required for this arrangement.

The firmware uses matching camera wiring and JPEG streaming based on Espressif's
[CameraWebServer example](https://github.com/espressif/arduino-esp32/tree/master/libraries/ESP32/examples/Camera/CameraWebServer).
For this hotspot setup, Granny can request camera images at `udp://CAMERA_IP:82`.
The original `http://CAMERA_IP:81/stream` and a diagnostic `/capture` JPEG endpoint remain available.
The firmware uses 640×480 JPEG and the Mac requests up to 5 frames per second; received frame rate depends on the network.
Keep the Mac and camera on a network where they can reach each other; venue Wi-Fi may isolate devices.
Only the video goes on the ESP32; Telegram, Twilio and ElevenLabs credentials remain on the Mac.

Once the camera firmware is running, replace the example IP with the camera's actual IP:

```sh
./run_granny_esp32.command --stream-url udp://192.168.1.50:82 --check --save
```

This checks several seconds of fresh frames and remembers a working URL privately. It sends no alerts.
Close the camera's browser stream before using Granny. Stop the Mac webcam demo, then start a rehearsal:

```sh
./run_granny_esp32.command
```

For the existing voice and real test-contact setup:

```sh
./run_granny_esp32.command --elevenlabs --telegram --calls --listen-calls
```

Open http://localhost:8000 and recalibrate for the new camera position. If the camera's IP changes,
repeat `--stream-url ... --check --save`. A failed check does not overwrite the saved URL or start the app.
If the stream fails during a response check, the existing alert timer continues. The app reconnects when
video returns at the same address. Keep `./run_granny_web.command` available as the Mac webcam fallback.

Verification on 2026-10-04 includes HTTP chunked JPEG, complete-image HTTP requests and
requested UDP frames. ESP32 tests reproduce HTTP chunked JPEG
framing, stale streams, disconnects and reconnection. A two-second pause resumes on the same connection;
the separate one-second freshness limit still prevents stale frames from being used for detection.
To run the full simulated-camera workflow:

```sh
.venv/bin/python verify_stream_runtime.py --esp32
.venv/bin/python verify_stream_runtime.py --udp
.venv/bin/python verify_backup_voice.py
```

The simulated workflow also verified the 45-second silence alert during a camera outage, retention of
the incident photo, and reconnection without duplicate notifications. No real notifications were sent.
Actual camera/upload/network checks and their limitations are recorded in [the camera guide](esp32/README.md).

The voice-backup replay verified real local recognition, two mocked calls and three
mocked text messages with no camera or calibration. “Help” and “Help me” triggered
about 0.1–0.6 seconds after the synthesized phrase ended; the tested unrelated,
negated-help and prompt phrases did not trigger. A separate real Mac microphone
test recognized “Help me” with all notification channels disabled. The Mac input
volume was raised from 29 to 65 for this test and demo. These are functional tests,
not measured recognition accuracy for other voices or a noisy room.
