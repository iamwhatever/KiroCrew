"""The two verbs that move a session in the tree: ``session_adopt`` / ``session_release``.

Split from the other session-control tests because what they can get wrong is
different. The send and stop verbs act on a target's WORK; these act on where the
sidebar puts it, so the failures worth catching are an authorization that admits the
wrong caller and a record that says the tree moved when it did not.

Every refusal is asserted against the REAL slot objects, the same rule the rest of the
session-control suite follows: the guards read ``memory_mode`` / ``workspace`` / ``_app``
off the production class, and a permissive double would let a dead guard look alive.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """The shipped state (enabled), without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _tree_on(tmp_path, monkeypatch):
    """A recorded tree, folded in memory, with no write armed on the real pool.

    Both verbs refuse outright when the crew log is off -- there would be nowhere to
    record the edge -- so every test here needs the flag on and the projection seeded
    for THIS home. The projection is dropped on both sides because it is bound to one
    store.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    _SIDS.clear()
    yield
    stp.reset_for_tests()
    _SIDS.clear()


#: Session ids per harness state, cleared between tests by the fixture above.
_SIDS: dict[int, dict[str, str]] = {}


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _live(state, slot, sid: str):
    """Map *slot* to an ACP session id, which is the log the entry is written into.

    Through the session map, because that is where the verb reads it: the slot's in-turn
    ACP client is cleared between turns, and the sessions a takeover is aimed at are the
    idle ones. The harness's ``state.sessions`` is a mock, so the lookup is given a real
    dictionary to answer from -- a bare mock returns a mock, which the verb correctly
    refuses as "no session id", and every test here would then pass for the wrong
    reason.
    """
    mapping = _SIDS.setdefault(id(state), {})
    mapping[f"dashboard:{slot.key}"] = sid
    # Assigned on EVERY call, never behind a "has it been set yet" probe: reading an
    # attribute off a mock creates it, so such a probe never answers None and the stub
    # would never be installed at all.
    state.sessions._session_map.mapped_sid.side_effect = lambda key: mapping.get(key, "")
    return slot


def _hold(slot: str, parent: str, *, at: int = 100, sid: str = "") -> None:
    """Put *slot* under *parent* in the fold, as a prior adoption would have."""
    proj = stp.projection()
    proj.apply(OpenedRecord(sid=sid or f"sid-{slot}", slot=slot, created_at=1))
    proj.apply(OpenedRecord(sid=f"sid-{parent}", slot=parent, created_at=2))
    proj.apply_edge(EdgeRecord(slot=slot, parent_slot=parent, at=at, sid=sid or f"sid-{slot}"))


def _run(coro):
    return asyncio.run(coro)


def _emitted(monkeypatch) -> list[dict]:
    """Capture what the verb hands the emitter, instead of draining the writer.

    The emitter's own path is covered where the entry's bytes matter
    (``test_crew_log_session_tree_adopt``). What these tests are about is WHICH call the
    verb makes and with what -- and a refusal must make none at all.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        emit,
        "on_session_adopted",
        lambda sid, **kw: calls.append({"op": "adopt", "sid": sid, **kw}),
    )
    monkeypatch.setattr(
        emit,
        "on_session_released",
        lambda sid, **kw: calls.append({"op": "release", "sid": sid, **kw}),
    )
    return calls


# ── adopt ────────────────────────────────────────────────────────────────────


def test_adopting_records_the_caller_as_the_parent(tmp_path, monkeypatch):
    """The adopter is the CALLER, never an argument: a tool that let one session
    nominate the parent would let it rearrange another session's tree with nothing in
    the record showing which of them asked."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    result = _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert result["target"] == "chat-2"
    assert result["parent"] == "chat-1"
    assert calls == [
        {
            "op": "adopt",
            "sid": "sid-target",
            "slot": "chat-2",
            "parent_slot": "chat-1",
            "parent_sid": "sid-caller",
            "previous_parent_slot": "",
            "previous_parent_sid": "",
        }
    ]


def test_a_takeover_records_the_parent_it_replaced(tmp_path, monkeypatch):
    """The case the verb exists for: one conductor taking over another's workers.

    Adopting a session that already has a parent is ALLOWED, and the parent it had is
    recorded -- so the log says who held the session before, which the fold does not.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-new"), "sid-new")
    _live(state, _slot(state, "chat-worker"), "sid-worker")
    _live(state, _slot(state, "chat-old"), "sid-old")
    _hold("chat-worker", "chat-old", sid="sid-worker")
    result = _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-worker"))
    assert result["previous_parent"] == "chat-old"
    assert calls[0]["previous_parent_slot"] == "chat-old"
    assert calls[0]["previous_parent_sid"] == "sid-old"


