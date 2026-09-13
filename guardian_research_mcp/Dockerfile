# syntax=docker/dockerfile:1.7
FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20 AS wheelhouse
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m pip download --only-binary=:all: \
      --requirement /tmp/requirements.lock --dest /wheelhouse

FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
COPY requirements.lock /tmp/requirements.lock
RUN --mount=type=bind,from=wheelhouse,source=/wheelhouse,target=/wheelhouse \
    python3 -m pip install --no-cache-dir --break-system-packages \
      --no-index --find-links=/wheelhouse --requirement /tmp/requirements.lock
COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
