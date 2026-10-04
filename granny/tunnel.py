"""Own one ngrok process, exposing only the signed call endpoint."""
import json
import queue
import re
import shutil
import subprocess
import threading


class CallTunnel:
    def __init__(self):
        self.process = None

    def start(self, port):
        executable = shutil.which("ngrok") or "/opt/homebrew/bin/ngrok"
        try:
            self.process = subprocess.Popen(
                [executable, "http", f"http://127.0.0.1:{port}", "--inspect=false",
                 "--log=stdout", "--log-format=json"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
        except OSError:
            raise RuntimeError("Install ngrok and run ngrok config add-authtoken before using --listen-calls.") from None
        events = queue.Queue()

        def read():
            for line in self.process.stdout:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("url", "").startswith("https://"):
                    events.put(("url", entry["url"]))
                if entry.get("lvl") in {"eror", "error", "crit"}:
                    code = re.search(r"ERR_NGROK_\d+", str(entry))
                    events.put(("error", code.group() if code else "ngrok connection failed"))
            events.put(("error", "ngrok stopped"))

        threading.Thread(target=read, daemon=True, name="granny-tunnel").start()
        try:
            kind, value = events.get(timeout=20)
            if kind != "url":
                raise RuntimeError(f"Call audio tunnel unavailable ({value}). Check ngrok authentication and internet access.")
            return value.rstrip("/")
        except queue.Empty:
            self.close()
            raise RuntimeError("Call audio tunnel did not connect within 20 seconds.") from None
        except Exception:
            self.close()
            raise

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdout:
                self.process.stdout.close()
