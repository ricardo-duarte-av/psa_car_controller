"""Trips merged from the ones psa records itself and the ones psacc rebuilds from its polls.

Psa's trips have the right boundaries, the distance, the fuel used and the average speed, and they
don't depend on psacc polling at the time nor on the car's gps. Psacc has what psa leaves out: the
route, the temperature, the altitude and battery levels good enough for the electric consumption
(psa reports none on some hybrids, and its trip battery levels are made up when the battery is
flat). So each psa trip is the base, enriched with what psacc recorded during it, and a trip only
psacc saw (psa missed it, or it predates psa's history) is kept as it is.
"""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from typing import List, Optional

from psa_car_controller.common.mylogger import CustomLogger
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.psacc.model.trip import Trip
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.psacc.repository.trips import Trips, MAX_SPEED

logger = CustomLogger.getLogger(__name__)

MS_TO_KMH = 3.6
CL_PER_LITRE = 100
# see psa_client.MAX_LEVEL_WITHOUT_RANGE: a higher level psa reports with a range of 0 is made up
MAX_LEVEL_WITHOUT_RANGE = 10
# the car reports around the moment the ignition turns on or off, not exactly at it
BOUNDARY_TOLERANCE = timedelta(minutes=2)
# a reading this long after the trip ended may already be into a charge
MAX_END_READING_DELAY = timedelta(hours=1)
# the level can rise a little while driving (regeneration, temperature), more means a wrong reading
MAX_LEVEL_GAIN = 2


class Reading:
    def __init__(self, date: datetime, level, source: str):
        self.date = date
        self.level = level
        self.source = source


class Readings:
    """The battery readings of a car, the car's own ones preferred to the status api's."""

    def __init__(self, car_readings: List[Reading], status_readings: List[Reading]):
        self._by_source = [(r, [x.date for x in r]) for r in (car_readings, status_readings) if r]

    def _sources(self, car_only):
        return [(r, d) for r, d in self._by_source if not car_only or r[0].source == "car"]

    def last_before(self, date: datetime, not_before: Optional[datetime], car_only=False) -> Optional[Reading]:
        for readings, dates in self._sources(car_only):
            i = bisect_right(dates, date + BOUNDARY_TOLERANCE) - 1
            if i >= 0 and (not_before is None or readings[i].date > not_before - BOUNDARY_TOLERANCE):
                return readings[i]
        return None

    def first_after(self, date: datetime, not_after: datetime, car_only=False) -> Optional[Reading]:
        for readings, dates in self._sources(car_only):
            i = bisect_left(dates, date - BOUNDARY_TOLERANCE)
            if i < len(readings) and readings[i].date <= not_after:
                return readings[i]
        return None


