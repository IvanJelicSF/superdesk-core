# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Tenant-aware subclasses of the legacy eve/motor mongo drivers.

Eve's ``PyMongo`` and superdesk's ``FlaskMongoAsync`` store one ``(client, db)``
pair per config prefix in ``app.extensions`` and expose them via ``.cx``/``.db``
properties that eve internals read on every operation. These shims keep the
client shared per prefix but resolve the ``db`` handle from the current tenant
at property-access time, so the whole legacy data layer (including GridFS,
versioning and aggregations) becomes tenant-scoped without touching eve.
"""

from quart import current_app

from eve.io.base import ConnectionException
from eve.io.mongo.mongo import Mongo
from eve.io.mongo.flask_pymongo import PyMongo
from eve_elastic import Elastic

from superdesk.eve_async.mongo_datalayer import MongoAsync
from superdesk.eve_async.flask_mongo import FlaskMongoAsync
from superdesk.eve_async.elastic_datalayer import ElasticAsync

from .context import get_current_tenant
from .naming import tenant_db_name, tenant_index_prefix


def _tenant_db(extensions: dict, config_prefix: str):
    """Resolve (and cache) the current tenant's db handle off the shared client."""

    tenant = get_current_tenant()
    if tenant.is_default:
        return extensions[config_prefix][1]

    cache_key = f"{config_prefix}::{tenant.id}"
    if cache_key not in extensions:
        client = extensions[config_prefix][0]
        extensions[cache_key] = (client, client[tenant_db_name(tenant, config_prefix)])
    return extensions[cache_key][1]


class TenantPyMongo(PyMongo):
    @property
    def db(self):
        return _tenant_db(current_app.extensions["pymongo"], self.config_prefix)

    def get_db_from_app(self, app):
        return _tenant_db(app.extensions["pymongo"], self.config_prefix)


class TenantFlaskMongoAsync(FlaskMongoAsync):
    @property
    def db(self):
        return _tenant_db(current_app.extensions["pymongo_async"], self.config_prefix)

    def get_db_from_app(self, app):
        return _tenant_db(app.extensions["pymongo_async"], self.config_prefix)


class TenantMongo(Mongo):
    def pymongo(self, resource=None, prefix=None) -> TenantPyMongo:
        px = prefix if prefix else self.current_mongo_prefix(resource=resource)

        if px not in self.driver:
            self.driver[px] = TenantPyMongo(self.app, px)

        # important, we don't want to preserve state between requests
        self.mongo_prefix = None

        try:
            return self.driver[px]
        except Exception as e:
            raise ConnectionException(e)


class TenantMongoAsync(MongoAsync):
    def pymongo(self, resource=None, prefix=None) -> TenantFlaskMongoAsync:
        px = prefix if prefix else self.current_mongo_prefix(resource=resource)

        if px not in self.driver:
            self.driver[px] = TenantFlaskMongoAsync(self.app, px)

        # important, we don't want to preserve state between requests
        self.mongo_prefix = None

        try:
            return self.driver[px]
        except Exception as e:
            raise ConnectionException(e)


def _tenant_resource_index(elastic: Elastic, resource: str, default_index: str) -> str:
    tenant = get_current_tenant()
    if tenant.is_default:
        return default_index
    prefix = elastic._resource_prefix(resource)
    datasource = elastic.get_datasource(resource)
    return f"{tenant_index_prefix(tenant, prefix)}_{datasource[0]}"


class TenantElastic(Elastic):
    def _resource_index(self, resource):
        return _tenant_resource_index(self, resource, super()._resource_index(resource))


class TenantElasticAsync(ElasticAsync):
    def _resource_index(self, resource):
        return _tenant_resource_index(self, resource, super()._resource_index(resource))
