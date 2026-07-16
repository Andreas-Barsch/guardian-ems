FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20

RUN apk add --no-cache py3-pip
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
