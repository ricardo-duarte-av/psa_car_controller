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
    client.brand = "AP"
    return client


def response(status, body='{"ok": 1}', headers=None):
    res = MagicMock()
    res.status_code = status
    res.text = body
    res.headers = headers or {"Content-Type": "application/json"}
    return res


class TestBtaProbe(unittest.TestCase):

    @patch("os.path.isfile", return_value=True)
    @patch("requests.request")
    def test_it_probes_each_host_path_and_safe_method_with_the_cert(self, req, _isfile):
        req.return_value = response(405, headers={"Allow": "POST, OPTIONS"})
        client = get_client()
        probe = client.probe_bta()
        # the client certificate the setup extracts is presented
        self.assertEqual(("certs/public.pem", "certs/private.pem"), req.call_args_list[0].kwargs["cert"])
        # bearer token, vin (not vehicle id) in the path
        self.assertEqual("Bearer tok", req.call_args_list[0].kwargs["headers"]["Authorization"])
        self.assertIn("myvin", req.call_args_list[0].args[1])
        # only the safe methods are used, never POST
        methods = {c.args[0] for c in req.call_args_list}
        self.assertEqual({"GET", "OPTIONS"}, methods)
        # the m2c host is derived from the brand
        self.assertTrue(any("mw-ap-m2c" in c.args[1] for c in req.call_args_list))
        # the Allow header the backend returns is reported
        self.assertEqual("POST, OPTIONS", probe["results"][0]["headers"]["Allow"])

    @patch("os.path.isfile", return_value=True)
    @patch("requests.request")
    def test_it_covers_every_host_path_and_method(self, req, _isfile):
        req.return_value = response(405)
        client = get_client()
        probe = client.probe_bta()
        expected = len(PSAClient.BTA_HOSTS) * len(PSAClient.BTA_PATHS) * len(PSAClient.BTA_METHODS)
        self.assertEqual(expected, len(probe["results"]))

    @patch("os.path.isfile", return_value=True)
    @patch("requests.request", side_effect=OSError("no route"))
    def test_a_transport_error_is_a_result_not_a_crash(self, req, _isfile):
        client = get_client()
        probe = client.probe_bta()
        self.assertTrue(all("error" in r for r in probe["results"]))

    def test_an_unknown_vin_is_rejected(self):
        client = get_client()
        with self.assertRaises(ValueError):
            client.probe_bta("notavin")

    @patch("os.path.isfile", return_value=False)
    @patch("requests.request")
    def test_it_reports_when_the_cert_is_missing(self, req, _isfile):
        req.return_value = response(496, body="client cert required")
        client = get_client()
        probe = client.probe_bta()
        self.assertFalse(probe["results"][0]["client_cert"])
        self.assertIsNone(req.call_args_list[0].kwargs["cert"])
