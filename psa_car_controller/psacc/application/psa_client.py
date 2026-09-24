import json
import threading
from datetime import datetime, timedelta, timezone
from json import JSONEncoder
from hashlib import md5
from urllib.parse import parse_qs, urlparse
from sqlite3.dbapi2 import IntegrityError

from oauth2_client.credentials_manager import ServiceInformation
import os
import requests
from urllib3.exceptions import HTTPError

from psa_car_controller.psa.connected_car_api.api.vehicles_api import VehiclesApi
from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psacc.model.car import Cars, Car
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psa.AccountInformation import AccountInformation
from psa_car_controller.psa.RemoteClient import RemoteClient
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.oauth import OpenIdCredentialManager, Oauth2PSACCApiConfig, OauthAPIClient
from .ecomix import Ecomix
from psa_car_controller.common.utils import TIMEOUT_IN_S
from psa_car_controller.psa.constants import realm_info, AUTHORIZE_SERVICE, BRAND

from .abrp import Abrp
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.common.mylogger import CustomLogger

SCOPE = ['openid profile']
# With the battery flat the status api reports a range of 0 alongside a made-up level (100% on an
# empty battery was seen). Real low levels also come with a range of 0, so only higher ones are
# discarded.
MAX_LEVEL_WITHOUT_RANGE = 10
PSA_TRIPS_CACHE_TTL = timedelta(minutes=5)
MAX_PSA_TRIPS_PAGES = 20
CARS_FILE = "cars.json"
DEFAULT_CONFIG_FILENAME = "config.json"

logger = CustomLogger.getLogger(__name__)


