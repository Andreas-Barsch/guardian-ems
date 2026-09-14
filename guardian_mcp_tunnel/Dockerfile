# syntax=docker/dockerfile:1.7
FROM alpine:3.20 AS tunnel-client-download

ARG TUNNEL_CLIENT_VERSION=0.0.14
ARG TUNNEL_CLIENT_ARCHIVE_SHA256=2de3fb879a18edb847e0313592c912f1983685488290a7fdba7ac403e6a4fb0a
ARG TUNNEL_CLIENT_ARCHIVE=tunnel-client-v0.0.14-linux-arm64.zip
ARG TUNNEL_CLIENT_URL=https://github.com/openai/tunnel-client/releases/download/v0.0.14/tunnel-client-v0.0.14-linux-arm64.zip

RUN apk add --no-cache ca-certificates curl unzip \
    && curl --fail --show-error --silent --location \
      --proto '=https' --tlsv1.2 \
      "${TUNNEL_CLIENT_URL}" --output /tmp/tunnel-client.zip \
    && echo "${TUNNEL_CLIENT_ARCHIVE_SHA256}  /tmp/tunnel-client.zip" | sha256sum -c - \
    && mkdir /out \
    && unzip -q /tmp/tunnel-client.zip -d /tmp/tunnel-client \
    && install -m 0755 /tmp/tunnel-client/tunnel-client /out/tunnel-client

FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
COPY --from=tunnel-client-download /out/tunnel-client /usr/local/bin/tunnel-client
COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh /app/startup.sh \
    && tunnel-client --version
CMD ["/run.sh"]
