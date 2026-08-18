"""db/database.py's office_digest_group get/set — the per-owner Telegram
group an owner (system owner OR a customer) opts to bind as a read-only
showcase of office_events activity, per docs/OFFICES_DESIGN.md §12 and the
Stage 1 multitenancy rollout (per-owner, not a single global row).

NOTE: unlike most other *_db.py test files, this one predates the
isolated_db fixture and actually hits the real data/bots.db (see
MEMORY.md "Backlog: test DB isolation") — each test uses a distinct,
unlikely-to-collide owner_telegram_id and cleans its own row up in a
finally block so repeated runs don't leak state into each other or into
runtime/factory_analytics_api.py's tests.

Run with: python -m pytest tests/test_office_digest_group_db.py
"""
from __future__ import annotations

import aiosqlite
import pytest

import db.database as db_module
from db.database import get_office_digest_group, init_db, set_office_digest_group


async def _clear(owner_telegram_id: int) -> None:
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute(
            "DELETE FROM office_digest_group WHERE owner_telegram_id = ?", (owner_telegram_id,)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_no_digest_group_by_default(isolated_db):
    await init_db()
    owner_id = 920901
    await _clear(owner_id)
    try:
        assert await get_office_digest_group(owner_id) is None
    finally:
        await _clear(owner_id)


@pytest.mark.asyncio
async def test_set_then_get_digest_group(isolated_db):
    await init_db()
    owner_id = 920902
    try:
        await set_office_digest_group(owner_id, "-100123456789")
        assert await get_office_digest_group(owner_id) == "-100123456789"
    finally:
        await _clear(owner_id)


@pytest.mark.asyncio
async def test_rebinding_replaces_previous_group(isolated_db):
    await init_db()
    owner_id = 920903
    try:
        await set_office_digest_group(owner_id, "-100111")
        await set_office_digest_group(owner_id, "-100222")
        assert await get_office_digest_group(owner_id) == "-100222"
    finally:
        await _clear(owner_id)


@pytest.mark.asyncio
async def test_digest_group_is_per_owner(isolated_db):
    """Stage 1 multitenancy: two different owners each bind their own group
    without clobbering each other's — the old schema was a single fixed row
    (id=1) shared by the whole factory; this is the behavior change that
    replaces it."""
    await init_db()
    owner_a, owner_b = 920904, 920905
    try:
        await set_office_digest_group(owner_a, "-100111")
        await set_office_digest_group(owner_b, "-100222")
        assert await get_office_digest_group(owner_a) == "-100111"
        assert await get_office_digest_group(owner_b) == "-100222"
    finally:
        await _clear(owner_a)
        await _clear(owner_b)
