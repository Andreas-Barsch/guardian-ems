FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20

ARG BUILD_VERSION

LABEL org.opencontainers.image.version="${BUILD_VERSION}"

RUN apk add --no-cache py3-pip
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app /app
COPY run.sh /run.sh
COPY .guardian-source-commit /tmp/guardian-source-commit
RUN source_commit="$(cat /tmp/guardian-source-commit)" \
    && test "$(printf '%s' "${source_commit}" | wc -c)" -eq 40 \
    && case "${source_commit}" in *[!0-9a-f]*) exit 1 ;; esac \
    && test -n "${BUILD_VERSION}" \
    && printf '{"guardian_version":"%s","source_commit":"%s"}\n' \
         "${BUILD_VERSION}" "${source_commit}" > /app/build-info.json \
    && chmod 0444 /app/build-info.json \
    && rm /tmp/guardian-source-commit \
    && chmod a+x /run.sh

CMD ["/run.sh"]
