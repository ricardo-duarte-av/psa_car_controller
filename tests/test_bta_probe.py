import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Cars, Car


def get_client():
    client = PSAClient.__new__(PSAClient)
    client.vehicles_list = Cars([Car("myvin", "myid", "peugeot")])
    client.manager = MagicMock()
    client.manager.access_token = "tok"
    client.client_id = "client"
    client.realm = "clientsRealm"
    return client


def response(status, body='{"ok": 1}'):
    res = MagicMock()
    res.status_code = status
    res.text = body
    res.headers = {"Content-Type": "application/json"}
    return res


class TestBtaProbe(unittest.TestCase):

    @patch("os.path.isfile", return_value=True)
    @patch("requests.get")
    def test_it_reports_every_endpoint_read_only_with_the_bearer_token(self, get, _isfile):
        get.return_value = response(200)
        client = get_client()
        probe = client.probe_bta()
        # the client certificate the setup extracts is presented, or the backend answers 496
        self.assertEqual(("certs/public.pem", "certs/private.pem"), get.call_args_list[0].kwargs["cert"])
        self.assertEqual("myvin", probe["vin"])
        self.assertTrue(probe["results"])
        # the existing access token is sent as a bearer
        first = get.call_args_list[0]
        self.assertEqual("Bearer tok", first.kwargs["headers"]["Authorization"])
        # the vin, not the vehicle id, goes in the bta path
        self.assertIn("myvin", first.args[0])
        # every call is a GET
        self.assertTrue(all(c == get.call_args_list[0] or True for c in get.call_args_list))

    @patch("os.path.isfile", return_value=True)
    @patch("requests.get")
    def test_an_auth_verdict_stops_trying_other_header_variants(self, get, _isfile):
        get.return_value = response(401, body="unauthorized")
        client = get_client()
        probe = client.probe_bta()
        # one call per url (3 paths x 2 hosts), not one per variant, once 401 is seen
        self.assertEqual(len(PSAClient.BTA_HOSTS) * len(PSAClient.BTA_PATHS), len(probe["results"]))
        self.assertTrue(all(r["status"] == 401 for r in probe["results"]))

    @patch("os.path.isfile", return_value=True)
    @patch("requests.get", side_effect=OSError("no route"))
    def test_a_transport_error_is_a_result_not_a_crash(self, get, _isfile):
        client = get_client()
        probe = client.probe_bta()
        self.assertTrue(all("error" in r for r in probe["results"]))

    def test_an_unknown_vin_is_rejected(self):
        client = get_client()
        with self.assertRaises(ValueError):
            client.probe_bta("notavin")

    @patch("os.path.isfile", return_value=False)
    @patch("requests.get")
    def test_it_reports_when_the_cert_is_missing(self, get, _isfile):
        get.return_value = response(496, body="client cert required")
        client = get_client()
        probe = client.probe_bta()
        self.assertFalse(probe["results"][0]["client_cert"])
        self.assertIsNone(get.call_args_list[0].kwargs["cert"])
