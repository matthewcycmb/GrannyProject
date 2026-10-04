"""Save Wi-Fi credentials privately for the GOOUUU camera firmware build."""
import getpass
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def credentials_header(ssid, password):
    if not 1 <= len(ssid.encode('utf-8')) <= 32 or any(c in ssid for c in '\x00\r\n'):
        raise ValueError('Wi-Fi name must be 1–32 UTF-8 bytes without line breaks.')
    raw_psk = len(password) == 64 and all(c in '0123456789abcdefABCDEF' for c in password)
    if not raw_psk and not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)):
        raise ValueError('Use a WPA/WPA2 password of 8–63 printable ASCII characters (or a 64-digit hex key).')
    # Hex bytes preserve quotes, backslashes and UTF-8 SSIDs without code injection.
    def literal(value):
        return '"' + ''.join(f'\\x{byte:02x}' for byte in value.encode('utf-8')) + '"'
    return ('#pragma once\n// Private local file: do not share or commit.\n'
            f'static const char WIFI_SSID[] = {literal(ssid)};\n'
            f'static const char WIFI_PASSWORD[] = {literal(password)};\n')


def save_credentials(ssid, password, path=ROOT / 'include' / 'secrets.h'):
    header = credentials_header(ssid, password)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(header)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    print('Use the same 2.4 GHz Wi-Fi network as your Mac. Password input is hidden.')
    if not sys.stdin.isatty():
        print('Run this command in Terminal so the password can be entered privately.')
        return 1
    try:
        ssid = input('Wi-Fi name: ')
        password = getpass.getpass('Wi-Fi password: ')
        save_credentials(ssid, password)
        # Compiled firmware also contains credentials. Restrict its output folder.
        build = ROOT / '.pio'
        build.mkdir(exist_ok=True, mode=0o700)
        os.chmod(build, 0o700)
        print('Wi-Fi saved locally. Firmware has not been uploaded.')
        return 0
    except (ValueError, OSError) as exc:
        print(f'Setup failed: {exc}')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nSetup cancelled.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
