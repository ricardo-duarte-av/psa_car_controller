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
           "battery_level": 64, "autonomy": 22, "charging": true, "charging_rate": 0, "remaining_time": 635,
           "cable_plugged": true, "preconditioning": false, "raw": {...}}}
    ```

    Example with curl: `curl -N http://localhost:5000/events`
