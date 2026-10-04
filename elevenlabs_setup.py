"""Prepare ElevenLabs voice privately. Setup/prepare generate audio using API credit."""
import argparse
import getpass
import sys
import threading

from granny.elevenlabs import (ElevenLabsClient, ElevenLabsError, load_clips,
                               load_api_key, load_config, prepare_clips, save_config)
from granny.speech import Speaker


def setup(voice_id=None):
    environment_key = load_api_key()
    key = environment_key or getpass.getpass("ElevenLabs API key (hidden): ").strip()
    client = ElevenLabsClient(key)
    if voice_id is None:
        voices = client.voices()
        if not voices:
            raise ElevenLabsError("No voices available. Add a voice in ElevenLabs, or pass --voice-id YOUR_VOICE_ID.")
        for index, voice in enumerate(voices, 1):
            name = " ".join(str(voice.get("name", "Voice")).split())[:100]
            print(f"{index}. {name}")
        try:
            index = int(input("Choose a voice by its list number: ")) - 1
            if not 0 <= index < len(voices):
                raise ValueError
        except ValueError:
            raise ElevenLabsError("Choose a number from the voice list.") from None
        voice_id = voices[index]["voice_id"]
    config = {"api_key": key, "voice_id": voice_id}
    save_config(config, store_api_key=not bool(environment_key))
    if environment_key:
        print("Using your .env/environment key; saved the voice choice without copying the key.")
    else:
        print("Key saved privately in .granny/elevenlabs.json.")
    print("Preparing the short demo phrases; API credit applies.")
    prepare_clips(config)
    print("Voice ready. Start: ./run_granny_web.command --elevenlabs")
    print("That option sends microphone audio to ElevenLabs only during response checks; usage charges apply.")
    print("Telegram and phone calls stay OFF unless you enable their flags.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="Read .env or ask for a key privately, select a voice and cache phrases")
    setup_parser.add_argument("--voice-id", help="Use a known voice ID instead of listing voices")
    commands.add_parser("prepare", help="Generate missing fixed phrases using saved configuration and API credit")
    commands.add_parser("check", help="Check cached voice files locally; no network requests")
    commands.add_parser("test-voice", help="Play cached prompts locally; no microphone capture or notifications")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args.voice_id)
        elif args.command == "prepare":
            print("Generating missing phrases with ElevenLabs; API credit applies.")
            prepare_clips(load_config())
        elif args.command == "check":
            load_config()
            clips = load_clips()
            print(f"Local configuration and {len(clips)} cached phrases are ready. Live recognition is not tested by this check.")
        else:
            speaker = Speaker(load_clips())
            print("Playback test only. No microphone audio, messages or calls.")
            try:
                for phrase in ("check", "cancel", "dry_run"):
                    speaker.speak(phrase, threading.Event())
            finally:
                speaker.stop()
        return 0
    except (ElevenLabsError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
