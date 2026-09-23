import unittest
from datetime import datetime, timedelta

from pytz import UTC

from psa_car_controller.psacc.application.merged_trips import MergedTrips
from psa_car_controller.psacc.model.trip import Trip
from psa_car_controller.psacc.repository import config_repository
from psa_car_controller.psacc.repository.db import Database
from tests.utils import DATA_DIR, get_new_test_db, vehicule_list, latitude, longitude

T0 = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def iso(date):
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")


def psa_trip(start, minutes, distance, electric=None, electric_autonomy=40, fuel=(38.0, 37.0), fuel_cl=None,
             avg_speed=None, mileage=1000.0):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    trip = {"startedAt": iso(start), "stoppedAt": iso(start + timedelta(minutes=minutes)),
            "duration": minutes * 60, "distance": distance, "startMileage": mileage,
            "startEnergies": [{"type": "Fuel", "level": fuel[0]}],
            "endEnergies": [{"type": "Fuel", "level": fuel[1]}]}
    if electric is not None:
        for key in ("startEnergies", "endEnergies"):  # psa copies its start level into the end one
            trip[key].append({"type": "Electric", "level": electric, "autonomy": electric_autonomy})
    if fuel_cl is not None:
        trip["energyConsumptions"] = [{"type": "Fuel", "consumption": fuel_cl[0], "avgConsumption": fuel_cl[1]}]
    if avg_speed is not None:
        trip["kinetic"] = {"avgSpeed": avg_speed, "maxSpeed": 0.0}
    return trip


def psacc_trip(car, start, end):
    trip = Trip()
    trip.car = car
    trip.start_at, trip.end_at = start, end
    trip.distance = 5
    return trip


