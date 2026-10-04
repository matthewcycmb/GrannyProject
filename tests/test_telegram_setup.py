import io
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import URLError

import telegram_setup as app


class TelegramConnectionTests(unittest.TestCase):
    def test_api_supplies_a_verified_context_when_python_has_no_default_roots(self):
        # Model the python.org macOS installation observed in this bug: no CA roots.
        empty_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        def server(req, timeout, context=None):
            if context is None or context.cert_store_stats()["x509_ca"] == 0:
                raise URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            return io.BytesIO(b'{"ok":true,"result":{"id":123}}')

        with patch("ssl.create_default_context", return_value=empty_context):
            with patch.object(app.request, "urlopen", side_effect=server):
                self.assertEqual(app.api("123:TEST_TOKEN", "getMe"), {"id": 123})

    def test_certificate_failure_has_an_actionable_message(self):
        problem = URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with patch.object(app.request, "urlopen", side_effect=problem):
            with self.assertRaises(app.SetupError) as caught:
                app.api("123:TEST_TOKEN", "getMe")
        self.assertIn("certificate", str(caught.exception).lower())
        self.assertIn("certifi", str(caught.exception))
        self.assertNotIn("delivery", str(caught.exception))

    def test_dns_failure_is_identified(self):
        problem = URLError(socket.gaierror(socket.EAI_NONAME, "Name not known"))
        with patch.object(app.request, "urlopen", side_effect=problem):
            with self.assertRaises(app.SetupError) as caught:
                app.api("123:TEST_TOKEN", "getMe")
        self.assertIn("DNS", str(caught.exception))

    def test_read_timeout_does_not_claim_a_message_may_have_been_sent(self):
        with patch.object(app.request, "urlopen", side_effect=TimeoutError):
            with self.assertRaises(app.SetupError) as caught:
                app.api("123:TEST_TOKEN", "getMe")
        self.assertIn("timed out", str(caught.exception))
        self.assertNotIn("delivery", str(caught.exception))

    def test_send_timeout_warns_about_uncertain_delivery_without_retrying(self):
        with patch.object(app.request, "urlopen", side_effect=TimeoutError) as transport:
            with self.assertRaises(app.SetupError) as caught:
                app.api("123:TEST_TOKEN", "sendPhoto", {"chat_id": "123"})
        self.assertIn("delivery may be uncertain", str(caught.exception))
        transport.assert_called_once()

    def test_network_errors_do_not_expose_tokens(self):
        token = "123:TEST_TOKEN"
        with patch.object(app.request, "urlopen", side_effect=URLError(token)):
            with self.assertRaises(app.SetupError) as caught:
                app.api(token, "getMe")
        self.assertNotIn(token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
