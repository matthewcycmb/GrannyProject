"""Private firmware configuration; no network, hardware or notification calls."""
from pathlib import Path
import tempfile
import unittest

from esp32.setup_wifi import credentials_header, save_credentials


class ESP32WifiTests(unittest.TestCase):
    def test_quoted_and_unicode_ssid_and_password_round_trip_as_bytes(self):
        ssid, password = 'Matthew’s "Wi-Fi"', 'a"b\\c$`12345'
        header = credentials_header(ssid, password)
        for line, expected in zip(header.splitlines()[2:], (ssid, password)):
            encoded = line.split('"')[1].replace('\\x', '')
            self.assertEqual(bytes.fromhex(encoded).decode('utf-8'), expected)

    def test_invalid_credentials_preserve_existing_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'include' / 'secrets.h'
            save_credentials('Demo', 'example-password', path)
            original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for ssid, password in [('', 'example-password'), ('é' * 17, 'example-password'),
                                   ('Demo\n', 'example-password'), ('Demo', 'short'),
                                   ('Demo', 'password\n'), ('Demo', 'g' * 64)]:
                with self.subTest(ssid=ssid), self.assertRaises(ValueError):
                    save_credentials(ssid, password, path)
                self.assertEqual(path.read_bytes(), original)
            save_credentials('Demo', 'a' * 64, path)
            self.assertNotEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
