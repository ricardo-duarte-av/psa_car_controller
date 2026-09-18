# Docker installation

A containerised version of the psa_car_controller.
- GitHub Container Registry: https://github.com/ricardo-duarte-av/psa_car_controller/pkgs/container/psa_car_controller

### Overview
Once the container is running, the configuration of the psa_car_controller app is near-identical classic Linux/Windows installtion.

### Image tags
The image is published to GHCR on every release tag:

|tag              | content                                      |
|-----------------|----------------------------------------------|
|latest           | last release                                 |
|vX.Y.Z           | this exact release                           |
|vX.Y             | last patch of this minor version             |
|vX               | last release of this major version           |
|edge             | build started manually from the Actions tab  |

The image is only built for linux/amd64. A GHCR package is private until you make it
public in its package settings, `docker login ghcr.io` is needed while it's private.

### Installation
Create the container, detached, exposing port 5000, and mapping config folder on your host to /config inside the container:

#### With docker-compose
```
docker-compose up -d
```
#### With Docker:
```
docker run -d -ti --name psa_car_controller1 \
  --publish 5000:5000 \
  -v /host_path/config:/config \
  --restart unless-stopped \
  ghcr.io/ricardo-duarte-av/psa_car_controller
```
Go to http://127.0.0.1:5000 and follow instruction

### Environment variable:
You can modify some parameter with docker environment variable. They are all listed, commented, in
[docker-compose.yml](../docker-compose.yml).

|variable         | default            | description                                                          |
|-----------------|--------------------|----------------------------------------------------------------------|
|PSACC_CONFIG_DIR | /config            | overide configuration directory                                       |
|PSACC_PORT       | 5000               | change port                                                           |
|PSACC_BASE_PATH  | /                  | base path of the web app, for a reverse proxy serving a sub path      |
|PSACC_OPTIONS    | -c -r --web-conf   | add cli argurment                                                     |
|MQTT_LOG         | 0                  | set to 1 to log the mqtt client, to debug remote control              |
|NO_HEADLESS      | *(unset)*          | set to any value to show the browser during the first authentication  |
|TZ               | UTC                | timezone of the container, charge hours and trips use the local time  |

The cli arguments accepted by `PSACC_OPTIONS` are:

|argument              | description                                                       |
|----------------------|-------------------------------------------------------------------|
|-c \[file\]           | enable charge control, default charge_config.json                 |
|-r                    | record vehicle data in the database, needed by the dashboard      |
|--web-conf            | configure psacc from the web page instead of the console          |
|-d \[level\]          | debug level, 10 is debug, 20 is info                              |
|-R \<minutes\>        | refresh the vehicle status every \<minutes\> minutes              |
|--remote-disable      | disable remote control, no mqtt                                   |
|--offline             | offline limited mode                                              |

### Publishing your own image
The release workflow (`.github/workflows/release.yml`) pushes to `ghcr.io/<owner>/<repo>` using the built in
`GITHUB_TOKEN`, so nothing has to be configured to publish from a fork: push a `vX.Y.Z` tag, or start the workflow
from the Actions tab to get an `edge` image. The package is created private, make it public once from the package
settings if you want to pull it without a login.

Docker Hub is only used when the `DOCKER_USERNAME` and `DOCKER_PASSWORD` secrets are set, and pypi publishing is
skipped when `PYPI_TOKEN` isn't set.
