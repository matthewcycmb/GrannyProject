"""Request a single complete JPEG over small, bounded UDP datagrams.

For the private Granny ESP32 firmware, not arbitrary IP cameras. A new request
nonce and the camera capture timestamp prevent stale or mixed-frame assembly.
"""
import secrets
import socket
import struct
import time

REQUEST = struct.Struct('!2I')
HEADER = struct.Struct('!6I')
REQUEST_MAGIC = 0x47524551
FRAME_MAGIC = 0x474a5047
CHUNK = 1200
MAX_BYTES = 2 * 1024 * 1024
FRAME_TIMEOUT = .4


class JPEGReceiver:
    def __init__(self, host, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        self.socket.connect((host, port))
        self.nonce = secrets.randbits(32)

    def receive(self, stop):
        self.nonce = (self.nonce + 1) & 0xffffffff
        started = time.monotonic()
        self.socket.send(REQUEST.pack(REQUEST_MAGIC, self.nonce))
        chunks = {}
        identity = None
        while not stop.is_set():
            remaining = started + FRAME_TIMEOUT - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Incomplete camera image')
            self.socket.settimeout(remaining)
            data = self.socket.recv(HEADER.size + CHUNK + 1)
            if len(data) <= HEADER.size:
                continue
            magic, nonce, length, offset, seconds, micros = HEADER.unpack_from(data)
            if magic != FRAME_MAGIC or nonce != self.nonce:
                continue
            payload = data[HEADER.size:]
            if (not 4 <= length <= MAX_BYTES or offset >= length or offset % CHUNK
                    or len(payload) != min(CHUNK, length - offset) or micros >= 1000000):
                continue
            key = (length, seconds, micros)
            if identity is None:
                identity = key
            if identity != key:
                continue
            chunks[offset] = payload
            if len(chunks) == (length + CHUNK - 1) // CHUNK:
                jpeg = b''.join(chunks[i] for i in range(0, length, CHUNK))
                if not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
                    raise ValueError('Invalid camera JPEG')
                return jpeg, (seconds, micros), started
        raise InterruptedError('Camera stopped')

    def close(self):
        self.socket.close()
