ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.18
FROM ${BUILD_FROM}

# Alpine 3.18 ships Python 3.11. cryptography (needed by broadlink) is taken
# from Alpine so it is precompiled for armhf/armv7/aarch64/amd64/i386.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        py3-cryptography

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
