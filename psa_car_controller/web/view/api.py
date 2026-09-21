import logging

from flask import jsonify, request, Response as FlaskResponse
from pydantic import BaseModel

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.remote_events import CommandResult
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.repository.db import Database
from psa_car_controller.web.app import app

from psa_car_controller.psacc.model.car import Cars
from psa_car_controller.psacc.repository.trips import Trips

from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psa.push import (MONITOR_GROUPS, PushState, build_callback, build_monitor,
                                         monitors_path, webhook_url)

import hmac
import json
from time import time

from psa_car_controller.web.tools.utils import convert_to_number_if_number_else_return_str

logger = logging.getLogger(__name__)

STYLE_CACHE = None
APP = PSACarController()
COMMAND_MAX_WAIT = 30


def json_response(json: str, status=200):
    return app.response_class(
        response=json,
        status=status,
        mimetype='application/json'
    )


def command_response(result: CommandResult):
    """Answer a remote command.

    The car answers asynchronously on mqtt, so the answer is pending unless the caller asks to
    wait for it with ?wait=<seconds>. The correlation id can be polled later on /command/<id>.
    """
    if result is None:
        return jsonify({"status": "failed",
                        "message": "command not sent, check the logs"}), 503
    wait = request.args.get('wait', None)
    if wait is not None:
        try:
            timeout = min(float(wait), COMMAND_MAX_WAIT)
        except ValueError:
            timeout = 0
        if timeout > 0:
            result.wait(timeout)
    return jsonify(result.to_dict())


