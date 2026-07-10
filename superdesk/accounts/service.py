# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2026 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import uuid
import logging
import datetime
from typing import Any, Optional

import bcrypt
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from superdesk.utc import utcnow
from superdesk.core import get_app_config, get_current_async_app

logger = logging.getLogger(__name__)

ACCOUNTS_COLLECTION = "accounts"
ACCOUNT_TENANTS_COLLECTION = "account_tenants"
SWITCH_TOKENS_COLLECTION = "account_switch_tokens"

#: mongo TTL cleanup for unredeemed switch nonces; the signed token itself
#: expires much earlier (SWITCH_TOKEN_MAX_AGE in api.py)
SWITCH_TOKEN_RETENTION_SECONDS = 300


def is_shared_accounts_enabled() -> bool:
    return bool(get_app_config("SHARED_ACCOUNTS_ENABLED", False))


def is_accounts_authoritative() -> bool:
    """Phase 2: passwords live only on the account; no tenant-local fallback."""
    return is_shared_accounts_enabled() and bool(get_app_config("SHARED_ACCOUNTS_AUTHORITATIVE", False))


def _collection():
    return get_current_async_app().tenants.get_control_plane_collection(ACCOUNTS_COLLECTION)


def _collection_async():
    return get_current_async_app().tenants.get_control_plane_collection_async(ACCOUNTS_COLLECTION)


def _mapping_collection():
    return get_current_async_app().tenants.get_control_plane_collection(ACCOUNT_TENANTS_COLLECTION)


def _switch_tokens_collection():
    return get_current_async_app().tenants.get_control_plane_collection(SWITCH_TOKENS_COLLECTION)


def ensure_account_indexes() -> None:
    _collection().create_index("email", unique=True)
    _collection().create_index("username", unique=True, sparse=True)
    _mapping_collection().create_index([("account_id", 1), ("tenant", 1)], unique=True)
    _switch_tokens_collection().create_index("_created", expireAfterSeconds=SWITCH_TOKEN_RETENTION_SECONDS)


def issue_switch_nonce() -> str:
    """Register a single-use nonce for a tenant-switch token."""
    nonce = uuid.uuid4().hex
    _switch_tokens_collection().insert_one({"_id": nonce, "_created": utcnow()})
    return nonce


def redeem_switch_nonce(nonce: str) -> bool:
    """Consume a switch nonce; False when unknown, expired or already used."""
    if not nonce:
        return False
    return _switch_tokens_collection().find_one_and_delete({"_id": nonce}) is not None


def record_account_tenant(account_id: ObjectId, tenant_id: str) -> None:
    """Maintain the account -> tenants mapping (used by the tenant switcher)."""
    _mapping_collection().update_one(
        {"account_id": account_id, "tenant": tenant_id},
        {"$set": {"_updated": utcnow()}, "$setOnInsert": {"_created": utcnow()}},
        upsert=True,
    )


def list_account_tenants(account_id: ObjectId) -> list[str]:
    return sorted(doc["tenant"] for doc in _mapping_collection().find({"account_id": account_id}))


def _credentials_query(username_or_email: str) -> dict:
    value = (username_or_email or "").strip()
    return {"$or": [{"username": value}, {"email": value.lower()}]}


async def find_account(username_or_email: str) -> Optional[dict]:
    if not username_or_email:
        return None
    return await _collection_async().find_one(_credentials_query(username_or_email))


def find_account_sync(username_or_email: str) -> Optional[dict]:
    if not username_or_email:
        return None
    return _collection().find_one(_credentials_query(username_or_email))


def find_account_sync_by_id(account_id) -> Optional[dict]:
    try:
        oid = ObjectId(account_id)
    except Exception:
        return None
    return _collection().find_one({"_id": oid})


def build_account_query(q: str = "") -> dict:
    """Mongo query matching a search term against email and username."""
    q = (q or "").strip()
    if not q:
        return {}
    import re

    pattern = re.compile(re.escape(q), re.IGNORECASE)
    return {"$or": [{"email": pattern}, {"username": pattern}]}


