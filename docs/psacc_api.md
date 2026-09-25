# PSACC API
These links will work only if PSACC is on your computer, if it isn't please replace localhost by the ip of PSACC server.

1. Get the car state
    
   http://localhost:5000/get_vehicleinfo/YOURVIN

2. Get the car state from cache to avoid to use PSA API too much

   http://localhost:5000/get_vehicleinfo/YOURVIN?from_cache=1

3. Stop charge

   http://localhost:5000/charge_now/YOURVIN/0

4. Set hour to stop the charge to 6 am

   http://localhost:5000/charge_control?vin=YOURVIN&hour=6&minute=0 

5. Change car charge threshold to 80%

   http://localhost:5000/charge_control?vin=YOURVIN&percentage=80 

6. See the dashboard (only if record is enabled)

   http://localhost:5000

7. Refresh car state (ask car to send its state):

   http://localhost:5000/wakeup/YOURVIN

8. Start (1)/Stop (0) preconditioning

   http://localhost:5000/preconditioning/YOURVIN/1 or 0

9. Change charge hour (for example: set it to 22h30)

   http://localhost:5000/charge_hour?vin=YOURVIN&hour=22&minute=30

10. Honk the horn

    http://localhost:5000/horn/YOURVIN/count

11. Flash the lights (Duration is always roughly 10 seconds, regardless of set duration)

    http://localhost:5000/lights/YOURVIN/duration

12. Lock (1)/Unlock (0) the doors
   
    http://localhost:5000/lock_door/YOURVIN/1 or 0

13. Get config

    http://localhost:5000/settings

14. Change config parameter in config.ini (you need to restart the app after)

    http://localhost:5000/settings/electricity_config?night_price=0.2

    http://localhost:5000/settings/general?currency=%C2%A3

15. Get battery SOH

    http://localhost:5000/battery/soh/YOURVIN

16. Get the vehicle charging sessions
   
   http://localhost:5000/vehicles/chargings

   Each session has a `place` (`home`, `work` or `public`, `home` unless set), the `metered_kw` the charger
   billed (null unless set) and `price_manual` (the price was set by hand). `kw` stays the estimate from the
   battery levels.

   Set by hand what psacc can't know about a finished session, e.g. the real bill of a public charger:

   ```
   curl -X PATCH http://localhost:5000/vehicles/YOURVIN/chargings -H 'Content-Type: application/json' \
        -d '{"start_at": "2026-09-25T08:32:30Z", "place": "public", "metered_kw": 8.67, "price": 10.26}'
   ```

   `start_at` names the session. A key left out is kept and a null clears it. A price set by hand is never
   estimated again; clear it to go back to the estimate, which uses `metered_kw` when set and is free at work.
   Answers the session as listed above, 404 for an unknown session and 409 for one still in progress.

17. Get the vehicle trips:
   
   http://localhost:5000/vehicles/trips

18. Get the result of a command

    Every command (charge_now, preconditioning, wakeup, horn, lights, lock_door, charge_hour) answers with the
    command it queued, not with what the car did:

    ```json
    {"correlation_id": "e4c5...1230", "vin": "YOURVIN", "action": "Doors", "status": "pending",
     "return_code": null, "reason": null, "message": "command sent, waiting for the car answer",
     "sent_at": "2026-09-17T20:54:39+00:00", "updated_at": "2026-09-17T20:54:39+00:00"}
    ```

    The car answers later on mqtt, so either poll the correlation id:

    http://localhost:5000/command/CORRELATION_ID

    or ask the command endpoint to wait for the answer (up to 30 seconds):

    http://localhost:5000/lock_door/YOURVIN/1?wait=10

    `status` is then `success` (`message`: "command accepted by the car") or `failed`, with `message` telling why,
    for example "PSA refused: this service isn't available for this car".

19. Get the last command results

    http://localhost:5000/commands or http://localhost:5000/commands?vin=YOURVIN

20. Follow the car events in real time (server sent events)

    http://localhost:5000/events

    The stream carries the mqtt events of the car (`vehicle`) and the answers to the commands
    (`command_result`), which avoids polling the car state:

    ```
    event: vehicle
    data: {"type": "vehicle", "date": "2026-09-17T20:54:39+00:00", "data": {"vin": "YOURVIN",
           "battery_level": 64, "autonomy": 22, "charging": false, "charging_rate": 0,
           "cable_plugged": true, "preconditioning": false, "raw": {...}}}
    ```

    `charging` is true only while the car reports a charging rate. The car also publishes a
    `remaining_time` and a `cable_detected`, both kept in `raw`: `remaining_time` was seen unchanged
    for hours on a car which was neither plugged nor charging, and `cable_detected` was seen at 1 on
    a car which wasn't plugged, so neither can be trusted yet. For the plug and the charge state, the
    api status (`/get_vehicleinfo/VIN`) stays the reference.
    ```

    Example with curl: `curl -N http://localhost:5000/events`