@app.route('/get_vehicles')
def get_vehicules():
    response = app.response_class(
        response=json.dumps(APP.myp.get_vehicles(), default=lambda car: car.to_dict()),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route('/get_vehicleinfo/<string:vin>')
def get_vehicle_info(vin):
    from_cache = int(request.args.get('from_cache', 0)) == 1
    response = app.response_class(
        response=json.dumps(APP.myp.get_vehicle_info(vin, from_cache).to_dict(), default=str),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route("/style.json")
def get_style():
    global STYLE_CACHE
    if not STYLE_CACHE:
        with open(app.root_path + "/assets/style.json", "r", encoding="utf-8") as f:
            res = json.loads(f.read())
            STYLE_CACHE = res
    url_root = request.url_root
    STYLE_CACHE["sprite"] = url_root + "assets/sprites/osm-liberty"
    return jsonify(STYLE_CACHE)


@app.route('/charge_now/<string:vin>/<int:charge>')
def charge_now(vin, charge):
    return command_response(APP.myp.remote_client.charge_now(vin, charge != 0))


@app.route('/charge_hour')
def change_charge_hour():
    return command_response(APP.myp.remote_client.change_charge_hour(request.args['vin'],
                                                                     request.args['hour'],
                                                                     request.args['minute']))


@app.route('/wakeup/<string:vin>')
def wakeup(vin):
    try:
        return command_response(APP.myp.remote_client.wakeup(vin))
    except RateLimitException:
        return jsonify({"error": "Wakeup rate limit exceeded"})


@app.route('/preconditioning/<string:vin>/<int:activate>')
def preconditioning(vin, activate):
    return command_response(APP.myp.remote_client.preconditioning(vin, activate))


@app.route('/position/<string:vin>')
def get_position(vin):
    """The car position, from the dedicated psa endpoint, falling back to the vehicle status.

    The lastPosition of the status can stay frozen for days while the dedicated endpoint keeps
    answering, so it is asked first; "source" tells which one answered.
    """
    coordinates = None
    source = "lastPosition"
    updated_at = None
    position = APP.myp.get_last_position(vin)
    if position is not None:
        coordinates = (position.get("geometry", None) or {}).get("coordinates", None)
        updated_at = (position.get("properties", None) or {}).get("createdAt", None)
    if not coordinates:
        source = "status"
        res = APP.myp.get_vehicle_info(vin)
        try:
            coordinates = res.last_position.geometry.coordinates
            updated_at = str(res.last_position.properties.updated_at)
        except AttributeError:
            return jsonify({'error': 'last_position not available from api'})
    longitude, latitude = coordinates[:2]
    if len(coordinates) == 3:  # altitude is not always available
        altitude = coordinates[2]
    else:
        altitude = None
    return jsonify(
        {"longitude": longitude, "latitude": latitude, "altitude": altitude,
         "updated_at": updated_at, "source": source,
         "url": f"https://maps.google.com/maps?q={latitude},{longitude}"})


@app.route('/vehicles/<string:vin>/psa_trips')
def get_psa_trips(vin):
    """The trips recorded by psa itself (see /vehicles/trips for the ones psacc rebuilds)."""
    trips = APP.myp.get_psa_trips(vin)
    if trips is None:
        return jsonify({"error": "trips not available from api"}), 404
    return jsonify(trips)


@app.route('/vehicles/<string:vin>/pictures')
def get_pictures(vin):
    """The distinct pictures of the car as urls served by this daemon, not by psa."""
    pictures = APP.myp.get_pictures(vin)
    if pictures is None:
        return jsonify({"error": "pictures not available from api"}), 404
    return jsonify({"vin": vin, "count": len(pictures),
                    "pictures": ["vehicles/{}/picture/{}".format(vin, i) for i in range(len(pictures))]})


@app.route('/vehicles/<string:vin>/picture/<int:index>')
def get_picture(vin, index):
    """One car picture, proxied from psa's public render host and cached."""
    pictures = APP.myp.get_pictures(vin)
    if pictures is None or not 0 <= index < len(pictures):
        return jsonify({"error": "no such picture"}), 404
    picture = pictures[index]
    return app.response_class(
        response=picture["content"],
        status=200,
        mimetype=picture["content_type"],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route('/vehicles/<string:vin>/maintenance')
def get_maintenance(vin):
    """Distance and days before the next service."""
    maintenance = APP.myp.get_maintenance(vin)
    if maintenance is None:
        return jsonify({"error": "maintenance not available from api"}), 404
    return jsonify(maintenance)


# Set a battery threshold and schedule an hour to stop the charge
@app.route('/charge_control')
def get_charge_control():
    logger.info(request)
    vin = request.args['vin']
    if APP.chc:
        charge_control = APP.chc.get(vin)
        if charge_control is None:
            return jsonify({"error": "VIN not in list"})
        if 'hour' in request.args and 'minute' in request.args:
            charge_control.set_stop_hour([int(request.args["hour"]), int(request.args["minute"])])
        if 'percentage' in request.args:
            charge_control.percentage_threshold = int(request.args['percentage'])
        APP.chc.save_config()
        return jsonify(charge_control.get_dict())
    error = "Charge control not setup check your PSACC configuration and logs"
    logger.error(error)
    return jsonify({"error": error})


@app.route('/positions')
def get_recorded_position():
    return FlaskResponse(Database.get_recorded_position(), mimetype='application/json')


@app.route('/abrp')
def abrp():
    vin = request.args.get('vin', None)
    enable = request.args.get('enable', None)
    token = request.args.get('token', None)
    if vin is not None and enable is not None:
        if enable == '1':
            APP.myp.abrp.abrp_enable_vin.add(vin)
        else:
            APP.myp.abrp.abrp_enable_vin.discard(vin)
    if token is not None:
        APP.myp.abrp.token = token
    return jsonify(dict(APP.myp.abrp))


@app.after_request
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    return response


@app.route('/horn/<string:vin>/<int:count>')
def horn(vin, count):
    try:
        return command_response(APP.myp.remote_client.horn(vin, count))
    except RateLimitException:
        return jsonify({"error": "Horn rate limit exceeded"})


@app.route('/lights/<string:vin>/<int:duration>')
def lights(vin, duration):
    try:
        return command_response(APP.myp.remote_client.lights(vin, duration))
    except RateLimitException:
        return jsonify({"error": "Lights rate limit exceeded"})


@app.route('/lock_door/<string:vin>/<int:lock>')
def lock_door(vin, lock):
    try:
        return command_response(APP.myp.remote_client.lock_door(vin, lock))
    except RateLimitException:
        return jsonify({"error": "Locks rate limit exceeded"})


@app.route('/command/<string:correlation_id>')
def get_command_result(correlation_id):
    result = APP.myp.remote_client.get_command_result(correlation_id)
    if result is None:
        return jsonify({"error": "unknown correlation id"}), 404
    return command_response(result)


@app.route('/commands')
def get_commands():
    vin = request.args.get('vin', None)
    return jsonify(APP.myp.remote_client.command_registry.get_all(vin))


@app.route('/events')
def get_events():
    """Server sent events stream of the mqtt vehicle events and of the command results."""
    return FlaskResponse(APP.myp.remote_client.event_broker.stream(),
                         mimetype='text/event-stream',
                         headers={"Cache-Control": "no-cache",
                                  "Connection": "keep-alive",
                                  "X-Accel-Buffering": "no"})


@app.route('/psa/probe')
def psa_probe():
    """Ask the psa api which of its documented endpoints answer for this account and car.

    Diagnostic only, everything it calls is a GET: see docs/api/*.md, where most endpoints are
    marked as out of the first release scope, and a car only answers for the services it is
    subscribed to.
    """
    try:
        return jsonify(APP.myp.probe_api(request.args.get('vin', None),
                                         name=request.args.get('endpoint', None),
                                         preview_len=request.args.get('preview', None),
                                         accept=request.args.get('accept', None),
                                         path=request.args.get('path', None),
                                         trip_id=request.args.get('trip', None)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


PUSH_STATE = PushState().load()
# A monitor event triggers a status refresh, but not more often than this: psa can send a burst.
WEBHOOK_REFRESH_PERIOD = 60
LAST_WEBHOOK_REFRESH = [0.0]


# The bta backend needs a token from the account password (obtained once, then kept in memory);
# the password itself is never stored.
_MYM = {"client": None}


@app.route('/psa/bta/fetch', methods=['POST', 'GET'])
def psa_bta_fetch():
    """Read a bta resource from the mym backend.

    Needs the psa account password once (?password= or a json body {"password": ...}) to obtain the
    mym token, which is kept in memory and reused; the password isn't stored. Defaults to the safe
    lastposition; ?path= and an optional json "body" allow finding the exact read shape of the trips
    without another build. Everything it POSTs carries the client cert and the token.
    """
    payload = request.get_json(silent=True) or {}
    password = request.args.get('password', None) or payload.get('password', None)
    email = request.args.get('email', None) or payload.get('email', None)
    vin = request.args.get('vin', None) or payload.get('vin', None)
    path = request.args.get('path', None) or payload.get('path', None) or "lastposition"
    body = payload.get('body', None)

    car = APP.myp.vehicles_list.get_car_by_vin(vin) if vin else next(iter(APP.myp.vehicles_list), None)
    if car is None:
        return jsonify({"error": "no vehicle"}), 404

    client = _MYM["client"]
    if client is None or client.token is None:
        if not password:
            return jsonify({"error": "password is required once to obtain the mym token "
                            "(?password=, and ?email= if it differs from the account)"}), 400
        brand_code = request.args.get('brand', None) or payload.get('brand', None)
        country = request.args.get('country', None) or payload.get('country', None)
        client = APP.myp.mym_client(brand_code, country)
        try:
            client.get_token(email or APP.myp.account_email(), password)
        except Exception as e:  # pylint: disable=broad-except
            return jsonify({"error": "couldn't obtain the mym token: " + str(e)}), 502
        _MYM["client"] = client

    answer, status = client.post_bta(car.vin, path, body)
    return jsonify({"path": path, "answer": answer, "status": status}), 200


@app.route('/psa/bta/probe')
def psa_bta_probe():
    """Check, read only, whether the bta trips (the ones the car logs and the official app uploads
    to the mym backend) are reachable with the credentials psacc already has.

    Answers the http status and a short preview per endpoint and header variant. It only reads, and
    it does not try to obtain a different token: a 401/403 means that backend wants its own auth.
    """
    try:
        return jsonify(APP.myp.probe_bta(request.args.get('vin', None)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route('/psa/call', methods=['GET', 'POST', 'DELETE'])
def psa_call():
    """Call a path of the psa api directly (diagnostic and setup of the monitors).

    GET is read only; POST and DELETE change the psa account, which is what creating a monitor
    means, so they are only reachable by whoever already reaches this api.
    """
    path = request.args.get('path', None)
    if not path:
        return jsonify({"error": "path is required"}), 400
    body = request.get_json(silent=True) if request.method == 'POST' else None
    try:
        answer, status = APP.myp.call_api(request.method, path, body,
                                          accept=request.args.get('accept', "application/hal+json"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(answer), status


@app.route('/psa/push')
def psa_push_state():
    """What psacc registered on the psa account for push, and the webhook it listens on."""
    return jsonify(PUSH_STATE.to_dict())


@app.route('/psa/push/enable', methods=['GET', 'POST'])
def psa_push_enable():
    """Ask psa to post an event to this psacc when the car changes.

    Creates our own callback, holding the webhook, then a monitor per group of data to watch.
    `base_url` is the public url of this psacc: the webhook is /psa/webhook/<token>, which the
    reverse proxy must let through without authentication, the token being what authenticates psa.

    Calling it again reuses the callback and adds only the monitors which are missing.
    """
    base_url = request.args.get('base_url', None)
    if not base_url:
        return jsonify({"error": "base_url is required, e.g. ?base_url=https://psacc.example.com"}), 400
    vin = request.args.get('vin', None)
    car = APP.myp.vehicles_list.get_car_by_vin(vin) if vin else next(iter(APP.myp.vehicles_list), None)
    if car is None:
        return jsonify({"error": "no vehicle"}), 404

    target = webhook_url(base_url, PUSH_STATE.ensure_token())
    steps = []
    if not PUSH_STATE.callback_id:
        answer, status = APP.myp.call_api('POST', "/user/callbacks", build_callback("events", target))
        steps.append({"step": "callback", "status": status, "answer": answer})
        if not 200 <= status < 300:
            return jsonify({"steps": steps}), status
        PUSH_STATE.callback_id = answer.get("callbackId", None)
        PUSH_STATE.target = target
        PUSH_STATE.save()

    existing = {m["label"] for m in PUSH_STATE.monitors.values()}
    for label, triggers in MONITOR_GROUPS:
        monitor = build_monitor(label, triggers)
        if monitor["label"] in existing:
            continue
        path = monitors_path(car.vehicle_id, PUSH_STATE.callback_id)
        answer, status = APP.myp.call_api('POST', path, monitor)
        steps.append({"step": "monitor", "label": monitor["label"], "status": status, "answer": answer})
        if 200 <= status < 300:
            PUSH_STATE.monitors[monitor_id_of(answer)] = {"vin": car.vin, "path": path,
                                                          "label": monitor["label"]}
            PUSH_STATE.save()
    return jsonify({"state": PUSH_STATE.to_dict(), "steps": steps})


def monitor_id_of(answer):
    """The id of a created monitor, psa answering it in the href of its _links."""
    href = ((answer.get("_links", None) or {}).get("monitor", None) or {}).get("href", "")
    return href.rstrip("/").rsplit("/", 1)[-1] or str(len(PUSH_STATE.monitors))


@app.route('/psa/push/disable', methods=['GET', 'POST', 'DELETE'])
def psa_push_disable():
    """Remove the monitors and the callback psacc created, and only those."""
    removed = {}
    for monitor_id, monitor in list(PUSH_STATE.monitors.items()):
        answer, status = APP.myp.call_api('DELETE', monitor["path"] + "/" + monitor_id)
        removed[monitor_id] = {"status": status, "answer": answer}
        if 200 <= status < 300 or status == 404:
            PUSH_STATE.monitors.pop(monitor_id, None)
    if not PUSH_STATE.monitors and PUSH_STATE.callback_id:
        answer, status = APP.myp.call_api('DELETE', "/user/callbacks/" + PUSH_STATE.callback_id)
        removed["callback"] = {"status": status, "answer": answer}
        if 200 <= status < 300 or status == 404:
            PUSH_STATE.callback_id = None
            PUSH_STATE.target = None
    PUSH_STATE.save()
    return jsonify({"removed": removed, "state": PUSH_STATE.to_dict()})


@app.route('/psa/webhook/<string:token>', methods=['POST', 'GET'])
def psa_webhook(token):
    """Where psa posts its monitor events.

    Answers 204 to anything authenticated, psa retrying whatever it can't deliver. The event is
    broadcast on /events so a client sees it immediately, and refreshes the vehicle status, at
    most once a minute.
    """
    if not PUSH_STATE.token or not hmac.compare_digest(token, PUSH_STATE.token):
        return jsonify({"error": "unknown token"}), 404
    event = request.get_json(silent=True) or {}
    logger.info("psa webhook: %s", json.dumps(event)[:1000])
    APP.myp.remote_client.event_broker.publish("psa_monitor", event)
    vin = event.get("vin", None) or (event.get("vehicle", None) or {}).get("vin", None)
    now = time()
    if vin and now - LAST_WEBHOOK_REFRESH[0] > WEBHOOK_REFRESH_PERIOD:
        LAST_WEBHOOK_REFRESH[0] = now
        try:
            APP.myp.get_vehicle_info(vin)
        except Exception:  # pylint: disable=broad-except
            logger.exception("psa_webhook refresh:")
    return "", 204


@app.route('/settings/<string:section>')
def settings_section(section: str):
    config_section: BaseModel = getattr(APP.config, section.capitalize())
    for key, value in request.args.items():
        typed_value = convert_to_number_if_number_else_return_str(value)
        setattr(config_section, key, typed_value)
        APP.config.write_config()
    return json_response(config_section.json())


@app.route('/vehicles/trips')
def get_trips():
    try:
        if not APP.myp.vehicles_list:
            return jsonify({})
        car = APP.myp.vehicles_list[0]
        trips_by_vin = Trips.get_trips(Cars([car]))
        trips = trips_by_vin[car.vin]
        trips_as_dict = trips.get_trips_as_dict()
        return jsonify(trips_as_dict)
    except (IndexError, TypeError, KeyError):
        logger.debug("Failed to get trips, there is probably not enough data yet:", exc_info=True)
        return jsonify([])


@app.route('/vehicles/chargings')
def get_chargings():
    try:
        chargings = Charging.get_chargings()
        return jsonify(chargings)
    except (IndexError, TypeError):
        logger.debug("Failed to get chargings, there is probably not enough data yet:", exc_info=True)
        return jsonify([])


@app.route('/settings')
def settings():
    return json_response(APP.config.json())


@app.route('/battery/soh/<string:vin>')
def db(vin: str):
    soh = Database.get_last_soh_by_vin(vin)
    return jsonify({"soh": soh})
