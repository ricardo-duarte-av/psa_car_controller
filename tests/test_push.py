import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from psa_car_controller.psa.push import (LABEL_PREFIX, MAX_TRIGGERS, MONITOR_GROUPS, PushState,
                                         build_callback, build_monitor, build_threshold_monitor,
                                         is_ours, monitors_path, webhook_url)
from tests.test_psa_probe import get_client, response


class TestMonitorBody(unittest.TestCase):

    def test_a_monitor_watches_what_psa_accepts(self):
        label, triggers = MONITOR_GROUPS[0]
        monitor = build_monitor(label, triggers)
        self.assertTrue(monitor["label"].startswith(LABEL_PREFIX))
        watched = [t["data"]["data"] for t in monitor["triggerParam"]["triggers"]]
        self.assertIn("vehicle.energy.charging.status", watched)
        self.assertIn("vehicle.kinetic.moving", watched)
        # psa refuses the documented "OnChange" and takes "onChange"
        self.assertTrue(all(t["data"]["op"] == "onChange" for t in monitor["triggerParam"]["triggers"]))
        # its expression parser refuses "||"
        self.assertNotIn("||", monitor["triggerParam"]["boolExp"])
        for name, _ in triggers:
            self.assertIn(name, monitor["triggerParam"]["boolExp"])
        # the status and the position ride with the event, so no call is needed to use it
        self.assertIn("vehicle.status", monitor["extendedEventParam"])

    def test_no_group_exceeds_what_psa_accepts(self):
        for label, triggers in MONITOR_GROUPS:
            self.assertTrue(0 < len(triggers) <= MAX_TRIGGERS, label)

    def test_too_many_triggers_is_refused_before_psa_does(self):
        with self.assertRaises(ValueError):
            build_monitor("events", [("t%d" % i, "vehicle.kinetic.moving") for i in range(MAX_TRIGGERS + 1)])
        with self.assertRaises(ValueError):
            build_monitor("events", [])

    def test_a_numeric_data_is_watched_with_a_comparison(self):
        monitor = build_threshold_monitor("lowbattery", "level", "vehicle.energy.electric.level",
                                          "lowerThan", 20)
        trigger = monitor["triggerParam"]["triggers"][0]
        self.assertEqual("lowerThan", trigger["data"]["op"])
        self.assertEqual(["20"], trigger["data"]["value"])

    def test_the_callback_holds_the_webhook(self):
        callback = build_callback("events", "https://psacc.example.com/psa/webhook/abc")
        # psa answers "invalid parameter: callback" unless it is at the top level
        self.assertEqual("https://psacc.example.com/psa/webhook/abc",
                         callback["callback"]["webhook"]["target"])
        self.assertTrue(callback["label"].startswith(LABEL_PREFIX))

    def test_a_monitor_lives_under_its_callback(self):
        # /user/vehicles/<id>/monitors, which the documentation describes, answers 404
        self.assertEqual("/user/vehicles/v1/callbacks/c1/monitors", monitors_path("v1", "c1"))

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
        state.callback_id = "cb1"
        state.monitors["mid"] = {"vin": "V", "path": "/p", "label": LABEL_PREFIX + "events"}
        state.save()
        reloaded = PushState(self.file).load()
        self.assertEqual({"mid": {"vin": "V", "path": "/p", "label": LABEL_PREFIX + "events"}},
                         reloaded.monitors)
        self.assertEqual("cb1", reloaded.callback_id)
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
