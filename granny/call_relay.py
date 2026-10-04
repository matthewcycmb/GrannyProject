"""Signed Twilio callbacks and one-way speaker audio for bounded demo calls.

Only these routes are public. No dashboard, camera, microphone, or recordings.
The conversation asks for an explicit commitment; it cannot verify arrival.
"""
import asyncio
from dataclasses import dataclass, field
import queue
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from aiohttp import web, WSMsgType
from twilio.request_validator import RequestValidator

from .call_audio import CallAudioPlayer
from .calls import TwilioClient, TwilioError, phone_number, CALL_VOICE, alert_summary
from .delivery import CALL_DETAILS
from .family_response import family_response

TERMINAL = {"completed", "failed", "canceled", "busy", "no-answer"}


@dataclass
class AudioRequest:
    incident: str
    token: str
    done: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False
    expires: float = field(default_factory=lambda: time.monotonic() + 2.5)


@dataclass
class Session:
    token: str
    incident: str
    number: str
    progress: object
    created: float = field(default_factory=time.monotonic)
    sid: str = ""
    confirmation: str = "pending"
    round: int = 0
    replies: dict = field(default_factory=dict)
    streamed: bool = False
    muted: bool = False
    player: object = None
    status_sequence: int = -1
    attempt: int = 1
    terminal_status: str = ""

    def update(self, **fields):
        self.progress.update(self.incident, "calls", self.number, attempt=self.attempt, **fields)


