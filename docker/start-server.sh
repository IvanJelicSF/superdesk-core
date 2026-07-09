#!/bin/bash
# Boot the superdesk-core API server: wait for backing services, initialize
# data (idempotent), ensure the admin user exists, then serve via hypercorn.
set -e

echo "waiting for mongo and elasticsearch..."
python - << 'PYEOF'
import os
import time
import socket
import urllib.request
from urllib.parse import urlparse

def wait_tcp(host, port, name, timeout=120):
    for _ in range(timeout):
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"{name} is up")
                return
        except OSError:
            time.sleep(1)
    raise SystemExit(f"{name} did not come up in {timeout}s")

mongo = urlparse(os.environ.get("MONGO_URI", "mongodb://mongo/superdesk"))
wait_tcp(mongo.hostname, mongo.port or 27017, "mongo")

elastic = os.environ.get("ELASTICSEARCH_URL", "http://elastic:9200")
parsed = urlparse(elastic)
wait_tcp(parsed.hostname, parsed.port or 9200, "elastic")
for _ in range(120):
    try:
        urllib.request.urlopen(elastic, timeout=2)
        print("elastic is ready")
        break
    except Exception:
        time.sleep(1)
PYEOF

export QUART_APP=docker.app:application

multi_tenant=$(echo "${MULTI_TENANT_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')
if [ "$multi_tenant" = "true" ] || [ "$multi_tenant" = "1" ] || [ "$multi_tenant" = "yes" ] || [ "$multi_tenant" = "on" ]; then
    # multi-tenant mode: no global db to initialize - each tenant is provisioned
    # (indexes, mappings, seed data, admin user) by tenants:create, e.g.:
    #   docker compose exec server quart tenants:create tenant-a \
    #     --host tenant-a.localhost --admin-username admin --admin-password admin \
    #     --admin-email admin@example.com
    echo "multi-tenant mode: skipping global data init; create tenants with 'quart tenants:create'"
else
    echo "initializing data (idempotent)..."
    quart app:initialize_data

    if [ -n "${SUPERDESK_ADMIN_USERNAME:-}" ]; then
        echo "ensuring admin user '${SUPERDESK_ADMIN_USERNAME}' exists (noop if present)..."
        quart users:create \
            -u "${SUPERDESK_ADMIN_USERNAME}" \
            -p "${SUPERDESK_ADMIN_PASSWORD:-admin}" \
            -e "${SUPERDESK_ADMIN_EMAIL:-admin@example.com}" \
            --admin || echo "users:create failed (continuing)"
    fi
fi

echo "starting api server on :5000"
exec hypercorn --bind 0.0.0.0:5000 --workers "${WEB_CONCURRENCY:-2}" docker.app:application
