import json
import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.psa.mym import MymClient, MymError


def response(status=200, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = text
    return r


class TestMymClient(unittest.TestCase):

    def test_site_code_is_derived_from_brand_and_country(self):
        self.assertEqual("AP_PT_ESP", MymClient("AP", "PT").site_code)

    @patch("requests.post")
    def test_get_token_posts_the_credentials_to_the_brandid_host(self, post):
        post.return_value = response(body={"accessToken": "TICKET"})
        client = MymClient("AP", "FR")
        token = client.get_token("me@example.com", "secret")
        self.assertEqual("TICKET", token)
        self.assertEqual("TICKET", client.token)
        # posted to the peugeot brandid host with the account in the json request
        self.assertIn("id-dcr.peugeot.com", post.call_args.args[0])
        jr = json.loads(post.call_args.kwargs["params"]["jsonRequest"])
        self.assertEqual("secret", jr["fields"]["USR_PASSWORD"]["value"])
        self.assertEqual("AP_FR_ESP", jr["siteCode"])

    @patch("requests.post")
    def test_get_token_raises_when_the_backend_gives_none(self, post):
        post.return_value = response(body={"error": "bad creds"}, text='{"error":"bad creds"}')
        with self.assertRaises(MymError):
            MymClient("AP", "FR").get_token("me", "wrong")

    def test_an_unknown_brand_has_no_brandid_host(self):
        with self.assertRaises(MymError):
            MymClient("ZZ", "FR").get_token("me", "pw")

    @patch("requests.post")
    def test_post_bta_presents_cert_and_token_on_the_rp_host(self, post):
        post.return_value = response(body={"lastPosition": {"lat": 1}})
        client = MymClient("AP", "FR")
        client.token = "TICKET"
        answer, status = client.post_bta("myvin", "lastposition")
        self.assertEqual(200, status)
        self.assertEqual({"lastPosition": {"lat": 1}}, answer)
        self.assertEqual(("certs/public.pem", "certs/private.pem"), post.call_args.kwargs["cert"])
        self.assertEqual("TICKET", post.call_args.kwargs["headers"]["Token"])
        self.assertIn("mw-ap-rp.mym.awsmpsa.com", post.call_args.args[0])
        self.assertIn("/contracts/bta/lastposition", post.call_args.args[0])

    def test_post_bta_needs_a_token_first(self):
        with self.assertRaises(MymError):
            MymClient("AP", "FR").post_bta("vin", "lastposition")

    @patch("requests.post", side_effect=OSError("no route"))
    def test_post_bta_reports_a_transport_error(self, _post):
        client = MymClient("AP", "FR")
        client.token = "T"
        answer, status = client.post_bta("vin", "lastposition")
        self.assertEqual(502, status)
        self.assertIn("error", answer)


class TestBrandDerivation(unittest.TestCase):

    def _client(self, brand, realm):
        from psa_car_controller.psacc.application.psa_client import PSAClient
        c = PSAClient.__new__(PSAClient)
        c.brand = brand
        c.realm = realm
        c.country_code = "PT"
        return c

    def test_brand_code_falls_back_to_the_realm(self):
        self.assertEqual("AP", self._client(None, "clientsB2CPeugeot").brand_code())
        self.assertEqual("AC", self._client(None, "clientsB2CCitroen").brand_code())
        # an explicit brand wins
        self.assertEqual("OP", self._client("OP", "clientsB2CPeugeot").brand_code())

    def test_mym_client_uses_the_derived_brand_and_country(self):
        client = self._client(None, "clientsB2CPeugeot").mym_client()
        self.assertEqual("AP_PT_ESP", client.site_code)

    def test_overrides_win(self):
        client = self._client(None, "clientsB2CPeugeot").mym_client("AC", "FR")
        self.assertEqual("AC_FR_ESP", client.site_code)
