"""db/database.py's office_digest_group get/set — the single Telegram group
the owner opts to bind as a read-only showcase of office_events activity,
per docs/OFFICES_DESIGN.md §12.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db — see MEMORY.md "Backlog: test DB isolation".

Run with: python -m pytest tests/test_office_digest_group_db.py
"""
from __future__ import annotations

import pytest

from db.database import get_office_digest_group, init_db, set_office_digest_group


@pytest.mark.asyncio
async def test_no_digest_group_by_default(isolated_db):
    await init_db()
    assert await get_office_digest_group() is None


@pytest.mark.asyncio
async def test_set_then_get_digest_group(isolated_db):
    await init_db()
    await set_office_digest_group("-100123456789")
    assert await get_office_digest_group() == "-100123456789"


@pytest.mark.asyncio
async def test_rebinding_replaces_previous_group(isolated_db):
    await init_db()
    await set_office_digest_group("-100111")
    await set_office_digest_group("-100222")
    assert await get_office_digest_group() == "-100222"
