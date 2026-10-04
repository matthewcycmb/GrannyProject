"""Twilio demo calls, limited to explicitly selected Canadian/US phone numbers."""
import base64
import json
import os
from pathlib import Path
import re
from urllib import error, parse, request
import uuid
from xml.etree import ElementTree

from telegram_setup import ssl_context

CONFIG = Path(__file__).resolve().parent.parent / ".granny" / "twilio.json"
CALL_VOICE = {"voice": "Polly.Joanna-Neural", "language": "en-US"}


def alert_summary(reason):
    """Describe the observed trigger without inventing an injury or a fall."""
    if reason == "Person requested help":
        return "Your loved one has asked for help. Please check on them right away."
    if reason == "No clear response before the deadline":
        return ("Your loved one may have fallen. Granny asked if they were okay, "
                "but didn't get a clear response. Please check on them now.")
    return "Granny detected a possible fall involving your loved one. Please check on them now."


class TwilioError(Exception):
    pass


def phone_number(value):
    if not isinstance(value, str):
        raise TwilioError("Use a full Canadian/US phone number with country code +1.")
    normalized = re.sub(r"[\s().-]", "", value)
    if re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", normalized):
        normalized = "+1" + normalized
    if not re.fullmatch(r"\+1[2-9]\d{2}[2-9]\d{6}", normalized):
        raise TwilioError("Use a full Canadian/US phone number. Emergency and short numbers are disabled.")
    return normalized


def validate_config(config, require_recipients=False):
    if not isinstance(config, dict):
        raise TwilioError("Twilio configuration must be an object.")
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", str(config.get("account_sid", ""))):
        raise TwilioError("Invalid Twilio account SID.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", str(config.get("auth_token", ""))):
        raise TwilioError("Invalid Twilio auth token.")
    config = dict(config)
    config["from_number"] = phone_number(config.get("from_number"))
    recipients = config.get("recipients", [])
    if not isinstance(recipients, list):
        raise TwilioError("Invalid Twilio recipient list.")
    selected = {}
    for person in recipients:
        if not isinstance(person, dict):
            raise TwilioError("Invalid Twilio recipient.")
        number = phone_number(person.get("number"))
        if number == config["from_number"]:
            raise TwilioError("A recipient cannot be the Twilio sending number.")
        selected[number] = {"number": number, "name": str(person.get("name") or "Demo contact")}
    if len(selected) > 5:
        raise TwilioError("Select at most five demo call recipients.")
    if require_recipients and not selected:
        raise TwilioError("Select a call recipient first: .venv/bin/python twilio_setup.py contacts")
    config["recipients"] = list(selected.values())
    return config


def load_config(path=CONFIG, require_recipients=False):
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise TwilioError("Twilio config missing or invalid. Run: .venv/bin/python twilio_setup.py setup") from None
    return validate_config(config, require_recipients)


def save_config(config, path=CONFIG):
    config = validate_config(config)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class TwilioClient:
    def __init__(self, config):
        self.config = validate_config(config)
        self.account_sid = self.config["account_sid"]

    def api(self, resource="", fields=None):
        # All routes are relative to this account on Twilio's fixed HTTPS API host.
        if not re.fullmatch(r"(?:[A-Za-z]+(?:/[A-Z]{2}[0-9a-fA-F]{32})?)?", resource):
            raise TwilioError("Invalid Twilio API resource.")
        suffix = f"/{resource}" if resource else ""
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}{suffix}.json"
        authorization = base64.b64encode(f"{self.account_sid}:{self.config['auth_token']}".encode()).decode()
        data = parse.urlencode(fields, doseq=True).encode() if fields is not None else None
        req = request.Request(url, data=data, headers={"Authorization": f"Basic {authorization}",
                                                     "Content-Type": "application/x-www-form-urlencoded"})
        opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=ssl_context()))
        try:
            with opener.open(req, timeout=15) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            code = ""
            try:
                detail = json.loads(exc.read())
                if isinstance(detail, dict) and isinstance(detail.get("code"), int):
                    code = f", Twilio code {detail['code']}"
            except (ValueError, OSError):
                pass
            raise TwilioError(f"Twilio returned HTTP {exc.code}{code}. Check Voice logs in Twilio Console.") from None
        except (error.URLError, OSError, TimeoutError, ValueError):
            message = "Could not confirm the Twilio response. Check your network and Voice logs."
            if fields is not None:
                message += " A call may have been created; do not retry automatically."
            raise TwilioError(message) from None
        if not isinstance(result, dict):
            raise TwilioError("Twilio returned an unexpected response.")
        return result

    def account(self):
        return self.api()

    def verified_recipients(self):
        page = self.api("OutgoingCallerIds")
        if page.get("next_page_uri"):
            raise TwilioError("There are more verified numbers than one page. Narrow the demo account's caller ID list first.")
        return [{"number": item["phone_number"], "name": item.get("friendly_name", "Demo contact")}
                for item in page.get("outgoing_caller_ids", [])]

    def check(self):
        account = self.account()
        if account.get("status") != "active":
            raise TwilioError("The Twilio account is not active.")
        owned = self.api("IncomingPhoneNumbers").get("incoming_phone_numbers", [])
        sender = next((item for item in owned if item["phone_number"] == self.config["from_number"]), None)
        if sender is None or not sender.get("capabilities", {}).get("voice"):
            raise TwilioError("The configured sending number is not a voice-enabled number on this account.")
        verified = self.verified_recipients()
        if account.get("type") == "Trial":
            allowed = {item["number"] for item in verified}
            if any(item["number"] not in allowed for item in self.config["recipients"]):
                raise TwilioError("A selected call recipient is not verified on this trial account.")
        return {"account_type": account.get("type"), "status": account["status"],
                "voice_enabled": True, "verified": verified}

    def create_call(self, number, incident_id, reason, setup_test=False):
        number = phone_number(number)
        if number not in {person["number"] for person in self.config["recipients"]}:
            raise TwilioError("Call blocked: the number is not a selected demo recipient.")
        response = ElementTree.Element("Response")
        say = ElementTree.SubElement(response, "Say", CALL_VOICE)
        if setup_test:
            say.text = "This is a Granny Project setup test. No fall or emergency has been detected. The test is complete. Goodbye."
        else:
            say.text = alert_summary(reason) + " Emergency services have not been called."
        ElementTree.SubElement(response, "Hangup")
        result = self.api("Calls", {"From": self.config["from_number"], "To": number,
                                    "Twiml": ElementTree.tostring(response, encoding="unicode"),
                                    "Timeout": 20, "TimeLimit": 60})
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", str(result.get("sid", ""))):
            raise TwilioError("Call acceptance is unconfirmed. Check Twilio Voice logs before retrying.")
        return result

    def call_status(self, call_sid):
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", str(call_sid)):
            raise TwilioError("Invalid call ID.")
        return self.api(f"Calls/{call_sid}")
