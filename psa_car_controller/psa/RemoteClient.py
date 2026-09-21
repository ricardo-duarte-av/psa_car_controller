import json
import logging
import threading
from datetime import datetime
from os import environ
import time

import paho.mqtt.client as mqtt
from requests import RequestException

from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psa.AccountInformation import AccountInformation
from psa_car_controller.psa.RemoteCredentials import RemoteCredentials
from psa_car_controller.psa.constants import INPROGRESS, DEFAULT_PRECONDITIONING_PROGRAM, IMMEDIATE_CHARGE, \
    DELAYED_CHARGE, REMOTE_URL
from psa_car_controller.psa.mqtt_request import MQTTRequest
from psa_car_controller.psa.oauth import OpenIdCredentialManager
from psa_car_controller.common.utils import RateLimitException, rate_limit, parse_hour, TIMEOUT_IN_S
from psa_car_controller.psa.otp.otp import ConfigException, save_otp, load_otp
from psa_car_controller.psa.remote_events import CommandRegistry, CommandResult, EventBroker, describe_refusal, \
    is_refusal

logger = logging.getLogger(__name__)

MQTT_SERVER = "mwa.mpsa.com"
MQTT_RESP_TOPIC = "psa/RemoteServices/to/cid/"
MQTT_EVENT_TOPIC = "psa/RemoteServices/events/MPHRTServices/"
MQTT_TOKEN_TTL = 890
# The battery soc can't physically move faster than this. The car replays its asleep events in
# bursts with stray soc values (a lone 66 among a run of 0s was observed), so a jump too large for
# the elapsed time between two events is a glitch and the last accepted value is kept instead.
MAX_SOC_PERCENT_PER_MIN = 5


class RemoteException(Exception):
    pass