def parse_psa_date(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def psa_energy(energies, energy_type):
    for energy in energies or []:
        if (energy.get("type", None) or "").lower() == energy_type.lower():
            return energy
    return None


def psa_level_made_up(energies) -> bool:
    """True when psa gives a level with no range, which it does when the battery is flat."""
    electric = psa_energy(energies, "Electric")
    return electric is not None and (electric.get("level", None) or 0) > MAX_LEVEL_WITHOUT_RANGE \
        and electric.get("autonomy", None) == 0


def psa_electric_level(energies):
    electric = psa_energy(energies, "Electric")
    if electric is None or electric.get("level", None) is None or psa_level_made_up(energies):
        return None
    return electric["level"]


def overlaps(trip: Trip, start: datetime, end: datetime) -> bool:
    return trip.start_at <= end + BOUNDARY_TOLERANCE and (trip.end_at or trip.start_at) >= start - BOUNDARY_TOLERANCE


class MergedTrips:
    @staticmethod
    def get(car: Car, psacc_trips: List[Trip], psa_trips: Optional[list]) -> Trips:
        """The merged trips of car, oldest first; psacc's alone when psa's aren't available."""
        psa_trips = sorted((t for t in psa_trips or [] if parse_psa_date(t.get("startedAt", None))),
                           key=lambda t: t["startedAt"])
        merged = []
        if psa_trips:
            window_start = parse_psa_date(psa_trips[0]["startedAt"]) - timedelta(days=1)
            window_end = (parse_psa_date(psa_trips[-1].get("stoppedAt", None))
                          or parse_psa_date(psa_trips[-1]["startedAt"])) + timedelta(days=1)
            positions = Database.get_positions(car.vin, window_start, window_end)
            readings = Readings(
                [Reading(r["date"], r["level"], "car")
                 for r in Database.get_battery_readings(car.vin, window_start, window_end)],
                [Reading(r["Timestamp"], r["level"], "status") for r in positions if r["level"] is not None])
            previous_stop = None
            for i, psa_trip in enumerate(psa_trips):
                next_start = parse_psa_date(psa_trips[i + 1]["startedAt"]) if i + 1 < len(psa_trips) else None
                trip = MergedTrips.from_psa(car, psa_trip, positions, readings, previous_stop, next_start)
                previous_stop = trip.end_at
                merged.append(trip)
        for trip in psacc_trips:
            if not any(overlaps(trip, other.start_at, other.end_at) for other in merged if other.source == "psa"):
                merged.append(trip)
        trips = Trips()
        for trip in sorted(merged, key=lambda t: t.start_at):
            trip.id = trips.trip_num
            trips.trip_num += 1
            trips.append(trip)
        return trips

    @staticmethod
    def from_psa(car: Car, psa_trip: dict, positions, readings: Readings,  # pylint: disable=too-many-arguments
                 previous_stop: Optional[datetime], next_start: Optional[datetime]) -> Trip:
        # pylint: disable=too-many-positional-arguments
        trip = Trip()
        trip.car = car
        trip.source = "psa"
        trip.start_at = parse_psa_date(psa_trip["startedAt"])
        # psa's duration is in seconds, a trip's in hours
        duration_s = psa_trip.get("duration", None)
        stopped_at = parse_psa_date(psa_trip.get("stoppedAt", None))
        if duration_s is None and stopped_at is not None:
            duration_s = (stopped_at - trip.start_at).total_seconds()
        trip.duration = (duration_s or 0) / 3600
        trip.end_at = stopped_at or trip.start_at + timedelta(seconds=duration_s or 0)
        trip.distance = psa_trip.get("distance", None) or 0
        start_mileage = psa_trip.get("startMileage", None)
        trip.mileage = start_mileage + trip.distance if start_mileage is not None else None
        avg_speed = ((psa_trip.get("kinetic", None) or {}).get("avgSpeed", None) or 0) * MS_TO_KMH
        trip.speed_average = avg_speed or Trips.get_speed_average(trip.distance, trip.duration)
        if trip.speed_average >= MAX_SPEED:
            trip.speed_average = None
        MergedTrips._set_fuel(trip, psa_trip)
        MergedTrips._set_battery(trip, psa_trip, readings, previous_stop, next_start)
        MergedTrips._set_positions(trip, psa_trip, positions)
        return trip

    @staticmethod
    def _set_fuel(trip: Trip, psa_trip: dict):
        start_fuel = psa_energy(psa_trip.get("startEnergies", None), "Fuel")
        end_fuel = psa_energy(psa_trip.get("endEnergies", None), "Fuel")
        trip.start_level_fuel = start_fuel.get("level", None) if start_fuel else None
        trip.end_level_fuel = end_fuel.get("level", None) if end_fuel else None
        consumption = psa_energy(psa_trip.get("energyConsumptions", None), "Fuel")
        # psa reports fuel in centilitres: checked against the car's trip computer
        if consumption is not None and consumption.get("consumption", None) is not None:
            trip.consumption_fuel = consumption["consumption"] / CL_PER_LITRE
            trip.consumption_fuel_km = (consumption.get("avgConsumption", None) or 0) / CL_PER_LITRE
            if trip.consumption_fuel_km > trip.car.max_fuel_consumption:
                trip.consumption_fuel, trip.consumption_fuel_km = None, None
        else:
            trip.consumption_fuel, trip.consumption_fuel_km = None, None

    @staticmethod
    def _set_battery(trip: Trip, psa_trip: dict, readings: Readings,
                     previous_stop: Optional[datetime], next_start: Optional[datetime]):
        trip.consumption, trip.consumption_km = None, None
        if not trip.car.has_battery():
            return
        # The status api reads the same made-up level as psa's trip: when psa's is, only the car's
        # own reading can be trusted (status rows recorded before that level was filtered say 100%).
        start = readings.last_before(trip.start_at, previous_stop,
                                     car_only=psa_level_made_up(psa_trip.get("startEnergies", None)))
        end_limit = trip.end_at + MAX_END_READING_DELAY
        if next_start is not None:
            end_limit = min(end_limit, next_start)
        end = readings.first_after(trip.end_at, end_limit,
                                   car_only=psa_level_made_up(psa_trip.get("endEnergies", None)))
        if start is not None:
            trip.start_level, trip.start_level_source = start.level, start.source
        else:
            trip.start_level = psa_electric_level(psa_trip.get("startEnergies", None))
            trip.start_level_source = "psa" if trip.start_level is not None else None
        if end is not None:
            trip.end_level, trip.end_level_source = end.level, end.source
        else:
            # psa usually copies its start level into its end one, only a different value is a reading
            end_level = psa_electric_level(psa_trip.get("endEnergies", None))
            if end_level is not None and end_level != psa_electric_level(psa_trip.get("startEnergies", None)):
                trip.end_level, trip.end_level_source = end_level, "psa"
        if trip.start_level is None or trip.end_level is None:
            return
        if trip.end_level > trip.start_level + MAX_LEVEL_GAIN:
            logger.debug("trip of %s: battery rose from %s to %s, end level dropped", trip.start_at,
                         trip.start_level, trip.end_level)
            trip.end_level, trip.end_level_source = None, None
            return
        if trip.distance > 0:
            trip.set_consumption(trip.start_level - trip.end_level)
            if trip.consumption_km > trip.car.max_elec_consumption:
                trip.consumption, trip.consumption_km = None, None

    @staticmethod
    def _set_positions(trip: Trip, psa_trip: dict, positions):
        during = [p for p in positions
                  if trip.start_at - BOUNDARY_TOLERANCE <= p["Timestamp"] <= trip.end_at + BOUNDARY_TOLERANCE]
        located = [p for p in during if p["latitude"] is not None and p["longitude"] is not None]
        if len(located) >= 2:
            for position in located:
                trip.add_points(position["latitude"], position["longitude"])
            trip.set_altitude_diff(located[0]["altitude"], located[-1]["altitude"])
        else:
            # only the two ends: a straight line, when the car reported them
            for key in ("startPosition", "stopPosition"):
                coordinates = ((psa_trip.get(key, None) or {}).get("geometry", None) or {}).get("coordinates", None)
                if coordinates and len(coordinates) >= 2:
                    trip.add_points(coordinates[1], coordinates[0])
        for position in during:
            if position["temperature"] is not None:
                trip.add_temperature(position["temperature"])


def get_merged_trips(psa_client, car: Car) -> Trips:
    """The merged trips of car, fetching psa's trips (cached) and psacc's."""
    psacc_trips = Trips.get_trips(Cars([car])).get(car.vin, Trips())
    return MergedTrips.get(car, psacc_trips, psa_client.get_psa_trips(car.vin))
