"""Keep track of remote command results and broadcast vehicle events.

PSA remote services are asynchronous: the http endpoints of psacc only queue a mqtt message,
the real answer (and the spontaneous vehicle events) come back later on mqtt.
This module stores those answers so they can be exposed, and broadcasts every event to the
subscribers of the /events stream.
"""
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from queue import Queue, Empty, Full

logger = logging.getLogger(__name__)

PENDING = "pending"
SUCCESS = "success"
FAILED = "failed"

MAX_STORED_RESULTS = 100
MAX_QUEUED_EVENTS = 100

# psa reasons which mean the command was refused: retrying or refreshing the token won't help
REFUSAL_REASONS = {
    "no.matching.service.key": "this service isn't available for this car",
    "service.not.available": "service not available",
    "not.allowed": "command not allowed for this car",
    "vehicle.not.eligible": "car isn't eligible for this service",
    "invalid.request": "the request was rejected as invalid",
}


def _now():
    return datetime.now(timezone.utc)


class CommandResult:
    def __init__(self, correlation_id, vin, action):
        self.correlation_id = correlation_id
        self.vin = vin
        self.action = action
        self.status = PENDING
        self.return_code = None
        self.reason = None
        self.message = "command sent, waiting for the car answer"
        self.sent_at = _now()
        self.updated_at = self.sent_at
        self._done = threading.Event()

    def set_result(self, return_code, reason=None):
        self.return_code = return_code
        self.reason = reason
        if return_code == "0":
            self.status = SUCCESS
            self.message = "command accepted by the car"
        else:
            self.status = FAILED
            self.message = describe_refusal(reason, return_code)
        self.updated_at = _now()
        self._done.set()

    def set_failed(self, message, reason=None, return_code=None):
        self.status = FAILED
        self.message = message
        if reason is not None:
            self.reason = reason
        if return_code is not None:
            self.return_code = return_code
        self.updated_at = _now()
        self._done.set()

    def wait(self, timeout):
        """Wait for the car answer, return True if it arrived before the timeout."""
        return self._done.wait(timeout)

    def to_dict(self):
        return {"correlation_id": self.correlation_id,
                "vin": self.vin,
                "action": self.action,
                "status": self.status,
                "return_code": self.return_code,
                "reason": self.reason,
                "message": self.message,
                "sent_at": self.sent_at.isoformat(),
                "updated_at": self.updated_at.isoformat()}


def describe_refusal(reason, return_code=None):
    if reason in REFUSAL_REASONS:
        return "PSA refused: " + REFUSAL_REASONS[reason]
    if reason:
        return "PSA refused: " + str(reason)
    return "PSA refused the command (return code {})".format(return_code)


def is_refusal(reason) -> bool:
    """True when the reason means the command was refused, so resending it is pointless."""
    return reason in REFUSAL_REASONS


class CommandRegistry:
    """Store the last results, keyed by correlation id."""

    def __init__(self, max_size=MAX_STORED_RESULTS):
        self._results = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def register(self, correlation_id, vin, action) -> CommandResult:
        result = CommandResult(correlation_id, vin, action)
        with self._lock:
            self._results[correlation_id] = result
            while len(self._results) > self._max_size:
                self._results.popitem(last=False)
        return result

    def relink(self, correlation_id, result: CommandResult):
        """A resent request gets a new correlation id, keep pointing at the same result."""
        result.correlation_id = correlation_id
        with self._lock:
            self._results[correlation_id] = result

    def get(self, correlation_id) -> CommandResult:
        with self._lock:
            return self._results.get(correlation_id, None)

    def get_last_pending(self) -> CommandResult:
        with self._lock:
            for result in reversed(self._results.values()):
                if result.status == PENDING:
                    return result
        return None

    def get_all(self, vin=None):
        with self._lock:
            results = list(self._results.values())
        if vin is not None:
            results = [result for result in results if result.vin == vin]
        return [result.to_dict() for result in results]


class EventBroker:
    """Fan out events to the subscribers of the event stream."""

    def __init__(self):
        self._subscribers = []
        self._listeners = []
        self._lock = threading.Lock()
        self._last_events = OrderedDict()

    def add_listener(self, listener):
        """Call listener(event_type, data) on every published event, in the publishing thread."""
        with self._lock:
            self._listeners.append(listener)

    def subscribe(self) -> Queue:
        queue = Queue(maxsize=MAX_QUEUED_EVENTS)
        with self._lock:
            self._subscribers.append(queue)
        logger.debug("event subscriber added (%d)", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: Queue):
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
        logger.debug("event subscriber removed (%d)", len(self._subscribers))

    def publish(self, event_type: str, data: dict):
        event = {"type": event_type, "date": _now().isoformat(), "data": data}
        with self._lock:
            self._last_events[event_type] = event
            subscribers = list(self._subscribers)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event_type, data)
            except Exception:  # pylint: disable=broad-except
                logger.exception("event listener failed on %s", event_type)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except Full:
                logger.warning("event subscriber is too slow, dropping event %s", event_type)

    def get_last_events(self):
        with self._lock:
            return list(self._last_events.values())

    @staticmethod
    def format_sse(event: dict) -> str:
        return "event: {}\ndata: {}\n\n".format(event["type"], json.dumps(event, default=str))

    def stream(self, keepalive=30):
        """Yield server sent events, forever. Meant to be used as a flask response."""
        queue = self.subscribe()
        try:
            for event in self.get_last_events():
                yield self.format_sse(event)
            while True:
                try:
                    yield self.format_sse(queue.get(timeout=keepalive))
                except Empty:
                    yield ": keepalive\n\n"
        finally:
            self.unsubscribe(queue)
