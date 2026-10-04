"""Keep frame acquisition off the UI/timer thread and expose frame freshness."""
from pathlib import Path
from http.client import HTTPConnection, HTTPSConnection
import threading
import time
from urllib.parse import urlsplit

OPEN_TIMEOUT_MS = 3000
# Hotspots can pause for ~2 seconds. The app still rejects frames older than
# one second, while the socket gets a short chance to resume before reopening.
READ_TIMEOUT_MS = 3000
RECONNECT_DELAY_SECONDS = .5
SNAPSHOT_TIMEOUT_SECONDS = 1.0
NETWORK_FRAME_INTERVAL_SECONDS = .2
MAX_JPEG_BYTES = 2 * 1024 * 1024


class Camera:
    def __init__(self, source="0"):
        self.source = int(source) if str(source).isdigit() else str(source)
        self.network = (isinstance(self.source, str)
                        and urlsplit(self.source).scheme.lower() in {"http", "https", "rtsp", "rtsps", "udp"})
        self.reconnect = self.network or isinstance(self.source, int)
        self.snapshot = (self.network and urlsplit(self.source).scheme.lower() in {"http", "https"}
                         and urlsplit(self.source).path.rstrip('/') == '/capture')
        self._snapshot_timestamp = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.frame = None
        self.received = 0.0
        self.sequence = 0
        self.error = "Opening camera..."
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def latest(self):
        with self._lock:
            return self.frame, self.received, self.sequence, self.error

    def _run(self):
        import cv2
        while not self._stop.is_set():
            try:
                if self.snapshot:
                    self._read_snapshots(cv2)
                elif self.network and urlsplit(self.source).scheme.lower() == 'udp':
                    self._read_udp(cv2)
                else:
                    self._open_video(cv2)
            except Exception as exc:
                self._unavailable(f"Camera failed ({type(exc).__name__})")
            # Built-in/USB cameras can fail a read after a transient macOS
            # interruption too. Reopen devices, but let recorded files end.
            if not self.reconnect or self._stop.wait(RECONNECT_DELAY_SECONDS):
                return

    def _open_video(self, cv2):
        capture = None
        try:
            if self.network:
                # Bound each attempt so an outage cannot block the alert
                # timer or prevent shutdown. A new connection can recover.
                capture = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS,
                ])
            else:
                capture = cv2.VideoCapture(self.source)
            if not capture.isOpened():
                self._unavailable("Camera stream unavailable" if self.network else
                                  "Camera unavailable. Allow Camera access in macOS Settings.")
            else:
                if isinstance(self.source, int):
                    capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                self._read_frames(capture, cv2)
        finally:
            if capture is not None:
                capture.release()

    def _read_snapshots(self, cv2):
        import numpy as np
        url = urlsplit(self.source)
        connection_class = HTTPSConnection if url.scheme.lower() == 'https' else HTTPConnection
        connection = connection_class(url.hostname, url.port, timeout=SNAPSHOT_TIMEOUT_SECONDS)
        try:
            self._snapshot_loop(cv2, np, connection, url.path + ('?' + url.query if url.query else ''))
        finally:
            connection.close()

    def _snapshot_loop(self, cv2, np, connection, path):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                connection.request('GET', path, headers={'Cache-Control': 'no-cache'})
                with connection.getresponse() as response:
                    if response.status != 200 or response.headers.get_content_type() != 'image/jpeg':
                        raise ValueError('Expected JPEG')
                    length = int(response.headers.get('Content-Length', '0'))
                    if not 4 <= length <= MAX_JPEG_BYTES:
                        raise ValueError('Invalid JPEG size')
                    timestamp = response.headers.get('X-Timestamp', '')
                    if not timestamp or timestamp == self._snapshot_timestamp:
                        raise ValueError('Camera repeated an old capture')
                    chunks = []
                    remaining = length
                    while remaining and not self._stop.is_set():
                        if time.monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS:
                            raise TimeoutError('Delayed JPEG')
                        chunk = response.read1(min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    jpeg = b''.join(chunks)
                if self._stop.is_set():
                    return
                if (time.monotonic() - started >= SNAPSHOT_TIMEOUT_SECONDS or len(jpeg) != length
                        or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9')):
                    raise ValueError('Incomplete or delayed JPEG')
                frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError('Invalid JPEG')
                self._snapshot_timestamp = timestamp
                with self._lock:
                    # Include transfer time in frame age, rather than making a
                    # delayed picture appear fresh when its download finishes.
                    self.frame, self.received = frame, started
                    self.sequence += 1
                    self.error = ''
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self._unavailable(f'Camera stream snapshot unavailable ({reason})')
                return
            self._stop.wait(max(0, NETWORK_FRAME_INTERVAL_SECONDS - (time.monotonic() - started)))

    def _unavailable(self, message):
        with self._lock:
            if self.reconnect:
                # Incident photos are held by the controller. A disconnected
                # camera must not expose its previous frame as a live picture.
                self.frame = None
                self.received = 0.0
                message += ". Reconnecting automatically..."
            self.error = message

    def _read_udp(self, cv2):
        import numpy as np
        from granny.udp_camera import JPEGReceiver
        url = urlsplit(self.source)
        receiver = JPEGReceiver(url.hostname, url.port or 82)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    jpeg, timestamp, captured_after = receiver.receive(self._stop)
                    if self._stop.is_set():
                        return
                    if timestamp == self._snapshot_timestamp:
                        raise ValueError('Repeated camera capture')
                    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        raise ValueError('Invalid camera image')
                    self._snapshot_timestamp = timestamp
                    with self._lock:
                        self.frame, self.received = frame, captured_after
                        self.sequence += 1
                        self.error = ''
                except (OSError, ValueError):
                    # A lost datagram does not invalidate an earlier, still
                    # fresh image. Its timestamp/sequence never advance; the
                    # app's one-second limit still stops stale monitoring.
                    if time.monotonic() - self.latest()[1] >= 1:
                        self._unavailable('Camera stream interrupted')
                self._stop.wait(max(.02, NETWORK_FRAME_INTERVAL_SECONDS - (time.monotonic() - started)))
        finally:
            receiver.close()

    def _read_frames(self, capture, cv2):
        is_file = isinstance(self.source, str) and not self.network and Path(self.source).is_file()
        fps = capture.get(cv2.CAP_PROP_FPS)
        interval = 1 / fps if is_file and 1 <= fps <= 120 else 0
        while not self._stop.is_set():
            start = time.monotonic()
            okay, frame = capture.read()
            if not okay:
                self._unavailable("Video ended" if is_file else
                                  "Camera stream disconnected or timed out" if self.network else
                                  "Camera disconnected or frame read failed")
                return
            with self._lock:
                self.frame = frame
                self.received = time.monotonic()
                self.sequence += 1
                self.error = ""
            if interval:
                self._stop.wait(max(0, interval - (time.monotonic() - start)))

    def close(self):
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=max(OPEN_TIMEOUT_MS, READ_TIMEOUT_MS) / 1000 + .5 if self.network else 2)
