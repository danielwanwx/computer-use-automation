from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cua.sessions import SessionHandle
from scripts.prepare_v9_handoff import _BLOCKER_PATH, _prepare_target_session


def test_blocker_path_is_a_param_free_authenticated_route():
    assert _BLOCKER_PATH == "/transfer.htm"
    assert _BLOCKER_PATH != "/activity.htm"


def test_prepare_target_session_uses_private_handle_behind_public_view():
    handle = SessionHandle(
        session_id="s_0123456789abcdef0123456789abcdef",
        page_id="p_0123456789abcdef01234567",
        principal_alias="alpha",
        profile_id="parabank-native-v1",
        origin="http://127.0.0.1:8080/parabank",
        browser_version="153.0.8010.53",
        auth_generation=1,
    )

    class Sessions:
        async def get(self, session_id):
            assert session_id == handle.session_id
            return SimpleNamespace(handle=handle, page=object())

    class Service:
        _sessions = Sessions()

        async def prepare_session(self, principal):
            assert principal == "alpha"
            # ApplicationService exposes SessionView here, which deliberately
            # has no page_id; the headed helper must retrieve its private handle.
            return SimpleNamespace(session_id=handle.session_id)

    prepared, managed = asyncio.run(_prepare_target_session(Service()))

    assert prepared == handle
    assert managed.handle == handle
