import sys
import unittest
from unittest.mock import MagicMock, patch

from dash import html

from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.model.charge import ChargingMode, ChargePlace
from psa_car_controller.psacc.repository.config_repository import ElectricityPriceConfig
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.utils.utils import Singleton
from psa_car_controller.web import app as web_app, view
from tests.utils import car, date0, date1, date2, date3, get_new_test_db, latitude, longitude, vehicule_list


def charge(modes=("Slow", "Slow", "Slow")):
    # a charge from 40 to 85 of a 46 kWh battery: 20.7 kWh
    for date, level, mode in zip((date0, date1, date2), (40, 60, 80), modes):
        Charging.record_charging(car, "InProgress", date, level, latitude, longitude, "FR", mode, 20, 60, 1000)
    Charging.record_charging(car, "Stopped", date3, 85, latitude, longitude, "FR", "No", 0, 60, 1000)
    return Database.get_last_charge(car.vin)


class TestChargingMode(unittest.TestCase):

    def test_the_api_modes_are_known(self):
        self.assertEqual(ChargingMode.AC, ChargingMode("Slow"))
        self.assertEqual(ChargingMode.DC, ChargingMode("Quick"))
        self.assertEqual(ChargingMode.AC, ChargingMode("slow"))
        self.assertEqual(ChargingMode.DC, ChargingMode("fast"))
        self.assertEqual(ChargingMode.UNKNOWN, ChargingMode("No"))
        self.assertEqual(ChargingMode.UNKNOWN, ChargingMode(None))

    def test_a_charge_started_without_a_mode_takes_the_first_real_one(self):
        get_new_test_db()
        self.assertEqual("Slow", charge(modes=("No", "Slow", "Quick")).charging_mode.value)
        get_new_test_db()
        self.assertEqual("Slow", charge(modes=(None, "Slow", "Slow")).charging_mode.value)

    def test_an_ac_charge_is_not_priced_as_dc(self):
        price = ElectricityPriceConfig(day_price=0.1, dc_charge_price=1, charger_efficiency=1)
        get_new_test_db()
        self.assertEqual(2.07, price.get_price(charge(), []))


class TestChargeEdit(unittest.TestCase):

    def setUp(self):
        get_new_test_db()
        Charging.elec_price = ElectricityPriceConfig(day_price=0.1, charger_efficiency=1)
        self.charge = charge()

    def tearDown(self):
        Charging.elec_price = ElectricityPriceConfig()

    def edit(self, **changes):
        return Charging.edit_charge(car, self.charge.start_at, changes)

    def test_a_new_charge_is_estimated_at_home(self):
        charge_row = Charging.get_chargings()[0]
        self.assertEqual("home", charge_row["place"])
        self.assertFalse(charge_row["price_manual"])
        self.assertEqual(2.07, charge_row["price"])

    def test_a_manual_price_is_kept(self):
        res = self.edit(price=10.26, metered_kw=8.67, place="public")
        self.assertEqual((10.26, 8.67, "public", True),
                         (res["price"], res["metered_kw"], res["place"], res["price_manual"]))
        Charging.set_default_price(vehicule_list)
        Charging.update_chargings(Database.get_db(), Database.get_last_charge(car.vin), car)
        self.assertEqual(10.26, Charging.get_chargings()[0]["price"])

    def test_a_charge_at_work_is_free(self):
        self.assertEqual(0, self.edit(place="work")["price"])
        self.assertEqual(2.07, self.edit(place="home")["price"])

    def test_the_metered_energy_is_what_is_estimated(self):
        self.assertEqual(0.87, self.edit(metered_kw=8.7)["price"])

    def test_a_cleared_price_is_estimated_again(self):
        self.edit(price=10.26)
        res = self.edit(price=None)
        self.assertEqual((2.07, False), (res["price"], res["price_manual"]))

    def test_a_key_left_out_is_kept(self):
        self.edit(place="public", price=5)
        res = self.edit(metered_kw=3)
        self.assertEqual(("public", 5), (res["place"], res["price"]))

    def test_a_charge_in_progress_cant_be_edited(self):
        Charging.record_charging(car, "InProgress", date3.replace(year=2022), 10, latitude, longitude, "FR",
                                 "Slow", 20, 60, 1000)
        with self.assertRaises(ValueError):
            Charging.edit_charge(car, date3.replace(year=2022), {"price": 1})

    def test_an_unknown_charge_is_none(self):
        self.assertIsNone(Charging.edit_charge(car, date1, {"price": 1}))


class TestChargeEditApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the api module builds the controller, which parses the command line
        with patch.object(sys, "argv", ["psa-car-controller"]):
            web_app.config_flask("test", "/", False, "127.0.0.1", 0, view="psa_car_controller.web.view.api")
            from psa_car_controller.web.view import api  # pylint: disable=import-outside-toplevel
        web_app.dash_app.layout = html.Div()  # dash refuses to serve anything without one
        api.APP.myp = MagicMock()
        api.APP.myp.vehicles_list = vehicule_list
        cls.client = web_app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        # leave no app behind: the next config_flask must build its own and register the api routes on it
        web_app.app = web_app.dash_app = None
        sys.modules.pop("psa_car_controller.web.view.api", None)
        if hasattr(view, "api"):
            del view.api
        # nor a controller built from this test's command line
        Singleton._instances.pop(PSACarController, None)  # pylint: disable=protected-access

    def setUp(self):
        get_new_test_db()
        Charging.elec_price = ElectricityPriceConfig(day_price=0.1, charger_efficiency=1)
        charge()

    def tearDown(self):
        Charging.elec_price = ElectricityPriceConfig()

    def patch(self, body, vin=car.vin):
        return self.client.patch(f"/vehicles/{vin}/chargings", json=body)

    def test_a_charge_is_edited(self):
        res = self.patch({"start_at": date0.isoformat().replace("+00:00", "Z"), "price": 10.26, "place": "public"})
        self.assertEqual(200, res.status_code)
        self.assertEqual((10.26, "public"), (res.json["price"], res.json["place"]))

    def test_a_naive_date_is_utc(self):
        res = self.patch({"start_at": date0.replace(tzinfo=None).isoformat(), "place": "work"})
        self.assertEqual((200, 0), (res.status_code, res.json["price"]))

    def test_bad_requests_are_refused(self):
        start_at = date0.isoformat()
        self.assertEqual(400, self.patch({"start_at": "yesterday"}).status_code)
        self.assertEqual(400, self.patch({"start_at": start_at, "place": "garage"}).status_code)
        self.assertEqual(400, self.patch({"start_at": start_at, "price": -1}).status_code)
        self.assertEqual(400, self.patch({"start_at": start_at, "price": "10"}).status_code)
        self.assertEqual(400, self.patch({"start_at": start_at, "metered_kw": True}).status_code)
        self.assertEqual(404, self.patch({"start_at": date1.isoformat()}).status_code)
        self.assertEqual(404, self.patch({"start_at": start_at}, vin="unknown").status_code)

    def test_the_place_is_listed(self):
        self.patch({"start_at": date0.isoformat(), "place": ChargePlace.PUBLIC.value})
        self.assertEqual("public", self.client.get("/vehicles/chargings").json[0]["place"])
