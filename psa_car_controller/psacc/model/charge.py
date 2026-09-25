from datetime import datetime
from enum import Enum


class ChargingMode(Enum):
    # values are the api's chargingMode
    AC = "Slow"
    DC = "Quick"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):
        # older rows and tests use lower case, "fast" was an old name of "Quick"
        if isinstance(value, str):
            value = value.lower()
            if value == "slow":
                return ChargingMode.AC
            if value in ("quick", "fast"):
                return ChargingMode.DC
        return ChargingMode.UNKNOWN

    @staticmethod
    def is_known(value) -> bool:
        return ChargingMode(value) != ChargingMode.UNKNOWN


class ChargePlace(Enum):
    HOME = "home"
    WORK = "work"
    PUBLIC = "public"


class Charge:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    def __init__(self, start_at: datetime, stop_at: datetime = None, vin=None, start_level=None, end_level=None,
                 co2=None, kw=None, price=None, charging_mode=None, mileage=None, place=None, metered_kw=None,
                 price_manual=None):
        if not isinstance(start_at, datetime):
            raise TypeError(f"start_at must be a datetime object, got {type(start_at)}")
        self.charging_mode: ChargingMode = ChargingMode(charging_mode)
        self.start_at = start_at
        self.stop_at = stop_at
        self.vin = vin
        self.start_level = start_level
        self.end_level = end_level
        self.co2 = co2
        self.kw = kw
        self.price = price
        self.mileage = mileage
        # where the car charged, set by hand: a missing place is home
        self.place: ChargePlace = ChargePlace(place) if place else ChargePlace.HOME
        # the kWh the charger billed, set by hand, kw stays the estimate from the battery levels
        self.metered_kw = metered_kw
        # the price was set by hand, it is never estimated again
        self.price_manual = bool(price_manual)
