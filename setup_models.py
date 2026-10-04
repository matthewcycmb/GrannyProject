"""Download the official local pose and speech models once; no API keys needed."""
from pathlib import Path
import argparse
import shutil
import tempfile
from urllib.request import urlopen
import zipfile

from telegram_setup import ssl_context

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / ".granny" / "models"
POSE_MODEL = MODELS / "pose_landmarker_heavy.task"
SPEECH_MODEL = MODELS / "vosk-model-small-en-us-0.15"
POSE_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task"
SPEECH_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"


def download(url, target):
    print(f"Downloading {target.name}...", flush=True)
    with urlopen(url, context=ssl_context(), timeout=60) as response:
        with target.open("wb") as output:
            shutil.copyfileobj(response, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pose-model', choices=('lite', 'full', 'heavy'), default='heavy')
    args = parser.parse_args()
    pose_model = MODELS / f'pose_landmarker_{args.pose_model}.task'
    pose_url = f'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_{args.pose_model}/float16/1/pose_landmarker_{args.pose_model}.task'
    MODELS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=MODELS) as directory:
        staging = Path(directory)
        if not pose_model.exists():
            downloaded = staging / pose_model.name
            download(pose_url, downloaded)
            if not zipfile.is_zipfile(downloaded):
                raise RuntimeError("Downloaded pose model is not a valid task bundle.")
            downloaded.replace(pose_model)
        if not (SPEECH_MODEL / "am" / "final.mdl").exists():
            archive = staging / "speech.zip"
            download(SPEECH_URL, archive)
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if not (staging / member.filename).resolve().is_relative_to(staging.resolve()):
                        raise RuntimeError("Unsafe archive path rejected.")
                bundle.extractall(staging)
            extracted = staging / SPEECH_MODEL.name
            if not (extracted / "am" / "final.mdl").exists():
                raise RuntimeError("Downloaded speech model is incomplete.")
            if SPEECH_MODEL.exists():
                raise RuntimeError(f"Incomplete model exists at {SPEECH_MODEL}; move it aside and retry.")
            extracted.replace(SPEECH_MODEL)
    print("Local pose and speech models are ready.")


if __name__ == "__main__":
    main()
