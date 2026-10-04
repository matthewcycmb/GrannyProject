from copy import deepcopy
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from granny.alerts import AlertDispatcher
from granny.call_relay import CallRelay
from granny.calls import TwilioClient, TwilioError
from granny.controller import Controller
from test_calls import CONFIG


class CallRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = deepcopy(CONFIG)
        self.config['recipients'] = self.config['recipients'][:1]
        self.number = self.config['recipients'][0]['number']
        self.client = TwilioClient(self.config)
        self.sids = []
        def create(resource, fields):
            sid = 'CA' + f'{len(self.sids) + 1:032x}'
            self.sids.append(sid)
            return {'sid': sid, 'status': 'queued'}
        self.client.api = Mock(side_effect=create)
        self.relay = CallRelay(self.config, 'https://calls.example.test', client=self.client, stream_audio=False)
        self.telegram = Mock(return_value={'message_id': 1})
        self.poll = Mock()
        self.alerts = AlertDispatcher(self.directory.name, telegram=True,
            config={'token': '0:FAKE', 'recipients': [{'id': '1'}, {'id': '2'}, {'id': '3'}]},
            client=self.telegram, calls=True, call_config=self.config, call_relay=self.relay,
            call_status_client=self.poll, call_poll_seconds=.001)
        self.default_retry_seconds = self.alerts.call_retry_seconds
        self.alerts.call_retry_seconds = .01
        self.addCleanup(self.alerts.close)

    def start(self):
        self.alerts.submit('incident', None, 'Person requested help').result(timeout=2)

    def finish(self):
        for future in self.alerts.call_futures:
            future.result(timeout=3)

    def session(self, sid):
        return next(s for s in self.relay.sessions.values() if s.sid == sid)

    def reply(self, sid, text):
        with self.relay.lock:
            self.relay.next_xml(self.session(sid), 0, {'SpeechResult': text})

    def row(self):
        return self.alerts.progress.rows['incident'][('calls', self.number)]

    def test_retries_until_spoken_commitment_and_never_resends_telegram(self):
        def poll(sid):
            attempt = self.sids.index(sid) + 1
            if attempt == 4:
                self.reply(sid, "Yes I'm coming")
            return {'status': {1: 'no-answer', 2: 'busy'}.get(attempt, 'completed')}
        self.poll.side_effect = poll
        self.start()
        self.finish()
        self.assertEqual(len(self.sids), 4)
        self.assertEqual(self.row()['confirmation'], 'coming')
        self.assertEqual(self.row()['attempt'], 4)
        self.assertEqual(self.telegram.call_count, 3)
        with sqlite3.connect(self.alerts.database) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM call_attempts').fetchone()[0], 4)
        # Late status/confirmation from the first call cannot overwrite the fourth.
        self.session(self.sids[0]).update(state='failed', confirmation='unavailable')
        self.assertEqual(self.row()['confirmation'], 'coming')
        self.assertNotEqual(self.row()['state'], 'failed')

    def test_refusal_stops_calls_to_that_recipient(self):
        def poll(sid):
            self.reply(sid, "I can't come")
            return {'status': 'completed'}
        self.poll.side_effect = poll
        self.start()
        self.finish()
        self.assertEqual(len(self.sids), 1)
        self.assertEqual(self.row()['confirmation'], 'unavailable')

    def test_reset_during_retry_pause_prevents_another_call(self):
        waiting = threading.Event()
        original = self.alerts._wait_call
        def wait(incident, seconds):
            if seconds == 30:
                waiting.set()
            return original(incident, seconds)
        self.alerts.call_retry_seconds = 30
        self.alerts._wait_call = wait
        self.poll.return_value = {'status': 'no-answer'}
        self.start()
        self.assertTrue(waiting.wait(2))
        controller = Controller(self.alerts)
        controller.monitor.incident_id = 'incident'
        controller.reset()
        self.finish()
        self.assertEqual(len(self.sids), 1)

    def test_shutdown_during_retry_pause_prevents_another_call(self):
        waiting = threading.Event()
        def wait(incident, seconds):
            if seconds == 30:
                waiting.set()
            return original(incident, seconds)
        original = self.alerts._wait_call
        self.alerts._wait_call = wait
        self.alerts.call_retry_seconds = 30
        self.poll.return_value = {'status': 'no-answer'}
        self.start()
        self.assertTrue(waiting.wait(2))
        self.alerts.close()
        self.finish()
        self.assertEqual(len(self.sids), 1)

    def test_uncertain_retry_creation_does_not_keep_dialing(self):
        original = self.client.api.side_effect
        def create(resource, fields):
            if self.sids:
                raise TwilioError('A call may have been created')
            return original(resource, fields)
        self.client.api.side_effect = create
        self.poll.return_value = {'status': 'no-answer'}
        self.start()
        self.finish()
        self.assertEqual(self.client.api.call_count, 2)
        self.assertEqual(self.row()['state'], 'unconfirmed')

    def test_confirmation_from_another_contact_stops_waiting_retries(self):
        people = deepcopy(CONFIG['recipients'])
        self.alerts.call_config['recipients'] = people
        self.relay.config['recipients'] = people
        waiting = threading.Event()
        original = self.alerts._wait_call
        self.alerts.call_retry_seconds = 30
        def wait(incident, seconds):
            if seconds == 30:
                waiting.set()
            return original(incident, seconds)
        self.alerts._wait_call = wait
        def poll(sid):
            if self.session(sid).number == self.number:
                return {'status': 'no-answer'}
            self.assertTrue(waiting.wait(2))
            self.reply(sid, "I'm on my way")
            return {'status': 'completed'}
        self.poll.side_effect = poll
        self.start()
        self.finish()
        self.assertEqual(len(self.sids), 2)
        self.assertEqual(self.row()['state'], 'retry-stopped')
        other = self.alerts.progress.rows['incident'][('calls', people[1]['number'])]
        self.assertEqual(other['confirmation'], 'coming')

    def test_provider_decline_or_cancel_retries_after_six_seconds(self):
        # Exercise default production timing without actually waiting or dialing.
        self.alerts.call_retry_seconds = self.default_retry_seconds
        waits = []
        def wait(incident, seconds):
            waits.append(seconds)
            return self.alerts._calls_active(incident)
        self.alerts._wait_call = wait
        def poll(sid):
            if len(self.sids) == 1:
                return {'status': 'canceled'}
            self.reply(sid, "Yes I'm coming")
            return {'status': 'completed'}
        self.poll.side_effect = poll
        self.start()
        self.finish()
        self.assertEqual(len(self.sids), 2)
        self.assertIn(6, waits)
        self.assertNotIn(30, waits)

    def test_real_six_second_pause_starts_when_previous_call_ends(self):
        self.alerts.call_retry_seconds = self.default_retry_seconds
        dialed = []
        ended = []
        original = self.client.api.side_effect
        def create(resource, fields):
            dialed.append(time.monotonic())
            return original(resource, fields)
        self.client.api.side_effect = create
        def poll(sid):
            if len(self.sids) == 1:
                ended.append(time.monotonic())
                return {'status': 'busy'}
            self.reply(sid, "I'm on my way")
            return {'status': 'completed'}
        self.poll.side_effect = poll
        self.start()
        for future in self.alerts.call_futures:
            future.result(timeout=9)
        self.assertEqual(len(dialed), 2)
        self.assertGreaterEqual(dialed[1] - ended[0], 6)
        self.assertLess(dialed[1] - ended[0], 7.5)

    def test_late_refusal_during_retry_pause_prevents_another_call(self):
        original = self.alerts._wait_call
        def wait(incident, seconds):
            if seconds == .01:
                self.reply(self.sids[0], "I can't come")
            return original(incident, seconds)
        self.alerts._wait_call = wait
        self.poll.return_value = {'status': 'completed'}
        self.start()
        self.finish()
        self.assertEqual(len(self.sids), 1)
        self.assertEqual(self.row()['confirmation'], 'unavailable')
        self.assertEqual(self.row()['state'], 'retry-stopped')

    def test_initial_timeout_is_not_automatically_retried(self):
        self.client.api.side_effect = TwilioError('A call may have been created')
        self.start()
        self.finish()
        self.assertEqual(self.client.api.call_count, 1)
        self.assertEqual(self.row()['state'], 'unconfirmed')