class PSAClient:
    def connect(self, code: str):
        self.manager.connect_with_code(code)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(self, refresh_token, client_id, client_secret, remote_refresh_token, customer_id, realm, country_code,
                 brand=None, proxies=None, weather_api=None, abrp=None, co2_signal_api=None):
        self.realm = realm
        self.service_information = ServiceInformation(AUTHORIZE_SERVICE[self.realm],
                                                      realm_info[self.realm]['oauth_url'],
                                                      client_id,
                                                      client_secret,
                                                      SCOPE, True)
        self.client_id = client_id
        self.country_code = country_code
        self.manager = OpenIdCredentialManager.create(self.service_information,
                                                      realm_info[self.realm]["scheme"], self.country_code)
        self.api_config = Oauth2PSACCApiConfig()
        self.api_config.set_refresh_callback(self._refresh_api_token)
        self.manager.refresh_token = refresh_token
        self.account_info = AccountInformation(client_id, customer_id, realm, country_code)
        self.remote_access_token = None
        self.vehicles_list = Cars.load_cars(CARS_FILE)
        self.customer_id = customer_id
        self._config_hash = None
        self.api_config.verify_ssl = True
        self.api_config.api_key['client_id'] = self.client_id
        self.api_config.api_key['x-introspect-realm'] = self.realm
        self.remote_token_last_update = None
        self._record_enabled = False
        self._last_position_date = {}
        self.weather_api = weather_api
        self.brand = brand
        self.info_callback = []
        self.info_refresh_rate = 120
        if abrp is None:
            self.abrp = Abrp()
        else:
            self.abrp: Abrp = Abrp(**abrp)
        self.set_proxies(proxies)
        self.config_file = DEFAULT_CONFIG_FILENAME
        Ecomix.co2_signal_key = co2_signal_api
        self.refresh_thread: threading.Timer = None
        remote_credentials = RemoteCredentials(remote_refresh_token)
        remote_credentials.update_callbacks.append(self.save_config)
        self.remote_client = RemoteClient(self.account_info,
                                          self.vehicles_list,
                                          self.manager,
                                          remote_credentials)
        self.remote_client.event_broker.add_listener(self._record_battery_event)
        self._psa_trips_cache = {}  # vin -> (fetch date, trips)

    def get_app_name(self):
        return realm_info[self.realm]['app_name']

    def api(self) -> VehiclesApi:
        self.api_config.access_token = self.manager.access_token
        api_instance = VehiclesApi(OauthAPIClient(self.api_config))
        return api_instance

    def _refresh_api_token(self) -> bool:
        """Renews the expired access token for the api calls, the one being retried included: they
        read it from api_config, which api() only fills when a call starts. The retry going out with
        the old token, which the refresh revokes, got "Invalid client id or secret"."""
        if not self.manager.refresh_token_now():
            return False
        self.api_config.access_token = self.manager.access_token
        return True

    # Read-only probe of the psa api itself (see docs/api/*.md, generated from psa's spec).
    # Most of those endpoints are marked "OUT OF 1ST RELEASE (R-LEV 1.1) SCOPE" and a car only
    # answers for the services its subscription covers, so asking is the only way to know.
    # Nothing here changes anything: every call is a GET.
    PROBE_ENDPOINTS = [
        ("user", "/user", {}),
        ("vehicles", "/user/vehicles", {}),
        ("vehicle", "/user/vehicles/{id}", {}),
        ("status", "/user/vehicles/{id}/status", {"profile": "endUser"}),
        ("lastPosition", "/user/vehicles/{id}/lastPosition", {}),
        ("maintenance", "/user/vehicles/{id}/maintenance", {}),
        ("alerts", "/user/vehicles/{id}/alerts", {}),
        ("trips", "/user/vehicles/{id}/trips", {}),
        ("telemetry", "/user/vehicles/{id}/telemetry", {}),
        ("monitors", "/user/vehicles/{id}/monitors", {}),
        ("collisions", "/user/vehicles/{id}/collisions", {}),
        ("user_trips", "/user/trips", {}),
    ]

    PROBE_PREVIEW_LEN = 1500
    PROBE_MAX_PREVIEW_LEN = 200000
    PROBE_DEFAULT_ACCEPT = "application/hal+json"
    # Some endpoints (lastPosition) answer 406 to hal but serve the same body to anything.
    PROBE_ANY_ACCEPT = "*/*"

    def probe_api(self, vin=None, name=None, preview_len=None, accept=None, path=None, trip_id=None):
        """Call the read-only psa endpoints and report what they answer.

        [name] probes that endpoint alone, [preview_len] asks for a longer preview of the body (a
        hal answer starts with a page of _links, which hides the payload) and [accept] changes the
        Accept header, some endpoints refusing hal with a 406.
        """
        car = self.vehicles_list.get_car_by_vin(vin) if vin else next(iter(self.vehicles_list), None)
        if car is None:
            raise ValueError("no vehicle to probe")
        endpoints = self.PROBE_ENDPOINTS
        if path is not None:
            # The documented endpoints are only part of what a car offers: its own _links advertise
            # more (remotes, callbacks, alarms...), and sub resources need a trip id. Any read only
            # path of the psa api can be asked for, without a new release for each discovery.
            endpoints = [("custom", self.check_probe_path(path), {})]
        elif name is not None:
            endpoints = [e for e in endpoints if e[0] == name]
            if not endpoints:
                raise ValueError("unknown endpoint " + str(name))
        length = min(int(preview_len or self.PROBE_PREVIEW_LEN), self.PROBE_MAX_PREVIEW_LEN)
        results = []
        for endpoint_name, endpoint_path, extra_params in endpoints:
            formatted = endpoint_path.replace("{id}", car.vehicle_id).replace("{tid}", str(trip_id or ""))
            results.append(self._probe_endpoint(endpoint_name, formatted, extra_params,
                                                length, accept or self.PROBE_DEFAULT_ACCEPT))
        return {"vin": car.vin, "results": results}

    @staticmethod
    def check_probe_path(path):
        """Only a path of the psa api itself, so the probe can't be pointed at another host."""
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ValueError("path must be an absolute path of the psa api, e.g. /user/vehicles/{id}/remotes")
        return path

    def _probe_endpoint(self, name, path, extra_params, preview_len=PROBE_PREVIEW_LEN,
                        accept=PROBE_DEFAULT_ACCEPT):
        url = self.api_config.host + path
        params = {"client_id": self.client_id}
        params.update(extra_params)
        entry = {"name": name, "path": path, "accept": accept}
        start = datetime.now()
        try:
            res = self.manager.get(url,
                                   params=params,
                                   headers={"x-introspect-realm": self.realm,
                                            "Accept": accept},
                                   timeout=TIMEOUT_IN_S)
        except Exception as e:  # pylint: disable=broad-except
            # A probe never fails the whole request: the error is the result.
            entry["error"] = str(e)
            return entry
        entry["status"] = res.status_code
        entry["duration_ms"] = int((datetime.now() - start).total_seconds() * 1000)
        entry["content_type"] = res.headers.get("Content-Type", None)
        body = res.text or ""
        entry["size"] = len(body)
        try:
            parsed = res.json()
            entry["keys"] = sorted(parsed.keys()) if isinstance(parsed, dict) else None
            entry["count"] = len(parsed) if isinstance(parsed, list) else None
        except ValueError:
            entry["keys"] = None
        entry["preview"] = body[:preview_len]
        return entry

    def _get_api(self, path, accept=PROBE_DEFAULT_ACCEPT, params=None):
        """GET a psa api path and return its parsed body, or None when it doesn't answer."""
        query = {"client_id": self.client_id}
        query.update(params or {})
        try:
            res = self.manager.get(self.api_config.host + path,
                                   params=query,
                                   headers={"x-introspect-realm": self.realm, "Accept": accept},
                                   timeout=TIMEOUT_IN_S)
        except Exception:  # pylint: disable=broad-except
            logger.exception("get %s:", path)
            return None
        if res.status_code != 200:
            logger.warning("get %s answered %s", path, res.status_code)
            return None
        try:
            return res.json()
        except ValueError:
            logger.warning("get %s didn't answer json", path)
            return None

    def get_last_position(self, vin):
        """The dedicated position endpoint.

        It is served apart from the status, whose lastPosition can stay frozen for days, and it
        refuses hal with a 406, hence the Accept.
        """
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is None:
            return None
        return self._get_api("/user/vehicles/{}/lastPosition".format(car.vehicle_id),
                             accept=self.PROBE_ANY_ACCEPT)

    def get_psa_trips(self, vin):
        """The trips psa itself recorded, richer than the ones psacc rebuilds from polled positions.

        They carry the energy levels at both ends, the consumptions, the average speed and, when the
        car reports its position, the start and stop positions.
        """
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is None:
            return None
        cached = self._psa_trips_cache.get(vin)
        if cached is not None and datetime.now(timezone.utc) - cached[0] < PSA_TRIPS_CACHE_TTL:
            return cached[1]
        trips = []
        params = None
        for _ in range(MAX_PSA_TRIPS_PAGES):
            body = self._get_api("/user/vehicles/{}/trips".format(car.vehicle_id), params=params)
            if body is None:
                return None
            trips.extend((body.get("_embedded", None) or {}).get("trips", []))
            page_token = self._next_page_token(body)
            if page_token is None:
                break
            params = {"pageToken": page_token}
        self._psa_trips_cache[vin] = (datetime.now(timezone.utc), trips)
        return trips

    @staticmethod
    def _next_page_token(body):
        href = ((body.get("_links", None) or {}).get("next", None) or {}).get("href", None)
        if not href:
            return None
        return (parse_qs(urlparse(href).query).get("pageToken", None) or [None])[0]

    def _record_battery_event(self, event_type, data):
        """Keep the car's own battery readings, which trips prefer to the status api level."""
        if event_type != "vehicle" or not self._record_enabled:
            return
        level = data.get("battery_level", None)
        raw_level = ((data.get("raw", None) or {}).get("charging_state", None) or {}).get("soc_batt", None)
        # a reading the plausibility filter replaced with the previous one isn't a reading
        date = RemoteClient.parse_event_date(data.get("date", None))
        if level is None or level != raw_level or date is None or data.get("vin", None) is None:
            return
        Database.record_battery_reading(data["vin"], date, level, data.get("autonomy", None))

    @staticmethod
    def _position_level(electric):
        level = getattr(electric, "level", None)
        if level is not None and level > MAX_LEVEL_WITHOUT_RANGE and getattr(electric, "autonomy", None) == 0:
            logger.info("discard battery level %s%% reported with no range", level)
            return None
        return level

    def call_api(self, method, path, body=None, accept=PROBE_DEFAULT_ACCEPT):
        """Call any path of the psa api, and answer what it answered.

        Monitors and callbacks are created, listed and deleted through paths the generated
        documentation doesn't describe correctly (a monitor lives under a callback), so the shape
        of those calls has to be found against the api itself rather than guessed once in code.
        Restricted to the paths of the psa user api.
        """
        self.check_probe_path(path)
        url = self.api_config.host + path
        headers = {"x-introspect-realm": self.realm, "Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            res = self.manager._bearer_request(  # pylint: disable=protected-access
                getattr(self.manager._get_session(), method.lower()),  # pylint: disable=protected-access
                url, params={"client_id": self.client_id}, headers=headers,
                json=body, timeout=TIMEOUT_IN_S)
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("%s %s:", method, path)
            return {"error": str(e)}, 502
        try:
            answer = res.json()
        except ValueError:
            answer = {"body": res.text[:2000]}
        logger.info("%s %s -> %s", method, path, res.status_code)
        return answer, res.status_code

    # The official app fetches the trips the car logs itself (with their gps track) over bluetooth,
    # then uploads them to a separate "mym" backend, reachable as contracts/bta. This connectedcar
    # api carries none of that. The probe checks, read only, whether the credentials psacc already
    # has are accepted there: if they are, the daemon can serve those trips without the app. If it
    # answers 401/403 the backend wants its own token, which this does not try to obtain.
    # The mym backend requires the client certificate the setup already extracts from the app
    # (assets/MWPMYMA1.pfx -> certs/public.pem + certs/private.pem, see psa/setup/apk_parser.py);
    # it is the same cert app_decoder presents to mw-<brand>-m2c for /api/v1/user. Without it the
    # backend answers "496 Client certificate is required".
    BTA_CERT = ("certs/public.pem", "certs/private.pem")
    # mw-<brand>-m2c is the host app_decoder posts /api/v1/user to; mw-ap-rp answered 405 to GET
    # (endpoint present, wrong method), so the method and the right host still have to be found.
    BTA_HOSTS = ["https://mw-ap-rp.mym.awsmpsa.com",
                 "https://mw-{brand}-m2c.mym.awsmpsa.com",
                 "https://microservices.mym.awsmpsa.com"]
    # GET and OPTIONS only: both are safe, and a 405 or the OPTIONS answer names the real method in
    # its Allow header, so nothing is POSTed (which on these paths could create data).
    BTA_METHODS = ["GET", "OPTIONS"]
    BTA_REPORT_HEADERS = ["Allow", "WWW-Authenticate", "Content-Type", "Access-Control-Allow-Methods"]
    BTA_PATHS = ["/api/v1/user/vehicles/{vin}/contracts/bta",
                 "/api/v1/user/vehicles/{vin}/contracts/bta/trips",
                 "/api/v1/user/vehicles/{vin}/contracts/bta/lastposition"]
    BTA_PREVIEW_LEN = 2000

    def probe_bta(self, vin=None):
        """Ask the mym backend for the bta trips, reusing the token psacc already has.

        Every call is a GET. The token, the client id and a couple of header variants are tried so
        the answer says whether the existing credentials are enough or whether that backend wants a
        separate authentication.
        """
        car = self.vehicles_list.get_car_by_vin(vin) if vin else next(iter(self.vehicles_list), None)
        if car is None:
            raise ValueError("no vehicle to probe")
        token = self.manager.access_token
        header_variants = [
            {"name": "bearer+clientid", "headers": {"x-api-key": self.client_id},
             "params": {"client_id": self.client_id}},
            {"name": "bearer+realm", "headers": {"x-introspect-realm": self.realm,
                                                 "x-api-key": self.client_id},
             "params": {"client_id": self.client_id}},
            {"name": "bearer only", "headers": {}, "params": {}},
        ]
        brand = (self.brand or "ap").lower()
        # the simplest header variant is enough to read the method/auth verdict; the earlier probe
        # already showed the variants don't change it
        variant = header_variants[0]
        results = []
        for host_tpl in self.BTA_HOSTS:
            host = host_tpl.replace("{brand}", brand)
            for path in self.BTA_PATHS:
                url = host + path.replace("{vin}", car.vin)
                for method in self.BTA_METHODS:
                    results.append(self._probe_bta_call(method, url, variant, token))
        return {"vin": car.vin, "results": results}

    def _probe_bta_call(self, method, url, variant, token):
        entry = {"method": method, "url": url, "variant": variant["name"]}
        cert = self.BTA_CERT if all(os.path.isfile(f) for f in self.BTA_CERT) else None
        entry["client_cert"] = cert is not None
        headers = {"Accept": "application/json", "Authorization": "Bearer " + str(token)}
        headers.update(variant["headers"])
        try:
            res = requests.request(method, url, params=variant["params"], headers=headers,
                                   cert=cert, timeout=TIMEOUT_IN_S)
        except Exception as e:  # pylint: disable=broad-except
            entry["error"] = str(e)
            return entry
        entry["status"] = res.status_code
        # the interesting part of a 405/401 is which methods and which auth the backend names
        entry["headers"] = {h: res.headers.get(h) for h in self.BTA_REPORT_HEADERS if res.headers.get(h)}
        body = res.text or ""
        entry["size"] = len(body)
        entry["preview"] = body[:self.BTA_PREVIEW_LEN]
        return entry

    # Peugeot/Citroen/DS/Opel serve public 3d renders of the exact car (colour, trim, options encoded
    # in the url) on visuel3d-secure. The vehicle payload lists them; several "views" repeat the same
    # image, so they are de-duplicated by content once and cached, and the bytes are proxied so a
    # client only ever talks to this daemon, not to psa.
    _pictures_cache = {}

    def get_picture_urls(self, vin):
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is None:
            return None
        body = self._get_api("/user/vehicles/{}".format(car.vehicle_id))
        if body is None:
            return None
        return body.get("pictures", None) or []

    def get_pictures(self, vin):
        """The distinct pictures of the car, fetched and de-duplicated once, then cached."""
        if vin in self._pictures_cache:
            return self._pictures_cache[vin]
        urls = self.get_picture_urls(vin)
        if urls is None:
            return None
        distinct = []
        seen = {}
        for url in urls:
            try:
                res = requests.get(url, timeout=TIMEOUT_IN_S)
            except Exception:  # pylint: disable=broad-except
                logger.exception("get_pictures: %s", url)
                continue
            if res.status_code != 200 or not res.content:
                continue
            digest = md5(res.content).hexdigest()
            if digest in seen:
                continue
            seen[digest] = True
            distinct.append({"content": res.content,
                             "content_type": res.headers.get("Content-Type", "image/png")})
        self._pictures_cache[vin] = distinct
        return distinct

    def brand_code(self):
        """The brand code (AP, AC, ...). self.brand may be unset at runtime, so fall back to the realm."""
        if self.brand:
            return self.brand
        for info in BRAND.values():
            if info["realm"] == self.realm:
                return info["brand_code"]
        return None

    def mym_client(self, brand_code=None, country_code=None):
        """A client for the mym backend, built from this account's brand and country (overridable)."""
        from psa_car_controller.psa.mym import MymClient  # pylint: disable=import-outside-toplevel
        return MymClient(brand_code or self.brand_code(), country_code or self.country_code)

    def account_email(self):
        """The email of the account, read from the psa user endpoint."""
        user = self._get_api("/user")
        if user is None:
            return None
        return user.get("email", None)

    def get_maintenance(self, vin):
        """Distance and days before the next service."""
        car = self.vehicles_list.get_car_by_vin(vin)
        if car is None:
            return None
        return self._get_api("/user/vehicles/{}/maintenance".format(car.vehicle_id))

    def set_proxies(self, proxies):
        if proxies is None:
            proxies = {"http": '', "https": ''}
            self.api_config.proxy = None
        else:
            self.api_config.proxy = proxies['http']
            self.abrp.proxies = proxies
        self.manager.proxies = proxies

    def get_vehicle_info(self, vin, cache=False):
        res = None
        car = self.vehicles_list.get_car_by_vin(vin)
        if cache and car.status is not None:
            res = car.status
        else:
            for _ in range(0, 2):
                try:
                    res = self.api().get_vehicle_status(car.vehicle_id)
                    if res is not None:
                        car.status = res
                        if self._record_enabled:
                            self.record_info(car)
                        return res
                except (ApiException, HTTPError) as ex:
                    logger.error("get_vehicle_info: ApiException: %s", ex, exc_info_debug=True)
            car.status = res
        return res

    def __refresh_vehicle_info(self):
        if self.info_refresh_rate is not None:
            if self.refresh_thread and self.refresh_thread.is_alive():
                logger.debug("refresh_vehicle_info: precedent task still alive")
                self.refresh_thread.cancel()
            self.refresh_thread = threading.Timer(self.info_refresh_rate, self.__refresh_vehicle_info)
            self.refresh_thread.daemon = True
            self.refresh_thread.start()
            try:
                logger.debug("refresh_vehicle_info")
                for car in self.vehicles_list:
                    self.get_vehicle_info(car.vin)
                for callback in self.info_callback:
                    callback()
            except BaseException:
                logger.exception("refresh_vehicle_info: ")

    def start_refresh_thread(self):
        if self.refresh_thread is None:
            self.__refresh_vehicle_info()

    def get_vehicles(self):
        try:
            res = self.api().get_vehicles_by_device()
            # an empty answer isn't an ApiException, it just gives nothing to deserialize
            vehicles = getattr(getattr(res, "embedded", None), "vehicles", None)
            if vehicles is None:
                logger.error("get_vehicles: no vehicle in the api answer, keeping the known cars")
                return self.vehicles_list
            for vehicle in vehicles:
                self.vehicles_list.add(Car(vehicle.vin, vehicle.id, vehicle.brand, vehicle.label))
            self.vehicles_list.save_cars()
        except (ApiException, HTTPError):
            logger.exception("get_vehicles:")
        return self.vehicles_list

    def get_charge_status(self, vin):
        data = self.get_vehicle_info(vin)
        status = data.get_energy('Electric').charging.status
        return status

    def save_config(self, name=None, force=False):
        if name is None:
            name = self.config_file
        config_str = json.dumps(self, cls=PSAClientEncoder, sort_keys=True, indent=4).encode("utf8")
        new_hash = md5(config_str).hexdigest()
        if force or self._config_hash != new_hash:
            with open(name, "wb") as f:
                f.write(config_str)
            self._config_hash = new_hash
            logger.info("save config change")

    @staticmethod
    def load_config(name="config.json"):
        with open(name, "r", encoding="utf-8") as f:
            config_str = f.read()
            config = {**json.loads(config_str)}
            if "country_code" not in config:
                config["country_code"] = input("What is your country code ? (ex: FR, GB, DE, ES...)\n")
            for new_el in ["abrp", "co2_signal_api"]:
                if new_el not in config:
                    config[new_el] = None
            psacc = PSAClient(**config)
            psacc.config_file = name
            return psacc

    def set_record(self, value: bool):
        self._record_enabled = value

    def _is_position_updated(self, vin, position_date) -> bool:
        """Only record a position the api actually updated.

        A stale position recorded under a fresh timestamp makes the car look like it jumped,
        which builds trips with a route it never drove.
        """
        if position_date is None:  # api gives no position date, nothing to compare
            return True
        return self._last_position_date.get(vin, None) != position_date

    def record_info(self, car: Car):  # pylint: disable=too-many-locals
        mileage = car.status.timed_odometer.mileage
        level = car.status.get_energy('Electric').level
        level_fuel = car.status.get_energy('Fuel').level
        if car.is_thermal():
            charge_date = car.status.get_energy('Fuel').updated_at
        else:
            charge_date = car.status.get_energy('Electric').updated_at
        moving = car.status.kinetic.moving

        longitude = car.status.last_position.geometry.coordinates[0]
        latitude = car.status.last_position.geometry.coordinates[1]
        altitude = car.status.last_position.geometry.coordinates[2]
        position_date = car.status.last_position.properties.updated_at
        date = position_date
        if date is None or date < datetime.now(timezone.utc) - timedelta(days=1):  # if position isn't updated
            date = charge_date

        temp = getattr(getattr(getattr(car.status, "environment", None), "air", None), "temp", None)

        logger.debug("vin:%s longitude:%s latitude:%s date:%s mileage:%s level:%s charge_date:%s level_fuel:"
                     "%s moving:%s temp:%s", car.vin, longitude, latitude, date, mileage, level, charge_date,
                     level_fuel, moving, temp)
        position_level = self._position_level(car.status.get_energy('Electric'))
        if self._is_position_updated(car.vin, position_date):
            Database.record_position(self.weather_api, car.vin, mileage, latitude, longitude, altitude, date,
                                     position_level, level_fuel, moving, temp)
            self._last_position_date[car.vin] = position_date
        else:
            # Still record the mileage and levels, which trips are built from, but not the stale
            # coordinates: a car whose gps stopped reporting would otherwise get no trips at all.
            logger.debug("position of %s wasn't updated since %s, recorded without it", car.vin, position_date)
            Database.record_position(self.weather_api, car.vin, mileage, None, None, None, charge_date,
                                     position_level, level_fuel, moving, temp)
        self.abrp.call(car, Database.get_last_temp(car.vin))
        if car.has_battery():
            electric_energy_status = car.status.get_energy('Electric')
            try:
                charging_status = electric_energy_status.charging.status
                charging_mode = electric_energy_status.charging.charging_mode
                charging_rate = electric_energy_status.charging.charging_rate
                autonomy = electric_energy_status.autonomy
                Charging.record_charging(car, charging_status, charge_date, level, latitude, longitude,
                                         self.country_code,
                                         charging_mode, charging_rate, autonomy, mileage)
                logger.debug("charging_status:%s ", charging_status)
            except AttributeError as ex:
                logger.error("charging status not available from api")
                logger.debug(ex)
            try:
                soh = electric_energy_status.battery.health.resistance
                Database.record_battery_soh(car.vin, charge_date, soh)
            except IntegrityError:
                logger.debug("SOH already recorded")
            except AttributeError as ex:
                logger.debug("Failed to record SOH: %s", ex)

    def __iter__(self):
        for key, value in self.__dict__.items():
            yield key, value


class PSAClientEncoder(JSONEncoder):

    def default(self, mp: PSAClient):  # pylint: disable=arguments-renamed
        mpd = {"proxies": mp.manager.proxies,
               "refresh_token": mp.manager.refresh_token,
               "client_secret": mp.service_information.client_secret,
               "abrp": dict(mp.abrp),
               "remote_refresh_token": mp.remote_client.remoteCredentials.refresh_token,
               "customer_id": mp.account_info.customer_id,
               "client_id": mp.account_info.client_id,
               "realm": mp.account_info.realm,
               "country_code": mp.account_info.country_code,
               "weather_api": mp.weather_api,
               "co2_signal_api": Ecomix.co2_signal_key
               }
        return mpd
