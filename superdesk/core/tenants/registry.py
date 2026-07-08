# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import time
import threading
from typing import TYPE_CHECKING, Optional

from pymongo import MongoClient, uri_parser
from pymongo.collection import Collection
from motor.motor_asyncio import AsyncIOMotorClient

from .models import Tenant, TenantStatus

if TYPE_CHECKING:
    from superdesk.core.app import SuperdeskAsyncApp


COLLECTION_NAME = "tenants"

#: cache key holding the list of all active tenant ids
_ALL_ACTIVE_KEY = "__all_active__"


class TenantRegistry:
    """Read/lookup layer for the tenants control-plane collection.

    Owns its own mongo clients (the control-plane db must never be routed
    through the tenant-scoped ``MongoResources``) and keeps a small TTL cache
    so per-request host lookups don't hit mongo.
    """

    def __init__(self, app: "SuperdeskAsyncApp"):
        self.app = app
        self._client: Optional[MongoClient] = None
        self._client_async: Optional[AsyncIOMotorClient] = None
        self._dbname = ""
        self._dbname_async = ""
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _connection_config(self) -> tuple[str, str]:
        config = self.app.wsgi.config
        dbname = config.get("TENANTS_MONGO_DBNAME", "superdesk_tenants")
        uri = config.get("TENANTS_MONGO_URI", f"mongodb://localhost/{dbname}")
        parsed_dbname = uri_parser.parse_uri(uri).get("database")
        return uri, parsed_dbname or dbname

    def _cache_ttl(self) -> float:
        return float(self.app.wsgi.config.get("TENANTS_CACHE_TTL", 60))

    @property
    def collection(self) -> Collection:
        if self._client is None:
            uri, dbname = self._connection_config()
            self._client = MongoClient(uri, tz_aware=True)
            self._dbname = dbname
        return self._client[self._dbname][COLLECTION_NAME]

    def get_collection_async(self):
        if self._client_async is None:
            uri, dbname = self._connection_config()
            self._client_async = AsyncIOMotorClient(uri, tz_aware=True)
            self._dbname_async = dbname
        return self._client_async[self._dbname_async][COLLECTION_NAME]

    def ensure_indexes(self) -> None:
        self.collection.create_index("hosts", unique=True, sparse=True)

    # cache helpers; unknown ids/hosts are cached as None too, so bad hosts
    # don't hammer the control-plane db

    def _cache_get(self, key: str):
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or entry[0] < time.monotonic():
                return None
            return entry

    def _cache_set(self, key: str, value) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic() + self._cache_ttl(), value)

    def invalidate(self, tenant_id: str | None = None) -> None:
        with self._lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(f"id:{tenant_id}", None)
                self._cache = {
                    key: entry
                    for key, entry in self._cache.items()
                    if not (
                        key == _ALL_ACTIVE_KEY
                        or (key.startswith("host:") and isinstance(entry[1], Tenant) and entry[1].id == tenant_id)
                    )
                }

    def _store(self, doc: Optional[dict], cache_key: str) -> Optional[Tenant]:
        tenant = Tenant.from_dict(doc) if doc is not None else None
        self._cache_set(cache_key, tenant)
        if tenant is not None:
            self._cache_set(f"id:{tenant.id}", tenant)
        return tenant

    def get_by_id_sync(self, tenant_id: str) -> Optional[Tenant]:
        cached = self._cache_get(f"id:{tenant_id}")
        if cached is not None:
            return cached[1]  # type: ignore[return-value]
        return self._store(self.collection.find_one({"_id": tenant_id}), f"id:{tenant_id}")

    async def get_by_id(self, tenant_id: str) -> Optional[Tenant]:
        cached = self._cache_get(f"id:{tenant_id}")
        if cached is not None:
            return cached[1]  # type: ignore[return-value]
        doc = await self.get_collection_async().find_one({"_id": tenant_id})
        return self._store(doc, f"id:{tenant_id}")

    def get_by_host_sync(self, host: str) -> Optional[Tenant]:
        cached = self._cache_get(f"host:{host}")
        if cached is not None:
            return cached[1]  # type: ignore[return-value]
        return self._store(self.collection.find_one({"hosts": host}), f"host:{host}")

    async def get_by_host(self, host: str) -> Optional[Tenant]:
        cached = self._cache_get(f"host:{host}")
        if cached is not None:
            return cached[1]  # type: ignore[return-value]
        doc = await self.get_collection_async().find_one({"hosts": host})
        return self._store(doc, f"host:{host}")

    def get_all_active_sync(self) -> list[Tenant]:
        cached = self._cache_get(_ALL_ACTIVE_KEY)
        if cached is not None:
            return cached[1]  # type: ignore[return-value]
        tenants = [Tenant.from_dict(doc) for doc in self.collection.find({"status": TenantStatus.ACTIVE.value})]
        self._cache_set(_ALL_ACTIVE_KEY, tenants)
        for tenant in tenants:
            self._cache_set(f"id:{tenant.id}", tenant)
        return tenants

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._client_async is not None:
            self._client_async.close()
            self._client_async = None
        with self._lock:
            self._cache.clear()
