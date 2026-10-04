"""Set up Granny Project Telegram photo alerts.

Run: python3 telegram_setup.py {setup,contacts,test-photo} --help
Only test-photo sends messages. This helper does not detect falls or make calls.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
from urllib import error, parse, request
import uuid


CONFIG = Path(__file__).resolve().parent / ".granny" / "telegram.json"


class SetupError(Exception):
    pass


def ssl_context():
    context = ssl.create_default_context()
    # python.org macOS installations may lack default OpenSSL CA certificates.
    # Add Mozilla's roots when available, preserving any configured local roots.
    try:
        import certifi
    except ImportError:
        return context
    context.load_verify_locations(cafile=certifi.where())
    return context


def connection_error(exc, method):
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return SetupError(
            "Python could not verify Telegram's HTTPS certificate. "
            "Run: python3 -m pip install --upgrade certifi, then retry. "
            "If it still fails, check your network's HTTPS certificate configuration."
        )
    if isinstance(reason, socket.gaierror):
        return SetupError(
            "DNS lookup failed for api.telegram.org. "
            "Check your internet connection or try another network."
        )
    if isinstance(reason, TimeoutError):
        message = "Telegram connection timed out. Check your internet connection."
    elif isinstance(reason, ssl.SSLError):
        message = "Telegram HTTPS connection failed. Check your network's HTTPS configuration."
    else:
        message = "Could not connect to Telegram. Check your internet connection or proxy settings."
    if method.startswith("send"):
        message += " Message delivery may be uncertain; check the chat before retrying."
    return SetupError(message)


def api(token, method, fields=None, photo=None):
    fields = fields or {}
    if photo is None:
        data = parse.urlencode(fields).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        content, mime = photo
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                f'\r\n\r\n{value}\r\n'.encode()
            )
        filename = "snapshot.png" if mime == "image/png" else "snapshot.jpg"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
            + content + f"\r\n--{boundary}--\r\n".encode()
        )
        data = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
    req = request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": content_type},
    )
    try:
        with request.urlopen(req, timeout=30, context=ssl_context()) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            result = json.loads(exc.read())
        except (ValueError, OSError):
            raise SetupError(f"Telegram returned HTTP {exc.code}.") from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise connection_error(exc, method) from None
    except ValueError:
        raise SetupError("Telegram returned an unreadable response.") from None
    if not isinstance(result, dict):
        raise SetupError("Telegram returned an unexpected response.")
    if not result.get("ok"):
        description = str(result.get("description", "Unknown API error"))
        raise SetupError(description.replace(token, "[token hidden]"))
    return result["result"]


def save_config(config):
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = CONFIG.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, CONFIG)
    finally:
        temporary.unlink(missing_ok=True)


def load_config():
    if not CONFIG.exists():
        raise SetupError("First run: python3 telegram_setup.py setup")
    try:
        config = json.loads(CONFIG.read_text())
        if not isinstance(config, dict) or not isinstance(config.get("token"), str):
            raise ValueError
        return config
    except ValueError:
        raise SetupError("Local configuration is invalid. Run setup again.") from None


def setup():
    token = getpass.getpass("Paste the BotFather token (input hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SetupError("That does not look like a bot token. Copy it from BotFather.")
    bot = api(token, "getMe")
    recipients = []
    if CONFIG.exists():
        try:
            previous = load_config()
            if previous.get("bot_id") == bot["id"]:
                recipients = previous.get("recipients", [])
        except SetupError:
            pass
    save_config({"token": token, "bot_id": bot["id"],
                 "username": bot["username"], "recipients": recipients})
    print(f"Connected to @{bot['username']}. Token saved locally in .granny/telegram.json.")
    print(f"Have each contact open https://t.me/{bot['username']} and press Start.")
    print("Then run: python3 telegram_setup.py contacts")


def contacts():
    config = load_config()
    token = config["token"]
    if api(token, "getWebhookInfo").get("url"):
        raise SetupError(
            "This bot already uses a webhook. Use a new BotFather bot for this demo "
            "so its existing integration is unaffected."
        )
    updates = api(token, "getUpdates", {"timeout": 0, "limit": 100})
    found = {str(item["id"]): item for item in config.get("recipients", [])}
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        words = message.get("text", "").split()
        if chat.get("type") != "private" or not words:
            continue
        if words[0].split("@")[0] != "/start":
            continue
        name = " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
        found[str(chat["id"])] = {"id": str(chat["id"]), "name": name or "Contact"}
    choices = list(found.values())
    if not choices:
        print(f"No contacts found. Open https://t.me/{config['username']} and send /start.")
        print("Ask your contacts to do the same, then run this command again.")
        return
    if len(updates) == 100:
        print("This bot has a large update backlog; use a fresh demo bot if contacts are missing.")
    for number, person in enumerate(choices, 1):
        print(f"{number}. {person['name']} (chat ID {person['id']})")
    raw = input("Choose consenting recipients by number, separated by commas (e.g. 1,2,3): ")
    try:
        numbers = list(dict.fromkeys(int(part.strip()) for part in raw.split(",")))
        if not numbers or any(number < 1 or number > len(choices) for number in numbers):
            raise ValueError
    except ValueError:
        raise SetupError("Enter valid numbers from the displayed list. Nothing was changed.") from None
    config["recipients"] = [choices[number - 1] for number in numbers]
    save_config(config)
    print(f"Saved {len(numbers)} recipients. No messages sent.")
    if len(numbers) < 3:
        print("You can test with one person now; select at least 3 for the full demo.")


def test_photo(path):
    config = load_config()
    recipients = config.get("recipients", [])
    if not recipients:
        raise SetupError("First run: python3 telegram_setup.py contacts")
    path = path.expanduser()
    if not path.is_file():
        raise SetupError("Photo not found. Supply the path to a JPEG or PNG file.")
    if path.stat().st_size > 10_000_000:
        raise SetupError("Use a photo smaller than 10 MB.")
    content = path.read_bytes()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise SetupError("Use a JPEG or PNG photo. Export HEIC photos to JPEG first.")
    caption = (
        "GRANNY PROJECT — TEST ONLY\n"
        "Photo alert setup test. No emergency or fall has been detected.\n"
        + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    )

    def send(person):
        try:
            api(config["token"], "sendPhoto",
                {"chat_id": person["id"], "caption": caption}, (content, mime))
            return f"Accepted by Telegram for {person['name']} — check their phone.", True
        except SetupError as exc:
            return f"Failed or unconfirmed for {person['name']}: {exc}", False

    print(f"Sending a TEST photo to {len(recipients)} selected recipients...")
    with ThreadPoolExecutor(max_workers=min(4, len(recipients))) as pool:
        results = list(pool.map(send, recipients))
    for message, _ in results:
        print(message)
    return 0 if all(success for _, success in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Privately enter and validate your bot token")
    commands.add_parser("contacts", help="Select people who pressed Start on your bot")
    photo = commands.add_parser("test-photo", help="Send a TEST photo to all selected contacts")
    photo.add_argument("path", type=Path, help="Path to a JPEG or PNG photo")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup()
        elif args.command == "contacts":
            contacts()
        else:
            return test_photo(args.path)
        return 0
    except (SetupError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