def test_adopting_a_session_already_above_the_caller_is_refused(tmp_path, monkeypatch):
    """The loop the tree cannot present.

    A cycle marks every slot on it and nests none of them, so allowing this would
    silently FLATTEN a whole branch rather than produce the takeover the caller asked
    for.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-top"), "sid-top")
    caller = _live(state, _slot(state, "chat-mid"), "sid-mid")
    _hold("chat-mid", "chat-top", sid="sid-mid")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-top"))
    assert excinfo.value.code == "would_cycle"
    assert calls == []


def test_adopting_yourself_is_refused_by_the_shared_gate(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-1"))
    assert excinfo.value.code == "self_target"
    assert calls == []


def test_adopting_a_session_that_is_not_open_is_refused(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-404"))
    assert excinfo.value.code == "target_not_found"
    assert calls == []


def test_an_unattended_caller_cannot_adopt(tmp_path, monkeypatch):
    """A scheduled run rearranging the person's sidebar is exactly the shape the
    unattended refusal exists for."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "workflow-7"), "sid-workflow")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "unattended_caller"
    assert calls == []


def test_adopting_across_a_workspace_is_refused(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2", workspace="other"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "workspace_mismatch"
    assert calls == []


def test_adopting_refuses_when_the_tree_is_not_being_recorded(tmp_path, monkeypatch):
    """The record IS the edge, so a verb whose record cannot be written has done
    nothing -- and says so instead of reporting a success the sidebar will not show."""
    calls = _emitted(monkeypatch)
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_unavailable"
    assert calls == []


def test_adopting_a_session_with_no_live_log_is_refused(tmp_path, monkeypatch):
    """No live ACP session means no log to append the edge to."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_unavailable"
    assert calls == []


# ── release ──────────────────────────────────────────────────────────────────


def test_a_parent_can_release_what_it_holds(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert result["previous_parent"] == "chat-1"
    assert calls == [
        {
            "op": "release",
            "sid": "sid-target",
            "slot": "chat-2",
            "previous_parent_slot": "chat-1",
            "previous_parent_sid": "sid-caller",
        }
    ]


def test_a_session_can_release_itself(tmp_path, monkeypatch):
    """A session taken over must not need its holder's cooperation to get out: a
    conductor that has stopped running would otherwise pin its workers under it."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    caller = _live(state, _slot(state, "chat-child"), "sid-child")
    _hold("chat-child", "chat-parent", sid="sid-child")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert result["previous_parent"] == "chat-parent"
    assert calls[0]["slot"] == "chat-child"


def test_an_agent_created_session_can_still_release_itself(tmp_path, monkeypatch):
    """The ownership fence bounds a caller to what it CREATED, and the session it is
    itself was never another session's to protect. Without the waiver a worker could
    never get out from under a stopped conductor."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    caller = _live(state, _slot(state, "chat-child"), "sid-child")
    caller._created_by = "chat-parent"
    _hold("chat-child", "chat-parent", sid="sid-child")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert result["target"] == "chat-child"
    assert calls[0]["op"] == "release"


def test_a_third_session_cannot_release_someone_elses_child(tmp_path, monkeypatch):
    """Only the holder and the held one, which is what keeps the verb from being a way
    to rearrange a tree the caller has no part in."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-bystander"), "sid-bystander")
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    _live(state, _slot(state, "chat-child"), "sid-child")
    _hold("chat-child", "chat-parent", sid="sid-child")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert excinfo.value.code == "not_parent"
    assert calls == []


def test_releasing_a_session_that_has_no_parent_is_refused(tmp_path, monkeypatch):
    """Nothing to release: writing the entry anyway would put a record of a change into
    a log where nothing changed."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "already_root"
    assert calls == []


def test_releasing_refuses_when_the_tree_is_not_being_recorded(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    # With the log off the fold answers no parent at all, so the earlier refusal is the
    # one that fires. Either way nothing is written, which is what this asserts.
    assert excinfo.value.code in {"tree_unavailable", "already_root"}
    assert calls == []


# ── the tool surface ─────────────────────────────────────────────────────────


def test_both_verbs_are_gated_and_blocked_like_the_other_session_control_tools():
    """Three places must agree on the tool set, and spelling it out per site is how
    ``session_create`` came to be identity-gated and reachable from a channel."""
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, _tool_definitions

    advertised = {d["name"] for d in _tool_definitions()}
    for tool in ("session_adopt", "session_release"):
        assert tool in advertised
        assert tool in SESSION_CONTROL_TOOLS
        assert tool in CHANNEL_AGENT_BLOCKED_TOOLS


def test_neither_verb_is_auto_granted_to_an_unattended_conductor():
    """The grant invariant: a granted verb may CREATE or READ, never MUTATE workspace
    state that already exists and is not the agent's own. An adoption moves where
    another session sits in the person's sidebar, and takes its subtree along."""
    from kiro_crew.agent import _CONDUCTOR_DASHBOARD_GRANTS, _MEMBER_DASHBOARD_GRANTS

    for tool in ("session_adopt", "session_release"):
        assert f"@kirocrew-dashboard/{tool}" not in _CONDUCTOR_DASHBOARD_GRANTS
        assert f"@kirocrew-dashboard/{tool}" not in _MEMBER_DASHBOARD_GRANTS


def test_the_routes_are_registered_and_carry_the_internal_secret():
    """A path missing from the internal-auth set falls through to the general branch,
    which honors only cookie/query tokens -- so the MCP caller's header is ignored and
    the tool is unreachable in production while handler tests still pass."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/adopt" in _STRICT_INTERNAL_API_PATHS
    assert "/api/session-control/release" in _STRICT_INTERNAL_API_PATHS
