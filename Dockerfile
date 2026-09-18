ARG PYTHON_DEP='python3 python3-wheel python3-typing-extensions python3-pandas python3-six python3-dateutil python3-brotli python3-pycryptodome libatlas3-base python3-cryptography python3-scipy androguard python3-flask python3-paho-mqtt python3-ruamel.yaml ca-certificates python3-numpy'
ARG DEBIAN_FRONTEND=noninteractive
# the dependencies of the wheel, they are installed in their own layer that stays cached as long as they don't change
FROM --platform=$BUILDPLATFORM python:3.11-slim AS requirements
ARG PSACC_VERSION="0.0.0"
COPY ./dist/psa_car_controller-${PSACC_VERSION}-py3-none-any.whl /
RUN python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); \
    metadata = next(n for n in z.namelist() if n.endswith('.dist-info/METADATA')); \
    print('\n'.join(l.split(': ', 1)[1] for l in z.read(metadata).decode().splitlines() if l.startswith('Requires-Dist: ')))" \
    /psa_car_controller-${PSACC_VERSION}-py3-none-any.whl > /requirements.txt

FROM debian:bookworm-slim AS builder
ARG PYTHON_DEP
RUN  BUILD_DEP='python3-pip python3-setuptools python3-dev libblas-dev liblapack-dev gfortran libffi-dev libxml2-dev libxslt1-dev make automake gcc g++ subversion ninja-build' ; \
     apt-get update && apt-get install -y --no-install-recommends $BUILD_DEP $PYTHON_DEP;
ARG PSACC_VERSION="0.0.0"
RUN pip3 install --break-system-packages --upgrade pip wheel setuptools
COPY --from=requirements /requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY ./dist/psa_car_controller-${PSACC_VERSION}-py3-none-any.whl .
RUN pip3 install --break-system-packages --no-cache-dir --no-deps psa_car_controller-${PSACC_VERSION}-py3-none-any.whl
EXPOSE 5000

FROM debian:bookworm-slim
ARG PYTHON_DEP
WORKDIR /config
ENV PSACC_BASE_PATH=/ PSACC_PORT=5000 PSACC_OPTIONS="-c -r --web-conf" PSACC_CONFIG_DIR="/config" PYTHONPATH="/app"
COPY --from=builder /var/lib/apt /var/lib/apt
COPY --from=builder /var/cache/apt/ /var/cache/apt/
RUN  apt-get install -y --no-install-recommends $PYTHON_DEP curl && \
     apt-get clean ; \
     rm -rf /var/lib/apt/lists/*
# after the apt install so a new release doesn't invalidate it
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin/  /usr/local/bin/

# Install Playwright and Chromium headless shell dependencies (skip on ARM where not available via pip)
RUN pip3 install --break-system-packages playwright 2>/dev/null && \
    playwright install --with-deps --only-shell chromium 2>/dev/null || \
    echo "Playwright not available on this architecture, headless auth will use manual fallback"

COPY /docker_files/init.sh /init.sh
CMD /init.sh
