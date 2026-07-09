FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    QUART_APP=docker.app:application

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    g++ \
    curl \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/superdesk

COPY . .

RUN pip install --upgrade pip setuptools wheel && pip install -e .

RUN chmod +x docker/start-server.sh

EXPOSE 5000 5100

CMD ["/opt/superdesk/docker/start-server.sh"]
