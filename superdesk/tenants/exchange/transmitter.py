# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import json
import logging

from superdesk.core import get_current_async_app
from superdesk.core.tenants import get_current_tenant
from superdesk.core.tenants.celery import TENANT_TASK_HEADER
from superdesk.errors import SuperdeskPublishError
from superdesk.publish import register_transmitter
from superdesk.publish.publish_service import PublishService

from .media import copy_item_media

logger = logging.getLogger(__name__)


class PublishTenantExchangeError(SuperdeskPublishError):
    _codes = {
        17100: "Tenant exchange error",
    }

    @classmethod
    def tenantExchangeError(cls, exception=None, destination=None):
        return cls(17100, exception, destination)


errors = [PublishTenantExchangeError.tenantExchangeError().get_error_description()]


class InternalTenantTransmitter(PublishService):
    """Delivers published items to another tenant of the same deployment.

    Destination config: ``{"tenant": <target id>, "auto_fetch": bool, "desk": ..., "stage": ...}``.
    The formatted (ninjs) item has its media copied into the target tenant's
    storage, then a Celery task ingests it in the target tenant's context.
    """

    NAME = "internal tenant"

    async def _transmit(self, queue_item, subscriber):
        destination = queue_item.get("destination") or {}
        config = destination.get("config") or {}
        target_id = config.get("tenant")

        source = get_current_tenant()
        target = get_current_async_app().tenants.get_by_id_sync(target_id) if target_id else None

        if source.is_default or target is None or not target.is_active:
            raise PublishTenantExchangeError.tenantExchangeError(
                Exception(f"Unknown or inactive target tenant '{target_id}'"), destination
            )
        if not source.can_send_to(target.id) or not target.can_receive_from(source.id):
            raise PublishTenantExchangeError.tenantExchangeError(
                Exception(f"Tenants '{source.id}' and '{target.id}' are not exchange partners"), destination
            )

        item = json.loads(queue_item["formatted_item"])

        # provenance: keep the guid stable so re-sends update instead of duplicating
        item.setdefault("extra", {})
        item["extra"]["original_tenant"] = source.id
        item["extra"]["original_item_id"] = queue_item.get("item_id")
        item.setdefault("source", source.id)

        copied = await copy_item_media(item, target)
        logger.info(
            "exchange transmit item=%s source=%s target=%s media=%d",
            queue_item.get("item_id"),
            source.id,
            target.id,
            copied,
        )

        from .receiver import deliver_to_tenant

        await deliver_to_tenant.apply_async(
            kwargs=dict(
                item=item,
                source_tenant=source.id,
                auto_fetch=bool(config.get("auto_fetch")),
                desk=config.get("desk"),
                stage=config.get("stage"),
            ),
            headers={TENANT_TASK_HEADER: target.id},
        )


register_transmitter("internal_tenant", InternalTenantTransmitter(), errors)
