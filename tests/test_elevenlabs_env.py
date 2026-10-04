import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import elevenlabs_setup
from granny.elevenlabs import ElevenLabsError, load_api_key, load_config, save_config

KEY = "sk_" + "dotenv_test_only" * 2
OLD_KEY = "sk_" + "previous_test_only" * 2


class ElevenLabsEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = Path(self.directory.name) / ".env"
        self.config = self.env.with_name("voice.json")

    def test_dotenv_accepts_quotes_comments_export_and_other_settings(self):
        for value in (KEY, f'"{KEY}"', f"'{KEY}'"):
            with self.subTest(value=value):
                self.env.write_text(f"# Private settings\nOTHER_SETTING=ignored\nexport ELEVENLABS_API_KEY = {value} # key\n")
                self.assertEqual(load_api_key(self.env, {}), KEY)

    def test_process_environment_has_priority_over_dotenv(self):
        self.env.write_text(f"ELEVENLABS_API_KEY={OLD_KEY}\n")
        self.assertEqual(load_api_key(self.env, {"ELEVENLABS_API_KEY": KEY}), KEY)
        self.assertEqual(load_api_key(self.env, {"ELEVENLABS_API_KEY": ""}), OLD_KEY)

    def test_dotenv_rotation_overrides_saved_key_at_runtime(self):
        save_config({"api_key": OLD_KEY, "voice_id": "voice123"}, self.config)
        self.env.write_text(f"ELEVENLABS_API_KEY={KEY}\n")
        self.assertEqual(load_config(self.config, env_path=self.env, environ={}),
                         {"api_key": KEY, "voice_id": "voice123"})

    def test_missing_or_blank_dotenv_preserves_previous_private_setup(self):
        config = {"api_key": OLD_KEY, "voice_id": "voice123"}
        save_config(config, self.config)
        self.assertIsNone(load_api_key(self.env, {}))
        self.assertEqual(load_config(self.config, env_path=self.env, environ={}), config)
        self.env.write_text("ELEVENLABS_API_KEY= # fill in later\n")
        self.assertIsNone(load_api_key(self.env, {}))
        self.assertEqual(load_config(self.config, env_path=self.env, environ={}), config)

    def test_invalid_entries_do_not_expose_secrets_or_execute_shell(self):
        marker = self.env.with_name("must-not-exist")
        for value in (f'"{KEY}', f"{KEY} extra", f"$(touch {marker})", "${ANOTHER_KEY}"):
            self.env.write_text(f"ELEVENLABS_API_KEY={value}\n")
            with self.subTest(value=value), self.assertRaises(ElevenLabsError) as caught:
                load_api_key(self.env, {})
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(value, str(caught.exception))
        self.assertFalse(marker.exists())

    def test_setup_uses_dotenv_without_prompting_copying_or_printing_key(self):
        self.env.write_text(f"ELEVENLABS_API_KEY={KEY}\n")
        output = io.StringIO()

        def save_locally(config, **kwargs):
            save_config(config, self.config, **kwargs)

        with patch("elevenlabs_setup.load_api_key", side_effect=lambda: load_api_key(self.env, {})), \
                patch("elevenlabs_setup.getpass.getpass") as prompt, \
                patch("elevenlabs_setup.ElevenLabsClient") as client, \
                patch("elevenlabs_setup.save_config", side_effect=save_locally), \
                patch("elevenlabs_setup.prepare_clips") as prepare, \
                contextlib.redirect_stdout(output):
            elevenlabs_setup.setup("voice123")

        prompt.assert_not_called()
        client.assert_called_once_with(KEY)
        prepare.assert_called_once_with({"api_key": KEY, "voice_id": "voice123"})
        self.assertEqual(json.loads(self.config.read_text()), {"voice_id": "voice123"})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(KEY, output.getvalue())
        self.assertEqual(load_config(self.config, env_path=self.env, environ={})["api_key"], KEY)


if __name__ == "__main__":
    unittest.main()