class CallRelay:
    def __init__(self, config, public_url=None, player_factory=CallAudioPlayer, client=None, stream_audio=True):
        self.client = client or TwilioClient(config)
        self.config = self.client.config
        self.validator = RequestValidator(self.config["auth_token"])
        self.public_url = ""
        if public_url:
            self.set_public_url(public_url)
        self.player_factory = player_factory
        self.stream_audio = stream_audio
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.sessions = {}
        self.active = None
        self.thread = self.loop = self.runner = None
        self.started = threading.Event()
        self.start_error = False
        self.port = None
        self.sockets = set()

    def set_public_url(self, url):
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment
                or parts.port not in {None, 443}):
            raise ValueError("Call relay needs a public HTTPS origin on port 443.")
        self.public_url = url.rstrip("/")

    def app(self):
        app = web.Application(client_max_size=16_384)
        app.router.add_post("/call/{token}/reply/{round}", self.reply)
        app.router.add_post("/call/{token}/status", self.status)
        app.router.add_get("/call/{token}/audio", self.audio)
        return app

    def start(self, port=8765):
        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            async def serve():
                self.runner = web.AppRunner(self.app(), access_log=None)
                await self.runner.setup()
                server = await self.loop.create_server(self.runner.server, "127.0.0.1", port)
                self.server = server
                self.port = server.sockets[0].getsockname()[1]
            try:
                self.loop.run_until_complete(serve())
            except Exception:
                self.start_error = True
            finally:
                self.started.set()
            if not self.start_error:
                self.loop.run_forever()
            if self.runner:
                self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()
        self.thread = threading.Thread(target=run, name="granny-call-relay", daemon=True)
        self.thread.start()
        if not self.started.wait(5) or self.start_error:
            raise RuntimeError("Could not start the separate call endpoint. Check port 8765.")

    def close(self):
        self.mute()
        if self.loop and self.loop.is_running():
            async def stop():
                for socket in list(self.sockets):
                    await socket.close(code=1001)
                self.server.close()
                await self.server.wait_closed()
            try:
                asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=5)
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=6)

    def mute(self, incident=None):
        with self.lock:
            for session in self.sessions.values():
                if incident is None or session.incident == incident:
                    session.muted = True
                    if session.player:
                        session.player.close()
                        session.update(audio="muted")

    def create_call(self, number, incident, reason, progress, attempt=1):
        number = phone_number(number)
        if number not in {p["number"] for p in self.config["recipients"]}:
            raise TwilioError("Call blocked: the number is not a selected demo recipient.")
        if not self.public_url:
            raise TwilioError("Call audio endpoint is not ready.")
        session = Session(secrets.token_urlsafe(32), incident, number, progress, attempt=attempt)
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items()
                             if time.monotonic() - value.created < 300}
            if len(self.sessions) >= 64:
                raise TwilioError("Too many recent calls. Wait before starting a new demo.")
            self.sessions[session.token] = session
        session.update(audio="waiting" if self.stream_audio else "upgrade-required")
        result = self.client.api("Calls", {
            "From": self.config["from_number"], "To": number,
            "Twiml": self.initial_xml(session, reason), "Timeout": 20, "TimeLimit": 120,
            "StatusCallback": self.url(session, "status"), "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        })
        with self.lock:
            self.bind(session, self.config["account_sid"], result.get("sid", ""))
            self.apply_status(session, result.get("status"))
        return result

    def apply_status(self, session, status):
        # Caller holds self.lock. A terminal call can never ring again.
        if status not in CALL_DETAILS or session.terminal_status:
            return
        session.update(state=status, detail=CALL_DETAILS[status])
        if status in TERMINAL:
            session.terminal_status = status
            if session.confirmation == "pending":
                session.confirmation = "no-response"
                session.update(confirmation="no-response")

    def outcome(self, sid, status=None):
        """Merge a read-only status poll with signed speech/status callbacks."""
        with self.lock:
            session = next((s for s in self.sessions.values() if s.sid == sid), None)
            if session is None:
                return None
            self.apply_status(session, status)
            return session.terminal_status, session.confirmation

    def url(self, session, path):
        return f"{self.public_url}/call/{session.token}/{path}"

    @staticmethod
    def say(parent, text):
        ET.SubElement(parent, "Say", CALL_VOICE).text = text

    def gather(self, root, session, round_number, text):
        gather = ET.SubElement(root, "Gather", input="speech", timeout="8",
                               speechModel="phone_call", speechTimeout="2", actionOnEmptyResult="true", method="POST",
                               action=self.url(session, f"reply/{round_number}"), language="en-US")
        self.say(gather, text)
        ET.SubElement(root, "Hangup")  # A failed callback never loops into another call.

    def initial_xml(self, session, reason):
        root = ET.Element("Response")
        if self.stream_audio:
            ET.SubElement(ET.SubElement(root, "Start"), "Stream", track="both_tracks",
                          url=self.url(session, "audio").replace("https://", "wss://", 1))
        disclosure = " This call may be played live beside your loved one." if self.stream_audio else ""
        self.say(root, alert_summary(reason) + " Emergency services have not been called." + disclosure)
        self.gather(root, session, 0, "Can you go and check on them now? "
                    "You can say, yes, I'm coming, or, I can't come.")
        return ET.tostring(root, encoding="unicode")

    def next_xml(self, session, round_number, fields):
        # A retried webhook gets exactly the same response, without changing state.
        if round_number in session.replies:
            return session.replies[round_number]
        if round_number != session.round or session.confirmation not in {"pending", "no-response"}:
            raise web.HTTPConflict()
        response = family_response(fields.get("SpeechResult", ""), fields.get("Confidence"))
        root = ET.Element("Response")
        if response == "unavailable":
            session.confirmation = "unavailable"
            self.say(root, "Thanks for letting me know you can't come. "
                           "Please ask another family member to check on them.")
        elif response == "coming":
            session.confirmation = "coming"
            self.say(root, "Thank you. I'll let them know you're coming. "
                           "Please get to them as soon as you can.")
        elif round_number >= 2:
            session.confirmation = "no-response"
            self.say(root, "I haven't received a confirmation. Please check on your loved one as soon as you can.")
        else:
            session.round += 1
            prompt = ("I didn't catch a clear answer. Are you able to come and check on them? "
                      "Please say, I'm coming, or, I can't come.")
            self.gather(root, session, session.round, prompt)
        if session.confirmation != "pending":
            ET.SubElement(root, "Hangup")
            session.update(confirmation=session.confirmation)
        xml = ET.tostring(root, encoding="unicode")
        session.replies[round_number] = xml
        return xml

    def bind(self, session, account, sid):
        if (account != self.config["account_sid"] or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid)
                or session.sid and session.sid != sid):
            raise web.HTTPForbidden()
        session.sid = sid

    async def authenticated(self, request, socket=False):
        fields = {} if socket else await request.post()
        # Reject duplicates, query strings, and never trust the request's Host header.
        if request.query_string or len(fields) != len(set(fields.keys())):
            raise web.HTTPForbidden()
        signature = request.headers.get("X-Twilio-Signature", "")
        url = self.public_url + request.path
        urls = [url, url.replace("https://", "wss://", 1)] if socket else [url]
        if not self.public_url or not any(self.validator.validate(u, fields, signature) for u in urls):
            raise web.HTTPForbidden()
        with self.lock:
            session = self.sessions.get(request.match_info["token"])
            if session is None or time.monotonic() - session.created >= 300:
                raise web.HTTPNotFound()
            if not socket:
                if fields.get("To") != session.number:
                    raise web.HTTPForbidden()
                self.bind(session, fields.get("AccountSid", ""), fields.get("CallSid", ""))
        return session, fields

    async def reply(self, request):
        session, fields = await self.authenticated(request)
        try:
            round_number = int(request.match_info["round"])
        except ValueError:
            raise web.HTTPBadRequest() from None
        with self.lock:
            xml = self.next_xml(session, round_number, fields)
        return web.Response(text=xml, content_type="application/xml")

    async def status(self, request):
        session, fields = await self.authenticated(request)
        status = fields.get("CallStatus")
        try:
            sequence = int(fields.get("SequenceNumber", "-1"))
        except ValueError:
            raise web.HTTPBadRequest() from None
        with self.lock:
            if status in CALL_DETAILS and sequence > session.status_sequence:
                session.status_sequence = sequence
                self.apply_status(session, status)
        return web.Response(status=204)

    async def audio(self, request):
        if not self.stream_audio:
            raise web.HTTPNotFound()
        session, _ = await self.authenticated(request, socket=True)
        with self.lock:
            if session.streamed:
                raise web.HTTPConflict()
            session.streamed = True
        socket = web.WebSocketResponse(max_msg_size=16_384, receive_timeout=15, timeout=1)
        await socket.prepare(request)
        self.sockets.add(socket)
        player = None
        stream_sid = None
        played = False
        failed = False
        try:
            async with asyncio.timeout(135):
                async for message in socket:
                    if message.type != WSMsgType.TEXT:
                        break
                    data = message.json()
                    if not isinstance(data, dict):
                        raise ValueError("Malformed audio event")
                    if data.get("event") == "start":
                        if stream_sid:
                            raise ValueError("Duplicate audio start")
                        start = data["start"]
                        with self.lock:
                            self.bind(session, start.get("accountSid", ""), start.get("callSid", ""))
                        if start.get("mediaFormat") != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}:
                            raise ValueError("Unsupported audio format")
                        stream_sid = start["streamSid"]
                        if not re.fullmatch(r"MZ[0-9a-fA-F]{32}", stream_sid):
                            raise ValueError("Invalid stream")
                        with self.lock:
                            available = self.active is None and not session.muted
                            if available:
                                self.active = session.token
                        if available:
                            gate = AudioRequest(session.incident, session.token)
                            self.events.put(gate)
                            await asyncio.to_thread(gate.done.wait, 3)
                            if gate.allowed and not session.muted:
                                player = self.player_factory()
                                player.start()
                                session.player = player
                            else:
                                session.update(audio="muted")
                        else:
                            session.update(audio="muted" if session.muted else "other-call")
                    elif data.get("event") == "media":
                        if not stream_sid or data.get("streamSid") != stream_sid:
                            raise ValueError("Audio does not match this call")
                        if player and not session.muted:
                            media = data["media"]
                            player.add(media["track"], media["timestamp"], media["chunk"], media["payload"])
                            if not played:
                                played = True
                                session.update(audio="live", state="in-progress", detail=CALL_DETAILS["in-progress"])
                    elif data.get("event") == "stop":
                        break
        except (ValueError, KeyError, TypeError, TimeoutError, web.HTTPException, OSError):
            failed = True
            session.update(audio="unavailable")
        except Exception:
            failed = True
            session.update(audio="unavailable")
        finally:
            if player:
                player.close()
                if not failed:
                    session.update(audio="muted" if session.muted else "ended")
            with self.lock:
                if self.active == session.token:
                    self.active = None
                    self.events.put((session.incident, session.token, "ended"))
            self.sockets.discard(socket)
            await socket.close()
        return socket
