import unittest
from unittest.mock import MagicMock

from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Cars, Car


def response(status, body='{"ok": 1}', content_type="application/hal+json"):
    res = MagicMock()
    res.status_code = status
    res.text = body
    res.headers = {"Content-Type": content_type}
    res.json.return_value = {"ok": 1}
    return res


def get_client():
    client = PSAClient.__new__(PSAClient)  # no network, no config file
    client.vehicles_list = Cars([Car("myvin", "myid", "peugeot")])
    client.manager = MagicMock()
    client.api_config = MagicMock()
    client.api_config.host = "https://api.example.invalid/connectedcar/v4"
    client.client_id = "client"
    client.realm = "realm"
    return client


class TestProbe(unittest.TestCase):

    def test_probe_reports_every_endpoint(self):
        # GIVEN an api which serves some endpoints and refuses others
        client = get_client()
        client.manager.get.side_effect = lambda url, **kwargs: response(200) if "maintenance" in url \
            else response(404, body="not found", content_type="text/plain")
        # WHEN the api is probed
        probe = client.probe_api()
        # THEN every endpoint is reported, with its status
        self.assertEqual("myvin", probe["vin"])
        self.assertEqual(len(PSAClient.PROBE_ENDPOINTS), len(probe["results"]))
        by_name = {r["name"]: r for r in probe["results"]}
        self.assertEqual(200, by_name["maintenance"]["status"])
        self.assertEqual(["ok"], by_name["maintenance"]["keys"])
        self.assertEqual(404, by_name["trips"]["status"])
        # AND the vehicle id is used in the path, not the vin
        called = [kwargs for _, kwargs in [(c.args, c.kwargs) for c in client.manager.get.call_args_list]]
        self.assertEqual("client", called[0]["params"]["client_id"])
        self.assertIn("myid", client.manager.get.call_args_list[2].args[0])

    def test_a_failing_call_is_a_result_not_a_crash(self):
        # GIVEN an api which can't be reached
        client = get_client()
        client.manager.get.side_effect = OSError("no route to host")
        # WHEN the api is probed
        probe = client.probe_api()
        # THEN the error is reported for every endpoint
        self.assertTrue(all("error" in r for r in probe["results"]))

    def test_an_unknown_vin_is_rejected(self):
        client = get_client()
        with self.assertRaises(ValueError):
            client.probe_api("notavin")


class TestProbeOptions(unittest.TestCase):

    def test_one_endpoint_with_a_longer_preview_and_another_accept(self):
        # GIVEN an api answering a long body
        client = get_client()
        client.manager.get.return_value = response(200, body="x" * 5000)
        # WHEN a single endpoint is probed with a longer preview and a plain json Accept
        probe = client.probe_api(name="trips", preview_len=4000, accept="application/json")
        # THEN only that endpoint is called, and the preview isn't cut at the default length
        self.assertEqual(1, len(probe["results"]))
        self.assertEqual("trips", probe["results"][0]["name"])
        self.assertEqual(4000, len(probe["results"][0]["preview"]))
        self.assertEqual("application/json", client.manager.get.call_args.kwargs["headers"]["Accept"])

    def test_the_preview_is_capped(self):
        client = get_client()
        client.manager.get.return_value = response(200, body="x" * 10)
        probe = client.probe_api(name="trips", preview_len=10 ** 9)
        self.assertEqual(10, len(probe["results"][0]["preview"]))

    def test_an_unknown_endpoint_is_rejected(self):
        client = get_client()
        with self.assertRaises(ValueError):
            client.probe_api(name="nope")


class TestProbeAnyPath(unittest.TestCase):

    def test_any_read_only_path_of_the_psa_api(self):
        # GIVEN a car and an api which answers
        client = get_client()
        client.manager.get.return_value = response(200)
        # WHEN a path its _links advertise, but the docs don't, is probed
        probe = client.probe_api(path="/user/vehicles/{id}/remotes")
        # THEN it is called with the vehicle id substituted
        self.assertEqual("custom", probe["results"][0]["name"])
        self.assertTrue(client.manager.get.call_args.args[0].endswith("/user/vehicles/myid/remotes"))

    def test_a_trip_id_is_substituted(self):
        client = get_client()
        client.manager.get.return_value = response(200)
        client.probe_api(path="/user/vehicles/{id}/trips/{tid}/wayPoints", trip_id="atrip")
        self.assertTrue(client.manager.get.call_args.args[0].endswith("/user/vehicles/myid/trips/atrip/wayPoints"))

    def test_the_probe_cannot_be_pointed_at_another_host(self):
        client = get_client()
        for path in ("https://example.invalid/steal", "user/vehicles", "/user/../../x"):
            with self.assertRaises(ValueError):
                client.probe_api(path=path)


class TestApiHelpers(unittest.TestCase):

    def test_last_position_asks_with_a_permissive_accept(self):
        # GIVEN the dedicated endpoint, which refuses hal
        client = get_client()
        client.manager.get.side_effect = lambda url, **kwargs: \
            response(200) if kwargs["headers"]["Accept"] == "*/*" else response(406, body="nope")
        # WHEN the position is asked for
        position = client.get_last_position("myvin")
        # THEN it answers
        self.assertEqual({"ok": 1}, position)

    def test_psa_trips_are_unwrapped(self):
        client = get_client()
        res = response(200)
        res.json.return_value = {"total": 2, "_embedded": {"trips": [{"id": "a"}, {"id": "b"}]}}
        client.manager.get.return_value = res
        self.assertEqual([{"id": "a"}, {"id": "b"}], client.get_psa_trips("myvin"))

    def test_an_endpoint_which_doesnt_answer_gives_none(self):
        client = get_client()
        client.manager.get.return_value = response(404, body="not found")
        self.assertIsNone(client.get_maintenance("myvin"))
        client.manager.get.side_effect = OSError("no route")
        self.assertIsNone(client.get_last_position("myvin"))

    def test_an_unknown_vin_gives_none(self):
        client = get_client()
        self.assertIsNone(client.get_psa_trips("notavin"))
