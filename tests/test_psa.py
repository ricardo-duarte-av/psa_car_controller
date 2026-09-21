from unittest.mock import MagicMock, patch

from psa_car_controller.psa.connected_car_api import Vehicles, ApiClient
from psa_car_controller.psa.constants import DISCONNECTED
from psa_car_controller.psacc.model.car import Car
from tests.data.car_status import ELECTRIC_CAR_STATUS

import unittest

from paho.mqtt.client import MQTTMessage

from psa_car_controller.psa.RemoteClient import MQTT_EVENT_TOPIC, MQTT_RESP_TOPIC, RemoteClient
from psa_car_controller.psa.remote_events import FAILED, PENDING, SUCCESS
from tests.utils import get_rc

import json

message_without_precond = b'{"date":"2022-03-30T12:00:52Z","etat_res_elec":0,"precond_state":{},"charging_state":{"program":{' \
                          b'"hour":22,"minute":30},"available":1,"remaining_time":0,"rate":0,"cable_detected":1,"soc_batt":76,' \
                          b'"autonomy_zev":178,"type":0,"hmi_state":0,"mode":2},"stolen_state":0,"vin":"vin",' \
                          b'"reason":0,"signal_quality":3,"sev_stop_date":"2022-03-30T11:10:25Z","fds":[],"sev_state":0,' \
                          b'"obj_counter":1,"privacy_customer":0,"privacy_applicable":0,"privacy_applicable_max":2,' \
                          b'"superlock_state":0} '
message_without_charge_info = b'{"date":"2022-03-30T13:18:56Z","etat_res_elec":5,"precond_state":{"available":1,' \
    b'"programs":{"program1":{"hour":34,"minute":7,"on":0,"day":[0,0,0,0,0,0,0]},' \
    b'"program2":{"hour":34,"minute":7,"on":0,"day":[0,0,0,0,0,0,0]},"program3":{' \
    b'"hour":34,"minute":7,"on":0,"day":[0,0,0,0,0,0,0]},"program4":{"hour":34,"minute":7,' \
    b'"on":0,"day":[0,0,0,0,0,0,0]}},"asap":0,"status":0,"aff":1},"charging_state":{' \
    b'"program":{"hour":22,"minute":30},"available":1,"rate":0,"cable_detected":1,' \
    b'"soc_batt":61,"type":0,"aff":1,"hmi_state":0,"mode":2},"stolen_state":0,' \
    b'"vin":"VIN","reason":4,"signal_quality":5,' \
    b'"sev_stop_date":"2022-03-30T12:41:10Z","fds":["NDR01","NBM01","NCG01","NAO01",' \
    b'"NAS01"],"sev_state":1,"obj_counter":2,"privacy_customer":0,"privacy_applicable":0,' \
    b'"privacy_applicable_max":2,"superlock_state":0} '


class TestUnit(unittest.TestCase):

    @patch('time.sleep', return_value=None)
    def test_fix_not_updated_api(self, patched_time_sleep):
        # GIVEN
        remote_client = get_rc()
        vin = "myvin"
        car = Car("a", "b", "c")
        car.status = ApiClient()._ApiClient__deserialize(ELECTRIC_CAR_STATUS, "Status")
        car.status.get_energy('Electric').charging.status = DISCONNECTED
        remote_client.vehicles_list.get_car_by_vin = MagicMock(return_value=car)
        remote_client.wakeup = MagicMock()
        # WHEN
        remote_client._fix_not_updated_api({'rate': 1}, vin)
        # THEN
        remote_client.wakeup.assert_called_once_with(vin)

    def test_message_without_precond(self):
        remote_client = get_rc()
        msg = MQTTMessage(topic=MQTT_EVENT_TOPIC.encode("utf-8"))
        msg.payload = message_without_precond
        remote_client._on_mqtt_message(None, None, msg)
        self.assertEqual(remote_client.precond_programs, {})

    def test_message_without_charge_info(self):
        remote_client = get_rc()
        msg = MQTTMessage(topic=MQTT_EVENT_TOPIC.encode("utf-8"))
        msg.payload = message_without_charge_info
        remote_client._on_mqtt_message(None, None, msg)