def count_accounts_sync(query: Optional[dict] = None) -> int:
    return _collection().count_documents(query or {})


def list_accounts_sync(query: Optional[dict] = None, page: int = 1, max_results: Optional[int] = None) -> list[dict]:
    """List accounts sorted by email; ``max_results=None`` returns everything."""
    cursor = _collection().find(query or {}).sort("email", 1)
    if max_results:
        cursor = cursor.skip(max(0, (page - 1) * max_results)).limit(max_results)
    return list(cursor)


def update_account_sync(email: str, updates: dict) -> bool:
    """Update account flags/fields; returns False when the account doesn't exist."""
    result = _collection().update_one({"email": email.strip().lower()}, {"$set": {**updates, "_updated": utcnow()}})
    return result.matched_count > 0


def verify_account_password(account: dict, password: str) -> bool:
    hashed = (account.get("password") or "").encode("UTF-8")
    encoded = (password or "").encode("UTF-8")
    if not hashed or not encoded:
        return False
    return bcrypt.checkpw(encoded, hashed)


def account_password_expired(account: dict) -> bool:
    expiry_days = get_app_config("PASSWORD_EXPIRY_DAYS", 0) or 0
    if int(expiry_days) <= 0:
        return False
    changed_on = account.get("password_changed_on")
    return changed_on is None or (changed_on + datetime.timedelta(days=int(expiry_days))) < utcnow()


async def upsert_account_credentials(
    email: str,
    username: Optional[str],
    password_hash: Optional[str],
    password_changed_on: Optional[datetime.datetime] = None,
) -> ObjectId:
    """Create the account for ``email`` or update its credentials; returns the account id.

    On a username unique-index conflict (same username already claimed by another
    account) the account is created/updated without a username — email login only.
    """

    email = email.strip().lower()
    now = utcnow()

    update: dict[str, Any] = {
        "$set": {"_updated": now},
        "$setOnInsert": {
            "email": email,
            "is_enabled": True,
            "is_super_admin": False,
            "_created": now,
        },
    }
    if password_hash:
        update["$set"]["password"] = password_hash
        update["$set"]["password_changed_on"] = password_changed_on or now
        # a fresh password satisfies any pending reset requirement
        update["$set"]["needs_password_reset"] = False
    else:
        update["$setOnInsert"]["needs_password_reset"] = False
    if username:
        update["$setOnInsert"]["username"] = username

    try:
        await _collection_async().update_one({"email": email}, update, upsert=True)
    except DuplicateKeyError:
        logger.warning("username '%s' already belongs to another account, linking '%s' by email only", username, email)
        update["$setOnInsert"].pop("username", None)
        await _collection_async().update_one({"email": email}, update, upsert=True)

    account = await _collection_async().find_one({"email": email})
    return account["_id"]


async def link_user_credentials(user_doc: dict) -> Optional[ObjectId]:
    """Dual-write hook: create-or-update the account for a tenant user doc that
    carries a (hashed) password, and set ``account_id`` on the doc.

    No-op unless ``SHARED_ACCOUNTS_ENABLED`` is set and the doc has an email
    and password hash.
    """

    if not is_shared_accounts_enabled():
        return None
    if not user_doc.get("email") or not user_doc.get("password"):
        return None

    account_id = await upsert_account_credentials(
        user_doc["email"],
        user_doc.get("username"),
        user_doc["password"],
        user_doc.get("password_changed_on"),
    )
    user_doc["account_id"] = account_id

    from superdesk.core.tenants import try_get_current_tenant

    tenant = try_get_current_tenant()
    if tenant is not None and not tenant.is_default:
        record_account_tenant(account_id, tenant.id)

    if is_accounts_authoritative():
        # phase 2: the account is the only credential store
        user_doc.pop("password", None)

    return account_id
