FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20

ARG BUILD_VERSION
LABEL org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache py3-pip
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app /app
COPY run.sh /run.sh
COPY .guardian-source-commit /tmp/guardian-source-commit
COPY tools/seal_build_info.py /tmp/seal_build_info.py
RUN PYTHONPATH=/app python3 /tmp/seal_build_info.py "${BUILD_VERSION}" /tmp/guardian-source-commit /app/build-info.json \
    && chmod 0444 /app/build-info.json \
    && rm /tmp/guardian-source-commit /tmp/seal_build_info.py \
    && chmod a+x /run.sh

CMD ["/run.sh"]