class TestRemoteCommand(unittest.TestCase):
    """Command results and the 400 handling."""

    @staticmethod
    def get_response_message(payload):
        msg = MQTTMessage(topic=MQTT_RESP_TOPIC.encode("utf-8"))
        msg.payload = payload
        return msg

    def get_remote_client(self):
        remote_client = get_rc()
        remote_client.account_info.get_mqtt_customer_id = MagicMock(return_value="cid")
        remote_client.mqtt_client = MagicMock()
        remote_client._refresh_remote_token = MagicMock(return_value=True)
        remote_client.remoteCredentials = MagicMock()
        remote_client.remoteCredentials.access_token = "token"
        return remote_client

    def send_command(self, remote_client):
        return remote_client.lights("myvin", 10)

    def test_command_result_success(self):
        # GIVEN a sent command
        remote_client = self.get_remote_client()
        result = self.send_command(remote_client)
        self.assertEqual(PENDING, result.status)
        # WHEN the car answers
        payload = json.dumps({"return_code": "0", "correlation_id": result.correlation_id}).encode("utf-8")
        remote_client._on_mqtt_message(None, None, self.get_response_message(payload))
        # THEN the result is exposed
        self.assertEqual(SUCCESS, result.status)
        self.assertIs(result, remote_client.get_command_result(result.correlation_id))

    def test_refusal_is_not_retried(self):
        # GIVEN a sent command
        remote_client = self.get_remote_client()
        result = self.send_command(remote_client)
        remote_client.mqtt_client.publish.reset_mock()
        # WHEN psa refuses it with a 400
        payload = json.dumps({"return_code": "400", "reason": "no.matching.service.key",
                              "correlation_id": result.correlation_id}).encode("utf-8")
        remote_client._on_mqtt_message(None, None, self.get_response_message(payload))
        # THEN it isn't sent again and the refusal is reported
        remote_client.mqtt_client.publish.assert_not_called()
        self.assertEqual(FAILED, result.status)
        self.assertIn("isn't available for this car", result.message)
        self.assertIsNone(remote_client.last_request)

    def test_expired_token_is_retried_once(self):
        # GIVEN a sent command
        remote_client = self.get_remote_client()
        result = self.send_command(remote_client)
        remote_client.mqtt_client.publish.reset_mock()
        payload = json.dumps({"return_code": "400", "correlation_id": result.correlation_id}).encode("utf-8")
        # WHEN a 400 without a refusal reason comes back
        remote_client._on_mqtt_message(None, None, self.get_response_message(payload))
        # THEN the command is sent again, and the result follows the new correlation id
        remote_client.mqtt_client.publish.assert_called_once()
        self.assertEqual(PENDING, result.status)
        self.assertIs(result, remote_client.get_command_result(result.correlation_id))
        # WHEN a second 400 comes back
        remote_client.mqtt_client.publish.reset_mock()
        payload = json.dumps({"return_code": "400", "correlation_id": result.correlation_id}).encode("utf-8")
        remote_client._on_mqtt_message(None, None, self.get_response_message(payload))
        # THEN it isn't sent a third time
        remote_client.mqtt_client.publish.assert_not_called()
        self.assertEqual(FAILED, result.status)

    @patch("time.sleep", MagicMock())
    def test_a_car_which_isnt_charging_isnt_woken_up(self):
        """remaining_time is published constantly by the car, it must not be taken for a charge."""
        # GIVEN a car which isn't charging
        remote_client = get_rc()
        vin = "myvin"
        car = Car("a", "b", "c")
        car.status = ApiClient()._ApiClient__deserialize(ELECTRIC_CAR_STATUS, "Status")
        car.status.get_energy('Electric').charging.status = DISCONNECTED
        remote_client.vehicles_list.get_car_by_vin = MagicMock(return_value=car)
        remote_client.wakeup = MagicMock()
        # WHEN an event reports no rate but a remaining time
        remote_client._fix_not_updated_api({'remaining_time': 635, 'rate': 0}, vin)
        # THEN the car is left asleep
        remote_client.wakeup.assert_not_called()

    def test_vehicle_event_reports_a_charge_only_on_rate(self):
        # GIVEN a car plugged in, not charging, with a remaining time
        event = RemoteClient._format_vehicle_event(
            {"vin": "myvin", "charging_state": {"soc_batt": 53, "rate": 0, "remaining_time": 635,
                                                "cable_detected": 1, "autonomy_zev": 24}})
        # THEN it isn't reported as charging
        self.assertFalse(event["charging"])
        self.assertEqual(53, event["battery_level"])
        # AND the unverified raw values stay available
        self.assertEqual(635, event["raw"]["charging_state"]["remaining_time"])
        self.assertNotIn("remaining_time", event)
        # WHEN it charges
        charging = RemoteClient._format_vehicle_event(
            {"vin": "myvin", "charging_state": {"soc_batt": 53, "rate": 12, "remaining_time": 635}})
        self.assertTrue(charging["charging"])

    def test_vehicle_event_is_broadcast(self):
        # GIVEN a subscriber to the event stream
        remote_client = get_rc()
        queue = remote_client.event_broker.subscribe()
        # WHEN a vehicle event is received
        msg = MQTTMessage(topic=MQTT_EVENT_TOPIC.encode("utf-8"))
        msg.payload = message_without_precond
        remote_client._on_mqtt_message(None, None, msg)
        # THEN it's broadcast
        event = queue.get_nowait()
        self.assertEqual("vehicle", event["type"])
        self.assertEqual(76, event["data"]["battery_level"])
        self.assertTrue(event["data"]["cable_plugged"])
        self.assertFalse(event["data"]["charging"])

    def test_implausible_battery_level_is_discarded(self):
        # GIVEN a remote client that accepted a first soc
        remote_client = get_rc()
        vin = "myvin"
        base = '{{"date":"{}","precond_state":{{}},"charging_state":{{"soc_batt":{},"rate":0}},"vin":"myvin"}}'

        def battery_after(date, soc):
            msg = MQTTMessage(topic=MQTT_EVENT_TOPIC.encode("utf-8"))
            msg.payload = base.format(date, soc).encode("utf-8")
            queue = remote_client.event_broker.subscribe()
            remote_client._on_mqtt_message(None, None, msg)
            level = queue.get_nowait()["data"]["battery_level"]
            remote_client.event_broker.unsubscribe(queue)
            return level

        self.assertEqual(0, battery_after("2026-09-21T14:26:54Z", 0))
        # WHEN a lone soc jumps too far for the elapsed time THEN it's discarded, the last one kept
        self.assertEqual(0, battery_after("2026-09-21T14:27:10Z", 66))
        # AND a plausible change is accepted
        self.assertEqual(1, battery_after("2026-09-21T14:37:10Z", 1))
        # AND a big change over a long gap (asleep then charged) is accepted
        self.assertEqual(95, battery_after("2026-09-21T20:37:10Z", 95))
