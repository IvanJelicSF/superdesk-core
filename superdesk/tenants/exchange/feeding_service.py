# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from superdesk.io.registry import register_feeding_service, register_feeding_service_parser
from superdesk.io.feeding_services import FeedingService


class TenantExchangeFeedingService(FeedingService):
    """Pseudo feeding service backing the per-partner ingest providers.

    Items are pushed by the exchange receiver task, never polled — ``_update``
    always returns nothing. The provider exists so exchanged items carry a
    proper source and routing schemes apply.
    """

    NAME = "tenant_exchange"
    ERRORS: list = []
    label = "Tenant exchange"

    async def _update(self, provider, update):
        return []


register_feeding_service(TenantExchangeFeedingService)
register_feeding_service_parser(TenantExchangeFeedingService.NAME, "ninjs")
