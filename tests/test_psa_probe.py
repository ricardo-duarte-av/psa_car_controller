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
