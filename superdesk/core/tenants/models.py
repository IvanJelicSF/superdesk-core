# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import re
from enum import Enum
from typing import Any, Mapping
from dataclasses import dataclass, field


#: id of the synthetic tenant used when multi-tenancy is disabled
DEFAULT_TENANT_ID = "default"

#: tenant ids must be usable as DNS labels and (prefixed) mongo db names
TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}$")


class TenantStatus(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


def default_db_prefix(tenant_id: str) -> str:
    return "sd_" + tenant_id.replace("-", "_")


@dataclass(frozen=True)
class Tenant:
    """A tenant definition, stored in the control-plane db and cached in process.

    Instances are immutable so they can be safely shared across concurrent requests.
    """

    id: str
    #: human-readable display name (falls back to the id)
    name: str = ""
    #: free-text description shown in the admin panel
    description: str = ""
    hosts: tuple[str, ...] = ()
    status: TenantStatus = TenantStatus.ACTIVE
    db_prefix: str = ""
    elastic_prefix: str = ""
    s3_subfolder: str | None = None
    config_overrides: Mapping[str, Any] = field(default_factory=dict)
    feature_flags: Mapping[str, bool] = field(default_factory=dict)
    #: content-exchange partner allowlist: ({"tenant": id, "direction": "send"|"receive"|"both"}, ...)
    exchange_partners: tuple = ()
    #: copy exchanged media (pictures/audio/video) into this tenant's storage;
    #: when off, exchanged items keep the source tenant's asset urls
    exchange_copy_media: bool = True
    is_default: bool = False

    def __post_init__(self):
        if not self.is_default and not TENANT_ID_RE.match(self.id):
            raise ValueError(f"Invalid tenant id '{self.id}', must match {TENANT_ID_RE.pattern}")
        if not self.name:
            object.__setattr__(self, "name", self.id)
        if not self.db_prefix:
            object.__setattr__(self, "db_prefix", default_db_prefix(self.id))
        if not self.elastic_prefix:
            object.__setattr__(self, "elastic_prefix", default_db_prefix(self.id))

    @property
    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE

    def _partner_direction(self, tenant_id: str) -> str:
        for partner in self.exchange_partners:
            if partner.get("tenant") == tenant_id:
                return partner.get("direction", "both")
        return ""

    def can_send_to(self, tenant_id: str) -> bool:
        return self._partner_direction(tenant_id) in ("send", "both")

    def can_receive_from(self, tenant_id: str) -> bool:
        return self._partner_direction(tenant_id) in ("receive", "both")

    def feature_enabled(self, name: str, default: bool = True) -> bool:
        return bool(self.feature_flags.get(name, default))

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Tenant":
        return cls(
            id=doc["_id"],
            name=doc.get("name") or "",
            description=doc.get("description") or "",
            hosts=tuple(doc.get("hosts") or ()),
            status=TenantStatus(doc.get("status", TenantStatus.ACTIVE)),
            db_prefix=doc.get("db_prefix") or "",
            elastic_prefix=doc.get("elastic_prefix") or "",
            s3_subfolder=doc.get("s3_subfolder"),
            config_overrides=doc.get("config_overrides") or {},
            feature_flags=doc.get("feature_flags") or {},
            exchange_partners=tuple(doc.get("exchange_partners") or ()),
            exchange_copy_media=bool(doc.get("exchange_copy_media", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "name": self.name,
            "description": self.description,
            "hosts": list(self.hosts),
            "status": self.status.value,
            "db_prefix": self.db_prefix,
            "elastic_prefix": self.elastic_prefix,
            "s3_subfolder": self.s3_subfolder,
            "config_overrides": dict(self.config_overrides),
            "feature_flags": dict(self.feature_flags),
            "exchange_partners": list(self.exchange_partners),
            "exchange_copy_media": self.exchange_copy_media,
        }