class RemoteClient:

    def __init__(self, account_info: AccountInformation, vehicles_list: Cars, manager: OpenIdCredentialManager,
                 remoteCredentials: RemoteCredentials):
        self.vehicles_list = vehicles_list
        self.remoteCredentials: RemoteCredentials = remoteCredentials
        self.manager = manager
        self.precond_programs = {}
        self.account_info = account_info
        self.headers = {
            "x-introspect-realm": self.account_info.realm,
            "accept": "application/hal+json",
            "User-Agent": "okhttp/4.8.0",
        }
        self.last_request = None
        self.command_registry = CommandRegistry()
        self.event_broker = EventBroker()
        self.mqtt_client = None
        self.otp = None
        self._lock = threading.Lock()
        self.update_thread: threading.Timer = None
        self._last_battery = {}  # vin -> (level, event date), the last soc accepted from an event

    def __on_mqtt_connect(self, client, userdata, result_code, _):  # pylint: disable=unused-argument
        logger.info("Connected with result code %s", result_code)
        topics = [MQTT_RESP_TOPIC + self.account_info.get_mqtt_customer_id() + "/#"]
        for car in self.vehicles_list:
            topics.append(MQTT_EVENT_TOPIC + car.vin)
        for topic in topics:
            client.subscribe(topic)
            logger.info("subscribe to %s", topic)

    def _on_mqtt_disconnect(self, client, userdata, result_code):  # pylint: disable=unused-argument
        logger.warning("Disconnected with result code %d", result_code)
        if result_code == 1:
            self._refresh_remote_token(force=True)
        else:
            logger.warning(mqtt.error_string(result_code))

    def _on_mqtt_message(self, client, userdata, msg):  # pylint: disable=unused-argument
        try:
            logger.info("mqtt msg received: %s %s", msg.topic, msg.payload)
            data = json.loads(msg.payload)
            charge_info = None
            if msg.topic.startswith(MQTT_RESP_TOPIC):
                self._handle_response(data)
            elif msg.topic.startswith(MQTT_EVENT_TOPIC):
                charge_info = data["charging_state"]
                programs = data["precond_state"].get("programs", None)
                if programs:
                    self.precond_programs[data["vin"]] = data["precond_state"]["programs"]
                event = self._format_vehicle_event(data)
                event["battery_level"] = self._plausible_battery_level(data["vin"], event["battery_level"],
                                                                       event["date"])
                self.event_broker.publish("vehicle", event)
            self._fix_not_updated_api(charge_info, data["vin"])
        except KeyError:
            logger.exception("on_mqtt_message:")

    def _handle_response(self, data):
        result = self._get_command_result(data)
        if "return_code" not in data:
            logger.debug("mqtt msg hasn't return code")
            return
        return_code = data["return_code"]
        reason = data.get("reason", None)
        if return_code == "400":
            self._handle_bad_request(result, reason)
        else:
            if return_code != "0":
                logger.error('mqtt error %s : %s', return_code, reason or "?")
            elif self.last_request is not None and result is not None \
                    and self.last_request.correlation_id == result.correlation_id:
                self.last_request = None
            if result is not None:
                result.set_result(return_code, reason)
        if result is not None:
            self.event_broker.publish("command_result", result.to_dict())

    def _handle_bad_request(self, result: CommandResult, reason):
        """Handle a 400 answer.

        A 400 doesn't always mean the remote token expired: PSA also answers 400 when it refuses
        the command. Sending those again only loops and burns the token refresh budget, so a
        refusal is reported and a resend is attempted at most once per request.
        """
        last_request = self.last_request
        self.last_request = None
        if is_refusal(reason):
            logger.error("command refused by PSA: %s", describe_refusal(reason, "400"))
        elif last_request is None:
            logger.error("mqtt error 400 (%s), no request to send again", reason or "?")
        elif last_request.retried:
            logger.error("command already sent again after a 400 (%s), giving up", reason or "?")
        else:
            logger.warning("last request is send again, token was expired")
            last_request.retried = True
            if self._refresh_remote_token(force=True):
                self.publish(last_request, result=result)
            elif result is not None:
                result.set_failed("can't refresh the remote token, command not sent", reason, "400")
            return
        if result is not None:
            result.set_result("400", reason)

    def _get_command_result(self, data) -> CommandResult:
        correlation_id = data.get("correlation_id", None)
        result = None
        if correlation_id is not None:
            result = self.command_registry.get(correlation_id)
        if result is None:
            # some answers come back without a correlation id, attach them to the waiting command
            result = self.command_registry.get_last_pending()
            if result is not None:
                logger.debug("answer without known correlation id, attached to %s", result.action)
        return result

    @staticmethod
    def _parse_event_date(date):
        if not date:
            return None
        try:
            return datetime.fromisoformat(date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def _plausible_battery_level(self, vin, level, date):
        """Reject a soc that jumped faster than physically possible, keeping the last accepted value.

        The car replays its asleep events out of order and with stray soc values, so a lone wrong
        reading (e.g. a single 66 between two 0s) would otherwise flash on the live view. The soc is
        rate bounded, so a change too large for the elapsed time between the two events is discarded.
        The first reading of a vin is accepted, later ones are compared to it. Only the live event is
        filtered, the recorded history and charge control read the api level, not this.
        """
        if level is None:
            return None
        event_date = self._parse_event_date(date)
        prev = self._last_battery.get(vin)
        if prev is not None:
            prev_level, prev_date = prev
            if event_date is not None and prev_date is not None:
                dt_min = abs((event_date - prev_date).total_seconds()) / 60  # events can arrive out of order
                max_delta = MAX_SOC_PERCENT_PER_MIN * dt_min + 1  # +1 so a rounded jump on a short gap passes
                if abs(level - prev_level) > max_delta:
                    logger.info("discard implausible battery level %s%% for %s (kept %s%%)", level, vin, prev_level)
                    return prev_level
        self._last_battery[vin] = (level, event_date)
        return level

    @staticmethod
    def _format_vehicle_event(data):
        charging = data.get("charging_state", None) or {}
        precond = data.get("precond_state", None) or {}
        return {"vin": data.get("vin", None),
                "date": data.get("date", None),
                "battery_level": charging.get("soc_batt", None),
                "autonomy": charging.get("autonomy_zev", None),
                # only the rate tells a charge apart: remaining_time is published constantly by the
                # car (observed unchanged for hours while unplugged and discharging), it isn't a
                # countdown and it doesn't mean a charge is running.
                "charging": (charging.get("rate", None) or 0) > 0,
                "charging_rate": charging.get("rate", None),
                # cable_detected has been observed at 1 on a car which wasn't plugged in, so its
                # meaning isn't confirmed: read it as "the car reports a cable", nothing more. The
                # api status (plugged) stays the reliable source. Raw values are kept in "raw".
                "cable_plugged": bool(charging.get("cable_detected", 0)),
                "preconditioning": bool(precond.get("asap", 0)),
                "raw": data}

    def _fix_not_updated_api(self, charge_info, vin):
        # A charge is only in progress when the car reports a rate: remaining_time is a constant of
        # the car, and using it here woke the car up on every event it sent (each wakeup triggering
        # another event), which kept the car awake and exhausted the remote token rate limit.
        if charge_info is not None and (charge_info.get('rate', None) or 0) > 0:
            try:
                car = self.vehicles_list.get_car_by_vin(vin=vin)
                if car is None:
                    logger.debug("car %s is unknown, can't check charging status", vin)
                    return
                if car.status is None:
                    # status isn't fetched yet (startup), nothing to compare the mqtt event with
                    logger.debug("status of %s isn't available yet, skip charge status check", vin)
                    return
                if car.status.get_energy('Electric').charging.status != INPROGRESS:
                    # fix a psa server bug where charge beginning without status api being properly updated
                    logger.warning("charge begin but API isn't updated")
                    time.sleep(60)
                    self.wakeup(vin)
            except (IndexError, AttributeError, RateLimitException):
                logger.exception("on_mqtt_message:")

    def start(self):
        if self.load_otp():
            self.mqtt_client = mqtt.Client(clean_session=True, protocol=mqtt.MQTTv311)
            if environ.get("MQTT_LOG", "0") == "1":
                self.mqtt_client.enable_logger(logger=logger)
            if self._refresh_remote_token():
                self.mqtt_client.tls_set_context()
                self.mqtt_client.on_connect = self.__on_mqtt_connect
                self.mqtt_client.on_message = self._on_mqtt_message
                self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
                self.mqtt_client.connect(MQTT_SERVER, 8885, 60)
                self.mqtt_client.loop_start()
                self.__keep_mqtt()
                return self.mqtt_client.is_connected()
        logger.error("Can't configure MQTT Client")
        return False

    def stop(self):
        if self.mqtt_client:
            logger.info("stop mqtt...")
            self.mqtt_client.on_disconnect = None
            self.mqtt_client.disconnect()
        if self.update_thread:
            self.update_thread.cancel()
            self.update_thread.join(timeout=TIMEOUT_IN_S)

    def __keep_mqtt(self):  # avoid token expiration
        timeout = 3600 * 24  # 1 day
        if len(self.vehicles_list) > 0:
            try:
                self.wakeup(self.vehicles_list[0].vin)
            except Exception:
                logger.exception("__keep_mqtt")
        self.update_thread = threading.Timer(timeout, self.__keep_mqtt)
        self.update_thread.daemon = True
        self.update_thread.start()

    def veh_charge_request(self, vin, hour, minute, charge_type):
        msg = self.mqtt_request(vin, {"program": {"hour": hour, "minute": minute}, "type": charge_type}, "/VehCharge")
        logger.info("veh_charge_request: %s", msg)
        return self.publish(msg)

    def publish(self, mqtt_request: MQTTRequest, store=True, result: CommandResult = None) -> CommandResult:
        """Send a command to the car.

        The answer comes back later on mqtt: the returned CommandResult is the handle to it,
        it starts as pending and is filled by _handle_response. None is returned when the
        command couldn't even be sent.
        """
        if not self._refresh_remote_token():
            logger.error("Can't publish %s: remote token refresh failed", mqtt_request.topic)
            if result is not None:
                result.set_failed("can't refresh the remote token, command not sent")
            return result
        message = mqtt_request.get_message_to_json(self.remoteCredentials.access_token)
        logger.debug("mqtt publish: %s %s", mqtt_request.topic, message)
        if result is None:
            result = self.command_registry.register(mqtt_request.correlation_id, mqtt_request.vin,
                                                    mqtt_request.action)
        else:  # resent request, it got a new correlation id
            self.command_registry.relink(mqtt_request.correlation_id, result)
        self.mqtt_client.publish(mqtt_request.topic, message)
        if store:
            self.last_request = mqtt_request
        return result

    def get_command_result(self, correlation_id) -> CommandResult:
        return self.command_registry.get(correlation_id)

    def mqtt_request(self, vin, req_parameters, topic):
        return MQTTRequest(topic, vin, req_parameters, self.account_info.get_mqtt_customer_id())

    def _refresh_remote_token(self, force=False) -> bool:
        with self._lock:
            bad_remote_token = self.remoteCredentials.refresh_token is None
            if not force and not bad_remote_token and self.remoteCredentials.last_update:
                last_update: datetime = self.remoteCredentials.last_update
                if (datetime.now() - last_update).total_seconds() < MQTT_TOKEN_TTL:
                    return True
            try:
                if not self.manager.refresh_token_now():
                    logger.error("Can't refresh remote token: access token refresh failed")
                    return False
                if bad_remote_token:
                    logger.warning("remote_refresh_token isn't defined")
                else:
                    res = self.manager.post(REMOTE_URL + self.account_info.client_id,
                                            json={"grant_type": "refresh_token",
                                                  "refresh_token": self.remoteCredentials.refresh_token},
                                            headers=self.headers, timeout=TIMEOUT_IN_S)
                    data = res.json()
                    logger.debug("refresh_remote_token: %s", data)
                    if "access_token" in data:
                        self.remoteCredentials.access_token = data["access_token"]
                        bad_remote_token = False
                        if "refresh_token" in data:
                            self.remoteCredentials.refresh_token = data["refresh_token"]
                    else:
                        logger.error("can't refresh_remote_token: %s\n Create a new one", data)
                        bad_remote_token = True
                if bad_remote_token:
                    otp_code = self.get_otp_code()
                    self._get_remote_access_token(otp_code)
                self.remote_token_last_update = datetime.now()
                self.mqtt_client.username_pw_set("IMA_OAUTH_ACCESS_TOKEN", self.remoteCredentials.access_token)
                return True
            except RateLimitException as e:
                logger.error("Can't refresh remote token please wait... %s", e)
                return False
            except (RequestException, KeyError, AttributeError, RemoteException):
                logger.exception("Can't refresh remote token, please redo otp procedure")
                return False

    def get_sms_otp_code(self):
        res = self.manager.post(
            "https://api.groupe-psa.com/applications/cvs/v4/mobile/smsCode?client_id=" + self.account_info.client_id,
            headers=self.headers, timeout=TIMEOUT_IN_S)
        return res

    # 6 otp by day
    @rate_limit(6, 3600 * 24)
    def get_otp_code(self):
        try:
            otp_code = self.otp.get_otp_code()
        except ConfigException:
            logger.exception("get_otp_code:")
            self.load_otp(force_new=True)
            otp_code = self.otp.get_otp_code()
        save_otp(self.otp)
        return otp_code

    def _get_remote_access_token(self, password):
        res = self.manager.post(REMOTE_URL + self.account_info.client_id,
                                json={"grant_type": "password", "password": password},
                                headers=self.headers, timeout=TIMEOUT_IN_S)
        data = res.json()
        try:
            self.remoteCredentials.access_token = data["access_token"]
            self.remoteCredentials.refresh_token = data["refresh_token"]
        except KeyError as e:
            raise RemoteException("get_remote_access_token: bad response" + str(data)) from e
        return res

    def horn(self, vin, count):
        msg = self.mqtt_request(vin, {"nb_horn": count, "action": "activate"}, "/Horn")
        logger.info(msg)
        return self.publish(msg)

    def lights(self, vin, duration: int):
        msg = self.mqtt_request(vin, {"action": "activate", "duration": duration}, "/Lights")
        logger.info(msg)
        return self.publish(msg)

    @rate_limit(6, 60 * 20)
    def wakeup(self, vin):
        logger.info("ask wakeup to %s", vin)
        msg = self.mqtt_request(vin, {"action": "state"}, "/VehCharge/state")
        logger.info(msg)
        return self.publish(msg)

    def lock_door(self, vin, lock: bool):
        if lock:
            value = "lock"
        else:
            value = "unlock"

        msg = self.mqtt_request(vin, {"action": value}, "/Doors")
        logger.info(msg)
        return self.publish(msg)

    def preconditioning(self, vin, activate: bool):
        if activate:
            value = "activate"
        else:
            value = "deactivate"
        if vin in self.precond_programs:
            programs = self.precond_programs[vin]
        else:
            programs = DEFAULT_PRECONDITIONING_PROGRAM
        msg = self.mqtt_request(vin, {"asap": value, "programs": programs}, "/ThermalPrecond")
        logger.info("Preconditioning: %s", msg)
        return self.publish(msg)

    def load_otp(self, force_new=False):
        otp_session = load_otp()
        if otp_session is None or force_new:
            logger.error("Please redo otp config")
            return False
        self.otp = otp_session
        return True

    def change_charge_hour(self, vin, hour, miinute):
        return self.veh_charge_request(vin, hour, miinute, DELAYED_CHARGE)

    def charge_now(self, vin, now):
        if now:
            charge_type = IMMEDIATE_CHARGE
        else:
            charge_type = DELAYED_CHARGE
        hour, minute = self.get_charge_hour(vin)
        res = self.veh_charge_request(vin, hour, minute, charge_type)
        logger.info("charge_now: %s", res)
        return res

    def get_charge_hour(self, vin):
        hour_str = self.vehicles_list.get_car_by_vin(vin).status.get_energy('Electric').charging.next_delayed_time
        try:
            return parse_hour(hour_str)[:2]
        except IndexError:
            logger.exception("Can't get charge hour: %s", hour_str)
            return None
