# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import logging

from superdesk import get_resource_service
from superdesk.celery_app import celery
from superdesk.core.tenants import get_current_tenant
from superdesk.errors import ProviderError
from superdesk.io.registry import registered_feeding_services
from superdesk.io.feed_parsers.ninjs import NINJSFeedParser
from superdesk.io.commands.update_ingest import ingest_items
from superdesk.utc import utcnow

from .feeding_service import TenantExchangeFeedingService

logger = logging.getLogger(__name__)


async def get_exchange_provider(source_tenant: str) -> dict:
    """Get-or-create the per-partner ingest provider in the current (target) tenant."""

    service = get_resource_service("ingest_providers")
    provider = await service.find_one_async(
        req=None, feeding_service=TenantExchangeFeedingService.NAME, source=source_tenant
    )
    if provider is not None:
        return provider

    provider = {
        "name": f"Tenant exchange: {source_tenant}",
        "source": source_tenant,
        "feeding_service": TenantExchangeFeedingService.NAME,
        "feed_parser": "ninjs",
        "content_expiry": None,
        "is_closed": False,
        "last_updated": utcnow(),
    }
    ids = await service.create_async([provider])
    logger.info("created exchange ingest provider for source tenant %s", source_tenant)
    return await service.find_one_async(req=None, _id=ids[0])


@celery.task(name="tenants.deliver_to_tenant", bind=True)
def deliver_to_tenant(self, item: dict, source_tenant: str, auto_fetch=False, desk=None, stage=None):
    """Ingest an exchanged (ninjs) item in the target tenant's context.

    The tenant is restored from the task headers by the standard prologue;
    this body already runs in the target tenant.
    """

    return _deliver(item, source_tenant, auto_fetch=auto_fetch, desk=desk, stage=stage)


async def _deliver(item: dict, source_tenant: str, auto_fetch=False, desk=None, stage=None):
    target = get_current_tenant()
    if target.is_default or not target.can_receive_from(source_tenant):
        raise ProviderError.ingestError(
            Exception(f"Tenant '{target.id}' does not accept content from '{source_tenant}'"), provider=None
        )

    provider = await get_exchange_provider(source_tenant)
    feeding_service = registered_feeding_services[TenantExchangeFeedingService.NAME].__class__()

    parser = NINJSFeedParser()
    parsed = parser._transform_from_ninjs(item)
    parsed.setdefault("guid", item.get("guid"))

    # keep the media references produced by the transmitter's in-process copy:
    # the parser rebuilds renditions from href only, dropping the media ids
    # that make the target-side ingest skip re-downloading
    if item.get("renditions"):
        parsed["renditions"] = item["renditions"]
    for key, association in (item.get("associations") or {}).items():
        if association and association.get("renditions") and parsed.get("associations", {}).get(key):
            parsed["associations"][key]["renditions"] = association["renditions"]

    failed = await ingest_items([parsed], provider, feeding_service)
    if failed:
        raise ProviderError.ingestError(Exception(f"Failed to ingest exchanged item(s): {failed}"), provider=provider)

    logger.info("exchange delivered item=%s source=%s target=%s", item.get("guid"), source_tenant, target.id)

    if auto_fetch:
        from apps.archive.common import fetch_item  # local import, avoids import cycle with apps

        try:
            ingest_service = get_resource_service("ingest")
            ingested = await ingest_service.find_one_async(req=None, guid=parsed.get("guid"))
            if ingested is not None:
                await fetch_item(ingested, desk, stage)
        except Exception:
            logger.exception("exchange auto-fetch failed item=%s target=%s", item.get("guid"), target.id)

    return parsed.get("guid")
