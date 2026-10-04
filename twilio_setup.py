"""Configure Twilio demo calls. Only test-call --place-call rings a phone."""
import argparse
import getpass
import sys
import uuid

from granny.calls import TwilioClient, TwilioError, load_config, phone_number, save_config, validate_config


def setup():
    config = validate_config({"account_sid": input("Twilio Account SID: ").strip(),
                              "auth_token": getpass.getpass("Twilio Auth Token (hidden): ").strip(),
                              "from_number": input("Your Twilio sending number: ").strip(), "recipients": []})
    TwilioClient(config).check()
    save_config(config)
    print("Twilio account connected. Credentials saved privately. No calls placed.")


def check():
    config = load_config()
    result = TwilioClient(config).check()
    print(f"Twilio {result['account_type']} account is active; sending number supports voice.")
    print(f"{len(result['verified'])} verified numbers; {len(config['recipients'])} selected demo recipients.")
    for person in config["recipients"]:
        print(f"Selected: number ending {person['number'][-4:]}")
    print("Read-only check complete. No calls placed. Calls are OFF until you start Granny with --calls.")


def contacts():
    config = load_config()
    choices = TwilioClient(config).verified_recipients()
    if not choices:
        raise TwilioError("Add and verify your own or a consenting contact's number in Twilio Console first.")
    for index, person in enumerate(choices, 1):
        print(f"{index}. {person['name']} ({person['number']})")
    raw = input("Choose consenting demo recipients by number (e.g. 1,2): ")
    try:
        selected = list(dict.fromkeys(int(part.strip()) for part in raw.split(",")))
        if not selected or any(index < 1 or index > len(choices) for index in selected):
            raise ValueError
    except ValueError:
        raise TwilioError("Choose valid entries from the list. Nothing was changed.") from None
    config["recipients"] = [choices[index - 1] for index in selected]
    save_config(config)
    print(f"Saved {len(selected)} call recipients. No calls placed.")


def test_call(number, place_call):
    config = load_config(require_recipients=True)
    number = phone_number(number)
    if number not in {person["number"] for person in config["recipients"]}:
        raise TwilioError("That number is not a selected demo recipient.")
    if not place_call:
        print(f"Preview: one setup test call to the number ending {number[-4:]}. No call placed.")
        print("It will say that this is a setup test and no fall or emergency has been detected.")
        print("To actually ring the consenting recipient, add --place-call. Twilio usage charges/credit apply.")
        return
    client = TwilioClient(config)
    client.check()
    call = client.create_call(number, uuid.uuid4().hex, "Setup test", setup_test=True)
    print(f"Twilio accepted the setup call: {call['sid']} (status: {call.get('status', 'unknown')}).")
    print("Acceptance does not confirm that the person answered. Check their phone and Twilio Voice logs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Enter credentials privately and validate the voice number")
    commands.add_parser("check", help="Read-only account, voice number and recipient checks")
    commands.add_parser("contacts", help="Choose recipients from existing verified numbers")
    test = commands.add_parser("test-call", help="Preview one setup call; sending requires --place-call")
    test.add_argument("number", help="A selected Canadian/US demo phone number")
    test.add_argument("--place-call", action="store_true", help="Actually place the call; consumes Twilio credit")
    status = commands.add_parser("call-status", help="Read a call's Twilio status without placing another call")
    status.add_argument("call_sid")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup()
        elif args.command == "check":
            check()
        elif args.command == "contacts":
            contacts()
        elif args.command == "test-call":
            test_call(args.number, args.place_call)
        else:
            result = TwilioClient(load_config()).call_status(args.call_sid)
            print(f"Call status: {result.get('status', 'unknown')}; duration: {result.get('duration') or 0} seconds")
        return 0
    except (TwilioError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
