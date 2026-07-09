# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Celery entrypoint for worker and beat processes (docker compose / dev).

Run with: celery -A docker.worker:celery worker  (or ``beat``)
"""

from superdesk.factory.app import get_app

app = get_app()
celery = app.celery
