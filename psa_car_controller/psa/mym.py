"""Read the bta data (the trips the car logs with their gps track) from the mym backend.

Unlike the connectedcar api the rest of psacc uses, this backend needs mutual TLS (the client
cert the setup already extracts, certs/*.pem) and its own token, obtained from the account
password through the same GetAccessToken flow the setup runs (see psa/setup/app_decoder.py). The
bta endpoints answer only to POST (checked: they return 405 "Allow: POST" to anything else).

The call shape is the official app's (its BaseRestAPI, read from the apk): the body is only the site
code and the ticket, the culture is a query parameter of every call, and trips takes "from" and "to"
query parameters in epoch milliseconds:
    POST .../contracts/bta/trips?culture=&from=&to=     the trips
    POST .../contracts/bta/trips/<id>?culture=          the positions of one trip
    POST .../contracts/bta/lastposition?culture=
    POST .../contracts/bta/data?culture=
    POST .../contracts/bta/alerts?culture=&state=
"""
import json
import logging
from datetime import datetime, timezone

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

# the period a trips read covers when none is given
BTA_DEFAULT_PERIOD_MS = 30 * 24 * 3600 * 1000


def bta_time(value) -> int:
    """A bta "from"/"to": epoch milliseconds as the app sends them, from milliseconds or an iso date."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    date = datetime.fromisoformat(str(value))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return int(date.timestamp() * 1000)


class MymError(Exception):
    pass


class MymClient:
    """Talks to the mym backend with the client cert and a token from the account password."""

    def __init__(self, brand_code, country_code, version=APP_VERSION):
        self.brand_code = brand_code
        self.country_code = country_code
        self.version = version
        self.site_code = "{}_{}_ESP".format(brand_code, country_code)
        # every bta call takes a culture query parameter; language usually matches the country for
        # these accounts (pt_PT, fr_FR), overridable from the request when it doesn't.
        self.culture = "{}_{}".format((country_code or "").lower(), country_code)
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

    def post_bta(self, vin, path, extra=None, query=None):
        """POST a bta path with the cert and the token.

        The body carries the site code and the ticket, the query the culture; [extra] merges other
        fields into the body and [query] other query parameters (both can override these), so a read
        shape the app doesn't show can still be tried without changing this code.
        """
        if self.token is None:
            raise MymError("no token, call get_token first")
        url = "{}/api/v1/user/vehicles/{}/contracts/bta/{}".format(self._host(), vin, path.strip("/"))
        payload = {"siteCode": self.site_code, "ticket": self.token}
        if extra:
            payload.update(extra)
        params = {"culture": self.culture}
        if query:
            params.update(query)
        try:
            res = requests.post(
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8",
                         "Token": self.token, "Source-Agent": "App-Android",
                         "User-Agent": "okhttp/4.8.0", "Version": self.version},
                params=params,
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
