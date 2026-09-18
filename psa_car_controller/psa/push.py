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

# What is worth being pushed, out of the data psa allows as a trigger (see docs/api/DataTrigger.md).
DEFAULT_TRIGGERS = [
    ("charging_status", "vehicle.energy.charging.status", "OnChange"),
    ("charging_plugged", "vehicle.energy.charging.plugged", "OnChange"),
    ("doors_locked", "vehicle.doorsState.lockedState", "OnChange"),
    ("moving", "vehicle.kinetic.moving", "OnChange"),
    ("trip", "vehicle.trip", "OnChange"),
]

# Data attached to each event, so the app doesn't have to ask for the status right after.
EXTENDED_EVENT_PARAM = ["vehicle.status", "vehicle.position"]


class PushState:
    """The webhook token and the monitors psacc created, kept between restarts."""

    def __init__(self, file_name=DEFAULT_STATE_FILE):
        self.file_name = file_name
        self.token = None
        self.target = None
        self.monitors = {}

    def load(self):
        if not os.path.isfile(self.file_name):
            return self
        try:
            with open(self.file_name, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.token = data.get("token", None)
            self.target = data.get("target", None)
            self.monitors = data.get("monitors", {})
        except (ValueError, OSError):
            logger.exception("can't read %s", self.file_name)
        return self

    def save(self):
        try:
            with open(self.file_name, "w", encoding="utf-8") as file:
                json.dump({"token": self.token, "target": self.target, "monitors": self.monitors},
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
                "monitors": self.monitors, "token_set": bool(self.token)}


def webhook_url(base_url, token):
    """Where psa posts the events. The token is in the path so no header survives a reverse proxy."""
    return base_url.rstrip("/") + "/psa/webhook/" + token


def build_monitor(label, target_url, triggers=None, locale="en"):
    """The body of a monitor: what to watch, and where to post it when it changes."""
    triggers = triggers or DEFAULT_TRIGGERS
    return {
        "label": LABEL_PREFIX + label,
        "locale": locale,
        "subscribeParam": {
            "callback": {
                "name": LABEL_PREFIX + label,
                "target": target_url,
            },
            # A webhook which answers an error is retried a few times rather than dropped.
            "retryPolicy": {"policy": "Bounded", "maxRetryNumber": 3, "retryDelay": 60},
        },
        "triggerParam": {
            "triggers": [{"name": name, "data": {"data": data, "op": op}} for name, data, op in triggers],
            "boolExp": " || ".join(name for name, _, _ in triggers),
        },
        "extendedEventParam": list(EXTENDED_EVENT_PARAM),
    }


def is_ours(item):
    """True for a callback or a monitor psacc created."""
    label = item.get("label", None)
    if label is None:
        label = ((item.get("monitor", None) or {}).get("label", None))
    return bool(label) and str(label).startswith(LABEL_PREFIX)
