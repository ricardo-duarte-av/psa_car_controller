"""Read the bta data (the trips the car logs with their gps track) from the mym backend.

Unlike the connectedcar api the rest of psacc uses, this backend needs mutual TLS (the client
cert the setup already extracts, certs/*.pem) and its own token, obtained from the account
password through the same GetAccessToken flow the setup runs (see psa/setup/app_decoder.py). The
bta endpoints answer only to POST (checked: they return 405 "Allow: POST" to anything else).
"""
import json
import logging

import requests

from psa_car_controller.common.utils import TIMEOUT_IN_S

logger = logging.getLogger(__name__)

# HOST_BRANDID_PROD, read from the app resources, per brand (only the value seen for Peugeot is
# verified; the others follow the same id-dcr pattern and may need adjusting).
BRANDID_HOST = {
    "AP": "https://id-dcr.peugeot.com/mobile-services",
    "AC": "https://id-dcr.citroen.com/mobile-services",
    "DS": "https://id-dcr.driveds.com/mobile-services",
    "OP": "https://id-dcr.opel.com/mobile-services",
    "VX": "https://id-dcr.vauxhall.co.uk/mobile-services",
}

# The mym host that serves the bta contract (checked: it answers, and asks for POST).
BTA_HOST = "https://mw-{brand}-rp.mym.awsmpsa.com"

APP_VERSION = "1.55.0"
CERT = ("certs/public.pem", "certs/private.pem")


class MymError(Exception):
    pass


class MymClient:
    """Talks to the mym backend with the client cert and a token from the account password."""

    def __init__(self, brand_code, country_code, version=APP_VERSION):
        self.brand_code = brand_code
        self.country_code = country_code
        self.version = version
        self.site_code = "{}_{}_ESP".format(brand_code, country_code)
        self.token = None

    def get_token(self, email, password):
        """Obtain the mym token from the account password (the GetAccessToken flow of the setup)."""
        host = BRANDID_HOST.get(self.brand_code)
        if host is None:
            raise MymError("no brandid host for brand " + str(self.brand_code))
        res = requests.post(
            host + "/GetAccessToken",
            headers={"Connection": "Keep-Alive", "Content-Type": "application/json",
                     "User-Agent": "okhttp/2.3.0"},
            params={"jsonRequest": json.dumps(
                {"siteCode": self.site_code, "culture": "fr-FR", "action": "authenticate",
                 "fields": {"USR_EMAIL": {"value": email}, "USR_PASSWORD": {"value": password}}})},
            timeout=TIMEOUT_IN_S)
        data = res.json()
        token = data.get("accessToken", None) or data.get("token", None)
        if not token:
            raise MymError("no token in GetAccessToken answer: " + res.text[:200])
        self.token = token
        return token

    def _host(self):
        return BTA_HOST.replace("{brand}", self.brand_code.lower())

    def post_bta(self, vin, path, body=None):
        """POST a bta path, with the cert and the token. Returns (parsed_or_text, status)."""
        if self.token is None:
            raise MymError("no token, call get_token first")
        url = "{}/api/v1/user/vehicles/{}/contracts/bta/{}".format(self._host(), vin, path.strip("/"))
        payload = body if body is not None else {"siteCode": self.site_code, "ticket": self.token}
        try:
            res = requests.post(
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "Token": self.token, "Source-Agent": "App-Android",
                         "User-Agent": "okhttp/4.8.0", "Version": self.version},
                data=json.dumps(payload),
                cert=CERT,
                timeout=TIMEOUT_IN_S)
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("post_bta %s:", url)
            return {"error": str(e)}, 502
        try:
            answer = res.json()
        except ValueError:
            answer = {"body": res.text[:3000]}
        return answer, res.status_code
