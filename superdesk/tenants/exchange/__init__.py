# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Cross-tenant content exchange.

Content flows tenant→tenant through the regular publish→ingest pipeline:
a subscriber in the sender tenant uses the ``internal_tenant`` transmitter
(ninjs formatter), which copies media into the target tenant's storage and
enqueues a Celery task that ingests the item in the target tenant's context.

Both tenants must allow each other in their ``exchange_partners`` list
(sender: direction ``send``/``both``; receiver: ``receive``/``both``).
"""

from .transmitter import InternalTenantTransmitter  # noqa: F401
from .receiver import deliver_to_tenant  # noqa: F401
from .feeding_service import TenantExchangeFeedingService  # noqa: F401