21. Probe the psa api (diagnostic)

    http://localhost:5000/psa/probe or http://localhost:5000/psa/probe?vin=YOURVIN

    Calls each read only endpoint of the psa api documented in `docs/api/` and answers with the
    http status, the duration, the keys and a short preview of each one, without changing anything.
    Most of those endpoints are marked "OUT OF 1ST RELEASE" in psa's own specification and a car
    only answers for the services it is subscribed to, so this tells which ones are really served
    for your account and your car (maintenance, alerts, last position, trips...).

    One endpoint at a time, with a longer preview (a hal answer starts with a page of `_links`
    which hides the payload) and another Accept header, some endpoints answering 406 to hal:

    http://localhost:5000/psa/probe?endpoint=trips&preview=20000

    http://localhost:5000/psa/probe?endpoint=lastPosition&accept=*/*

    Any read only path of the psa api, the `_links` of a vehicle advertising more than the
    documentation of `docs/api/` lists (remotes, callbacks, alarms...). `{id}` is the vehicle and
    `{tid}` the trip given by `?trip=`:

    http://localhost:5000/psa/probe?path=/user/vehicles/{id}/remotes

    http://localhost:5000/psa/probe?path=/user/vehicles/{id}/trips/{tid}/wayPoints&trip=TRIP_ID

22. Get the trips recorded by psa

    http://localhost:5000/vehicles/YOURVIN/psa_trips

    These are psa's own trips, with the energy levels at both ends, the consumptions and the
    average speed, and the start and stop positions when the car reports its position. They are
    not the ones of `/vehicles/trips`, which psacc rebuilds from the positions it polled.

23. Get the next service

    http://localhost:5000/vehicles/YOURVIN/maintenance

    Answers `mileageBeforeMaintenance` and `daysBeforeMaintenance`.

Note: `/position/YOURVIN` now asks the dedicated position endpoint of psa first and falls back to
the position of the vehicle status, which can stay frozen for days. The answer says which one it
used in `source` and when the position was taken in `updated_at`.

24. Let psa push the changes of the car (monitors)

    Psa can watch a data of the car and post an event to a webhook when it changes, which is what
    the official app subscribes to for its own notifications. This avoids polling.

    Enable it, `base_url` being the public url of this psacc:

    http://localhost:5000/psa/push/enable?base_url=https://psacc.example.com

    It creates our own callback, holding the webhook, then the monitors under it, labelled
    `psacc_...`, and asks psa to post the events to `/psa/webhook/<token>`. The token is generated
    once and kept in `psa_push.json`, with the callback and the monitors. Calling it again reuses
    the callback and only adds what is missing.

    Watched: the charge status and the plug, the doors (locked and opening), whether the car moves,
    whether the engine runs, and the alerts of the car. Each event carries the vehicle status and
    the last position.

    What psa accepts, found against its api rather than in `docs/api/`:

    - a monitor lives under a callback (`/user/vehicles/{id}/callbacks/{cbid}/monitors`), the
      documented `/user/vehicles/{id}/monitors` answers 404,
    - the operator is `onChange`, the documented `OnChange` is refused,
    - at most 5 triggers per monitor,
    - `boolExp` refuses `||` and takes ` or `,
    - `vehicle.trip` and the maintenance counters can't be watched; the numeric data (level,
      odometer, speed, temperature) need `lowerThan`/`greaterThan` and a value.

    **The reverse proxy must let `/psa/webhook/` through without authentication**, psa having no
    way to authenticate: the token in the path is what does it.

    The events are broadcast on `/events` as `psa_monitor`, and a webhook refreshes the vehicle
    status (at most once a minute).

    See what is registered, and remove what psacc created (never what the official app registered):

    http://localhost:5000/psa/push

    http://localhost:5000/psa/push/disable

25. Call the psa api directly

    http://localhost:5000/psa/call?path=/user/callbacks

    `GET` reads, `POST` (with a json body) and `DELETE` write: this is how the monitors and the
    callbacks are created and removed, their shape not being described correctly by
    `docs/api/`. It only accepts paths of the psa api.

26. Probe the bta backend (diagnostic)

    http://localhost:5000/psa/bta/probe

    The trips the car logs itself, with their gps track, are not in the connectedcar api this
    daemon uses: the official app reads them over bluetooth and uploads them to a separate "mym"
    backend (`contracts/bta`). This checks, read only, whether the credentials psacc already has
    are accepted there, answering the http status and a short preview per endpoint. A 401/403 means
    that backend wants its own authentication, which this does not try to obtain.

27. Get the pictures of the car

    http://localhost:5000/vehicles/YOURVIN/pictures

    Answers the distinct pictures of the car as urls served by this daemon:

    ```json
    {"vin": "YOURVIN", "count": 8,
     "pictures": ["vehicles/YOURVIN/picture/0", "vehicles/YOURVIN/picture/1", ...]}
    ```

    Each `http://localhost:5000/vehicles/YOURVIN/picture/<n>` streams one image. They are the public
    3d renders of the exact car (colour, trim and options), fetched from psa's render host,
    de-duplicated (several psa "views" repeat the same image) and cached, and proxied so a client
    only talks to this daemon.

28. Read the bta data (the trips the car logs itself)

    http://localhost:5000/psa/bta/fetch?password=YOURPASSWORD (POST or GET)

    The trips the car records with their gps track live on a separate "mym" backend that needs the
    client certificate the setup extracts (certs/*.pem) and its own token. The token is obtained
    once from the psa account password (the same GetAccessToken flow as the setup) and kept in
    memory; the password itself is not stored. Afterwards no password is needed until restart.

    Defaults to the (safe) last position. `?path=trips` and an optional json `body` allow reading
    the other bta resources. Those endpoints answer only to POST (Allow: POST), so this posts with
    the cert and the token.

