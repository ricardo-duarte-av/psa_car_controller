import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from psa_car_controller.psa.push import (DEFAULT_TRIGGERS, LABEL_PREFIX, PushState, build_monitor,
                                         is_ours, webhook_url)
from tests.test_psa_probe import get_client, response


class TestMonitorBody(unittest.TestCase):

    def test_a_monitor_watches_the_data_worth_pushing(self):
        monitor = build_monitor("events", "https://psacc.example.com/psa/webhook/abc")
        self.assertTrue(monitor["label"].startswith(LABEL_PREFIX))
        watched = [t["data"]["data"] for t in monitor["triggerParam"]["triggers"]]
        self.assertIn("vehicle.energy.charging.status", watched)
        self.assertIn("vehicle.trip", watched)
        self.assertEqual(len(DEFAULT_TRIGGERS), len(watched))
        # every trigger takes part in the expression, or psa would never fire it
        for name, _, _ in DEFAULT_TRIGGERS:
            self.assertIn(name, monitor["triggerParam"]["boolExp"])
        self.assertEqual("https://psacc.example.com/psa/webhook/abc",
                         monitor["subscribeParam"]["callback"]["target"])
        # the status and the position ride with the event, so no call is needed to use it
        self.assertIn("vehicle.status", monitor["extendedEventParam"])

    def test_ours_are_told_apart_from_the_ones_of_the_official_app(self):
        self.assertTrue(is_ours({"label": LABEL_PREFIX + "events"}))
        self.assertTrue(is_ours({"monitor": {"label": LABEL_PREFIX + "events"}}))
        self.assertFalse(is_ours({"label": "aos_callbackremoteLEV"}))
        self.assertFalse(is_ours({"monitor": {"label": "remoteChargeFinished"}}))
        self.assertFalse(is_ours({}))

    def test_the_webhook_carries_the_token_in_its_path(self):
        self.assertEqual("https://a.example.com/psa/webhook/tok",
                         webhook_url("https://a.example.com/", "tok"))


class TestPushState(unittest.TestCase):

    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "psa_push.json")

    def test_the_token_is_kept_between_restarts(self):
        state = PushState(self.file)
        token = state.ensure_token()
        self.assertEqual(token, state.ensure_token())
        self.assertEqual(token, PushState(self.file).load().token)
        self.assertTrue(len(token) > 20)

    def test_a_missing_or_broken_file_is_not_fatal(self):
        self.assertIsNone(PushState(self.file).load().token)
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(PushState(self.file).load().token)

    def test_monitors_are_remembered(self):
        state = PushState(self.file)
        state.ensure_token()
        state.monitors["mid"] = {"vin": "V", "path": "/p", "label": LABEL_PREFIX + "events"}
        state.save()
        reloaded = PushState(self.file).load()
        self.assertEqual({"mid": {"vin": "V", "path": "/p", "label": LABEL_PREFIX + "events"}},
                         reloaded.monitors)
        self.assertTrue(reloaded.to_dict()["enabled"])


class TestCallApi(unittest.TestCase):

    def test_a_write_is_sent_as_json_and_its_status_reported(self):
        client = get_client()
        session = MagicMock()
        session.post.return_value = response(201, body='{"mid": "m1"}')
        session.post.return_value.json.return_value = {"mid": "m1"}
        client.manager._get_session = MagicMock(return_value=session)
        client.manager._bearer_request = lambda method, url, **kwargs: method(url, **kwargs)

        answer, status = client.call_api("POST", "/user/vehicles/x/monitors", {"label": "psacc_events"})

        self.assertEqual(({"mid": "m1"}, 201), (answer, status))
        kwargs = session.post.call_args.kwargs
        self.assertEqual({"label": "psacc_events"}, kwargs["json"])
        self.assertEqual("application/json", kwargs["headers"]["Content-Type"])

    def test_another_host_is_refused(self):
        client = get_client()
        with self.assertRaises(ValueError):
            client.call_api("POST", "https://example.invalid/steal", {})

    def test_a_broken_call_is_reported_not_raised(self):
        client = get_client()
        client.manager._get_session = MagicMock(side_effect=OSError("down"))
        answer, status = client.call_api("GET", "/user/vehicles")
        self.assertEqual(502, status)
        self.assertIn("error", answer)
