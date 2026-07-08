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
from typing import Iterator

from superdesk.core import get_current_async_app
from superdesk.core.tenants import Tenant, tenant_context

logger = logging.getLogger(__name__)


def _iter_renditions(item: dict) -> Iterator[dict]:
    for rendition in (item.get("renditions") or {}).values():
        yield rendition
    for association in (item.get("associations") or {}).values():
        if association:
            for rendition in (association.get("renditions") or {}).values():
                yield rendition


async def copy_item_media(item: dict, target: Tenant) -> int:
    """Copy an item's rendition media into the target tenant's storage.

    Reads bytes in the current (source) tenant context, writes them inside the
    target tenant context, and rewrites ``media``/``href`` in place so the
    ingest pipeline on the target side short-circuits its download step
    (``transfer_renditions`` keeps local media as is).

    Returns the number of copied files.
    """

    media_storage = get_current_async_app().wsgi.media

    collected = []
    for rendition in _iter_renditions(item):
        media_id = rendition.get("media")
        if not media_id:
            continue
        media_file = await media_storage.get_async(media_id)
        if media_file is None:
            logger.warning("media %s not found while copying to tenant %s", media_id, target.id)
            continue
        collected.append(
            (
                rendition,
                await media_file.to_bytes(),
                media_file.filename,
                media_file.content_type,
                dict(media_file.metadata or {}),
            )
        )

    with tenant_context(target):
        for rendition, content, filename, content_type, metadata in collected:
            new_id = await media_storage.put_async(
                content, filename=filename, content_type=content_type, metadata=metadata
            )
            rendition["media"] = str(new_id)
            rendition["href"] = media_storage.url_for_media(new_id, content_type)

    return len(collected)
