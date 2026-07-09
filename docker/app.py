# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""ASGI entrypoint for running superdesk-core standalone (docker compose / dev).

All configuration comes from environment variables (see superdesk/default_settings.py).
Run with: hypercorn --bind 0.0.0.0:5000 docker.app:application
"""

from superdesk.factory.app import get_app

application = get_app()
