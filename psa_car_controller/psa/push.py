"""Push notifications from psa, through the monitors of its api.

Psa can watch a data of the car (the charge status, the doors, a new trip...) and post an event to
a webhook when it changes: this is what the official app subscribes to for its own notifications.
It avoids polling entirely, which is the only way psacc has to notice something otherwise.

A monitor is created under a callback, which holds the webhook and its authentication, and both
belong to the psa account: the ones psacc creates are labelled so they can be told apart from the
ones of the official app, which are never touched.
"""
import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Everything psacc creates on the psa account carries this prefix, so it can be listed and removed
# without ever touching what the official app registered.
LABEL_PREFIX = "psacc_"

DEFAULT_STATE_FILE = "psa_push.json"

# What is worth being pushed, out of the data psa accepts as a trigger. Found against the api:
# the documented "OnChange" is refused, the operator is "onChange"; vehicle.trip and the
# maintenance counters aren't supported at all; the numeric data (level, odometer, speed,
# temperature) need a comparison and a value instead, see THRESHOLD_OP.
CHANGE_OP = "onChange"

# psa accepts at most 5 triggers per monitor, hence the groups.
MAX_TRIGGERS = 5

MONITOR_GROUPS = [
    ("events", [
        ("chargingStatus", "vehicle.energy.charging.status"),
        ("chargingPlugged", "vehicle.energy.charging.plugged"),
        ("doorsLocked", "vehicle.doorsState.lockedState"),
        ("moving", "vehicle.kinetic.moving"),
        ("engineRunning", "vehicle.engines.running"),
    ]),
    ("alerts", [
        ("doorsOpening", "vehicle.doorsState.opening"),
        ("alert", "vehicle.alert"),
    ]),
]

# Data attached to each event, so the app doesn't have to ask for the status right after.
EXTENDED_EVENT_PARAM = ["vehicle.status", "vehicle.position"]


class PushState:
    """The webhook token and the monitors psacc created, kept between restarts."""

    def __init__(self, file_name=DEFAULT_STATE_FILE):
        self.file_name = file_name
        self.token = None
        self.target = None
        self.callback_id = None
        self.monitors = {}

    def load(self):
        if not os.path.isfile(self.file_name):
            return self
        try:
            with open(self.file_name, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.token = data.get("token", None)
            self.target = data.get("target", None)
            self.callback_id = data.get("callback_id", None)
            self.monitors = data.get("monitors", {})
        except (ValueError, OSError):
            logger.exception("can't read %s", self.file_name)
        return self

    def save(self):
        try:
            with open(self.file_name, "w", encoding="utf-8") as file:
                json.dump({"token": self.token, "target": self.target,
                           "callback_id": self.callback_id, "monitors": self.monitors},
                          file, indent=4)
        except OSError:
            logger.exception("can't write %s", self.file_name)

    def ensure_token(self):
        if not self.token:
            self.token = secrets.token_urlsafe(24)
            self.save()
        return self.token

    def to_dict(self):
        return {"enabled": bool(self.monitors), "target": self.target,
                "callback_id": self.callback_id, "monitors": self.monitors,
                "token_set": bool(self.token)}


def webhook_url(base_url, token):
    """Where psa posts the events. The token is in the path so no header survives a reverse proxy."""
    return base_url.rstrip("/") + "/psa/webhook/" + token


def build_callback(label, target_url):
    """The body which creates our own callback, holding the webhook psa posts to.

    Found against the api: the callback goes at the top level of the body, `/user/callbacks`
    answering "invalid parameter: callback" for anything else.
    """
    return {
        "label": LABEL_PREFIX + label,
        "callback": {"webhook": {"name": LABEL_PREFIX + label, "target": target_url}},
    }


def build_monitor(label, triggers, locale="en"):
    """The body of a monitor: what to watch, psa posting it to the webhook of its callback."""
    if not 0 < len(triggers) <= MAX_TRIGGERS:
        raise ValueError("a monitor takes 1 to {} triggers, got {}".format(MAX_TRIGGERS, len(triggers)))
    return {
        "label": LABEL_PREFIX + label,
        "locale": locale,
        "triggerParam": {
            "triggers": [{"name": name, "data": {"data": data, "op": CHANGE_OP}} for name, data in triggers],
            # psa's expression parser refuses "||" and takes "or".
            "boolExp": " or ".join(name for name, _ in triggers),
        },
        "extendedEventParam": list(EXTENDED_EVENT_PARAM),
    }


def build_threshold_monitor(label, name, data, op, value, locale="en"):
    """A monitor on a numeric data, which needs a comparison rather than onChange."""
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    return {
        "label": LABEL_PREFIX + label,
        "locale": locale,
        "triggerParam": {
            "triggers": [{"name": name, "data": {"data": data, "op": op, "value": [str(value)]}}],
            "boolExp": name,
        },
        "extendedEventParam": list(EXTENDED_EVENT_PARAM),
    }


def monitors_path(vehicle_id, callback_id):
    """Where a monitor lives: under its callback, not under the vehicle as the documentation says."""
    return "/user/vehicles/{}/callbacks/{}/monitors".format(vehicle_id, callback_id)


def is_ours(item):
    """True for a callback or a monitor psacc created."""
    label = item.get("label", None)
    if label is None:
        label = ((item.get("monitor", None) or {}).get("label", None))
    return bool(label) and str(label).startswith(LABEL_PREFIX)