class TestMergedTrips(unittest.TestCase):
    def setUp(self):
        config_repository.CONFIG_FILENAME = DATA_DIR + "config.ini"
        get_new_test_db()
        self.car = vehicule_list[1]  # a plug-in hybrid: battery and fuel
        self.assertTrue(self.car.is_hybrid())

    def test_psa_units_are_converted(self):
        trips = MergedTrips.get(self.car, [], [psa_trip(at(0), 20, 20.5, fuel_cl=(32.184, 156.995), avg_speed=16.53)])
        trip = trips[0]
        self.assertEqual("psa", trip.source)
        self.assertAlmostEqual(20 / 60, trip.duration)  # hours, from psa's seconds
        self.assertAlmostEqual(59.5, trip.speed_average, places=1)  # km/h, from m/s
        self.assertAlmostEqual(0.32, trip.consumption_fuel, places=2)  # litres, from centilitres
        self.assertAlmostEqual(1.57, trip.consumption_fuel_km, places=2)
        self.assertEqual(1020.5, trip.mileage)
        self.assertEqual((38.0, 37.0), (trip.start_level_fuel, trip.end_level_fuel))
        self.assertEqual(at(20), trip.end_at)
        self.assertEqual(1, trip.id)

    def test_a_psa_level_with_no_range_is_dropped(self):
        # 23/09/2026: 100% with no range, on a trip driven just before a charge from 5%
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 6.2, electric=100.0, electric_autonomy=0)])[0]
        self.assertIsNone(trip.start_level)
        self.assertIsNone(trip.end_level)
        self.assertIsNone(trip.consumption_km)

    def test_a_trusted_psa_start_level_is_kept_but_not_its_copied_end(self):
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 3.1, electric=80.0, electric_autonomy=32)])[0]
        self.assertEqual((80.0, "psa"), (trip.start_level, trip.start_level_source))
        self.assertIsNone(trip.end_level)

    def test_the_cars_readings_are_preferred(self):
        Database.record_battery_reading(self.car.vin, at(-30), 60, 24)
        Database.record_battery_reading(self.car.vin, at(11), 50, 20)
        # the status api's bogus 100% is recorded too, but the car's own readings win
        Database.record_position(None, self.car.vin, 1000, None, None, None, at(-5), 100, 38, False, None)
        Database.record_position(None, self.car.vin, 1010, None, None, None, at(12), 100, 37, False, None)
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 10, electric=100.0, electric_autonomy=0)])[0]
        self.assertEqual((60, "car"), (trip.start_level, trip.start_level_source))
        self.assertEqual((50, "car"), (trip.end_level, trip.end_level_source))
        self.assertAlmostEqual(10 * self.car.battery_power / 100, trip.consumption)
        self.assertAlmostEqual(10 * self.car.battery_power / 10, trip.consumption_km)

    def test_a_status_reading_is_not_trusted_when_psa_made_its_level_up(self):
        # 23/09/2026: a status row of 21/09 (recorded before such levels were filtered) said 100%,
        # like psa's trip, on a battery that was at 5% half an hour later
        Database.record_position(None, self.car.vin, 1000, None, None, None, at(-2 * 24 * 60), 100, 38, False, None)
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 1.7, electric=100.0, electric_autonomy=0)])[0]
        self.assertIsNone(trip.start_level)
        # the car's own reading still is
        Database.record_battery_reading(self.car.vin, at(-60), 6, 0)
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 1.7, electric=100.0, electric_autonomy=0)])[0]
        self.assertEqual((6, "car"), (trip.start_level, trip.start_level_source))

    def test_the_status_readings_are_used_without_the_cars(self):
        Database.record_position(None, self.car.vin, 1000, None, None, None, at(-5), 60, 38, False, None)
        Database.record_position(None, self.car.vin, 1010, None, None, None, at(12), 55, 37, False, None)
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 10)])[0]
        self.assertEqual((60, "status"), (trip.start_level, trip.start_level_source))
        self.assertEqual((55, "status"), (trip.end_level, trip.end_level_source))

    def test_a_reading_is_not_carried_over_another_trip(self):
        Database.record_battery_reading(self.car.vin, at(-120), 90, 36)
        trips = MergedTrips.get(self.car, [], [psa_trip(at(-60), 30, 10), psa_trip(at(0), 10, 5)])
        # the reading before the first trip isn't the start level of the second one
        self.assertEqual(90, trips[0].start_level)
        self.assertIsNone(trips[1].start_level)

    def test_a_reading_taken_into_a_charge_is_not_an_end_level(self):
        Database.record_battery_reading(self.car.vin, at(-10), 5, 0)
        Database.record_battery_reading(self.car.vin, at(40), 60, 24)  # plugged in right after
        trip = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 3)])[0]
        self.assertEqual(5, trip.start_level)
        self.assertIsNone(trip.end_level)
        self.assertIsNone(trip.consumption)

    def test_the_route_comes_from_psacc_positions_or_psa_ends(self):
        Database.record_position(None, self.car.vin, 1000, latitude, longitude, 10, at(1), 60, 38, False, 20)
        Database.record_position(None, self.car.vin, 1005, latitude + 0.01, longitude, 30, at(5), 58, 38, False, 22)
        with_positions = psa_trip(at(0), 10, 10)
        without = psa_trip(at(60), 10, 10)
        without["startPosition"] = {"geometry": {"coordinates": [-9.14, 38.72]}}
        without["stopPosition"] = {"geometry": {"coordinates": [-9.12, 38.74]}}
        trips = MergedTrips.get(self.car, [], [with_positions, without])
        self.assertEqual({"lat": [latitude, latitude + 0.01], "long": [longitude, longitude]},
                         trips[0].get_positions())
        self.assertEqual(20, trips[0].altitude_diff)
        self.assertEqual(21, trips[0].get_temperature())
        self.assertEqual({"lat": [38.72, 38.74], "long": [-9.14, -9.12]}, trips[1].get_positions())

    def test_psacc_trips_fill_what_psa_missed(self):
        overlapping = psacc_trip(self.car, at(-1), at(12))
        earlier = psacc_trip(self.car, at(-600), at(-580))
        trips = MergedTrips.get(self.car, [overlapping, earlier], [psa_trip(at(0), 10, 10)])
        self.assertEqual(["psacc", "psa"], [t.source for t in trips])
        self.assertIs(earlier, trips[0])
        self.assertEqual([1, 2], [t.id for t in trips])

    def test_the_trip_psa_is_still_recording_is_in_progress(self):
        done, driving = psa_trip(at(0), 10, 6.2), psa_trip(at(60), 17, 5.5)
        done["done"], driving["done"] = True, False
        trips = MergedTrips.get(self.car, [], [done, driving, psa_trip(at(-60), 5, 1)])
        self.assertEqual([False, False, True], [t.in_progress for t in trips])
        self.assertTrue(trips[2].get_info()["in_progress"])

    def test_without_psa_trips_psacc_ones_are_kept(self):
        trip = psacc_trip(self.car, at(0), at(10))
        self.assertEqual([trip], list(MergedTrips.get(self.car, [trip], None)))

    def test_the_web_page_format_is_kept(self):
        info = MergedTrips.get(self.car, [], [psa_trip(at(0), 10, 10)])[0].get_info()
        for key in ("start_at", "duration", "speed_average", "distance", "mileage", "consumption_km",
                    "consumption_fuel_km", "positions", "consumption_by_temp", "altitude_diff", "id"):
            self.assertIn(key, info)
        self.assertEqual(10, info["duration"])  # minutes, like psacc's trips


if __name__ == '__main__':
    unittest.main()
