"""Adoption and release: the records that MOVE a session in the tree.

The creating edge is stamped once and never rewritten, so every property here is about
a second kind of statement living beside it. Four things can break independently and
each gets its own test:

* the FOLD prefers the newest decision over the creating citation, and over an older
  decision, whatever order the records arrive in;
* the fold's cycle colouring still runs over the applied decisions, because a takeover
  can be recorded against a reading of the tree that has since moved;
* the PROJECTION advances only after the append succeeded, refuses a decision that
  arrives out of order, and hands out the same object when nothing moved;
* a COLD scan with no checkpoint reaches the same tree as the fold, which is the whole
  claim that lets the checkpoint be a shortcut rather than an authority.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import (
    EdgeRecord,
    OpenedRecord,
    SessionTree,
    edge_record,
    fold_tree,
    latest_edges,
)
from kiro_crew.crew_log.session_tree_projection import (
    CHECKPOINT_NAME,
    SessionTreeProjection,
)
from kiro_crew.crew_log.store import read_last_tree_edge
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, and no write is armed on the real pool.

    Same shape as the projection suite's fixture and for the same two reasons: the
    process-wide projection is bound to ONE store, so a test inheriting another test's
    fold would read another store's records; and ``_debounced_write`` saves outside the
    lock, so a worker that already passed the epoch check could land a file in this
    test's tmp home after pytest considers it finished.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()


def _rec(sid: str, slot: str, created: int = 1, parent: str | None = None) -> OpenedRecord:
    return OpenedRecord(sid=sid, slot=slot, created_at=created, parent_slot=parent)


def _parents(nodes) -> dict[str, str | None]:
    return {slot: node.parent_slot for slot, node in nodes.items()}


def _log(sid: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)


def _opened(handle: CrewLog, slot: str, *, parent: str | None = None) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent is not None:
        data["parent"] = {"slot": parent}
    handle.append("session/opened", data, src=GATEWAY)


def _checkpoint_path():
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME


# ── the fold ───────────────────────────────────────────────────────────────


def test_an_adoption_replaces_the_creating_citation_and_carries_the_subtree():
    """The takeover case, which is the whole feature: one record moves a branch.

    D adopts A, and B and C follow with no record of their own -- they cite A's SLOT,
    so nothing about them has to change for the tree under them to move. A fold that
    keyed children on a PATH would need an entry per descendant here.
    """
    records = [
        _rec("s-a", "A", created=1),
        _rec("s-b", "B", created=2, parent="A"),
        _rec("s-c", "C", created=3, parent="B"),
        _rec("s-d", "D", created=4),
    ]
    adopted = fold_tree(records, [EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a")])
    assert _parents(adopted) == {"A": "D", "B": "A", "C": "B", "D": None}


def test_a_release_clears_the_edge_that_the_opening_entry_recorded():
    """The one record that RETRACTS a parent.

    B was opened by A, and the release says it has none now -- which a
    ``session/opened`` carrying no parent deliberately cannot say, because that only
    means the entry did not repeat a creator.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-b", "B", created=2, parent="A")]
    released = fold_tree(records, [EdgeRecord(slot="B", parent_slot=None, at=50, sid="s-b")])
    assert _parents(released) == {"A": None, "B": None}


def test_the_newest_decision_wins_whatever_order_the_records_arrive_in():
    """Order-independence, which is the property a stale checkpoint plus a replayed
    tail actually needs.

    The release is newer than the adoption, so the answer is "no parent" whether the
    fold sees it first or second. Taking the LAST arrival instead would put the session
    back under a parent that already let it go, and it would stay there until the
    process restarted.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-d", "D", created=2)]
    adopt = EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a")
    release = EdgeRecord(slot="A", parent_slot=None, at=200, sid="s-a")
    assert _parents(fold_tree(records, [adopt, release]))["A"] is None
    assert _parents(fold_tree(records, [release, adopt]))["A"] is None
    # And the reverse pairing: an adoption that came after a release still wins.
    later_adopt = EdgeRecord(slot="A", parent_slot="D", at=300, sid="s-a")
    assert _parents(fold_tree(records, [later_adopt, release]))["A"] == "D"


def test_two_decisions_in_one_millisecond_are_ordered_by_the_citing_log():
    """The tie-break, so two runs over the same records agree.

    Wall-clock milliseconds collide, and a fold whose answer depended on input order
    would flip between two scans of the same files.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-d", "D", created=2)]
    same_ms = [
        EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a1"),
        EdgeRecord(slot="A", parent_slot=None, at=100, sid="s-a2"),
    ]
    assert _parents(fold_tree(records, same_ms))["A"] is None
    assert _parents(fold_tree(records, list(reversed(same_ms))))["A"] is None
    assert latest_edges(same_ms)["A"].sid == "s-a2"


def test_an_adoption_that_would_close_a_loop_is_coloured_as_a_cycle_by_the_fold():
    """The fold-time guard, which is not the same guard as the tool's.

    The tool refuses a target already above the caller, reading the tree as it stands
    then. This is the case that reading cannot cover: a decision reaching the fold from
    a checkpoint and a replayed tail, in an order no writer saw. Every slot on the loop
    is marked, so a consumer nests none of them rather than rendering a branch that
    eats itself.
    """
    records = [
        _rec("s-a", "A", created=1),
        _rec("s-b", "B", created=2, parent="A"),
        _rec("s-c", "C", created=3, parent="B"),
    ]
    nodes = fold_tree(records, [EdgeRecord(slot="A", parent_slot="C", at=100, sid="s-a")])
    assert sorted(slot for slot, node in nodes.items() if node.cycle) == ["A", "B", "C"]


def test_a_decision_for_a_slot_with_no_log_changes_nothing():
    """An edge onto a node that does not exist is dropped, the same rule the fold
    already applies to a cited creator with no log of its own."""
    records = [_rec("s-a", "A", created=1)]
    ghost = EdgeRecord(slot="GHOST", parent_slot="A", at=100, sid="s-ghost")
    assert _parents(fold_tree(records, [ghost])) == {"A": None}


def test_an_adoption_naming_a_parent_with_no_log_keeps_the_citation_unfollowed():
    """The citation is the child's record and is retained; only the FOLLOWING stops.

    Same posture as an opened entry citing a creator that has no log: the reader is
    told what the session says about itself, and the tree does not invent a node.
    """
    records = [_rec("s-a", "A", created=1)]
    nodes = fold_tree(records, [EdgeRecord(slot="A", parent_slot="GONE", at=100, sid="s-a")])
    assert nodes["A"].parent_slot == "GONE"
    assert nodes["A"].cycle is False


# ── the record builder ─────────────────────────────────────────────────────


def _entry(entry_type: str, data: dict, *, at: int = 7) -> object:
    from kiro_crew.crew_log.schema import Entry

    return Entry(type=entry_type, data=data, src=GATEWAY, seq=2, time=at)


def test_an_adoption_with_no_parent_slot_is_refused_rather_than_read_as_a_release():
    """The strongest possible meaning is exactly what a malformed entry must not get.

    A release detaches a subtree. Inferring one from an adoption whose ``parent`` did
    not survive would do that on the strength of damage.
    """
    assert edge_record("A", "s-a", _entry("session/adopted", {})) is None
    assert edge_record("A", "s-a", _entry("session/adopted", {"parent": {}})) is None
    assert edge_record("A", "s-a", _entry("session/adopted", {"parent": "D"})) is None


def test_a_release_needs_no_parent_and_folds_as_no_parent():
    built = edge_record("A", "s-a", _entry("session/released", {}))
    assert built is not None and built.parent_slot is None and built.at == 7


def test_an_over_long_key_is_refused_rather_than_truncated():
    """A truncated slot key is a DIFFERENT key: it matches nothing, or it matches
    another session."""
    from kiro_crew.validation import MAX_SHORT_STRING

    long_slot = "x" * (MAX_SHORT_STRING + 1)
    assert edge_record(long_slot, "s-a", _entry("session/released", {})) is None
    adopt = _entry("session/adopted", {"parent": {"slot": long_slot}})
    assert edge_record("A", "s-a", adopt) is None


def test_an_entry_of_another_type_contributes_no_decision():
    assert edge_record("A", "s-a", _entry("turn/started", {"turn": 1})) is None
    assert edge_record("A", "s-a", None) is None


# ── the projection ─────────────────────────────────────────────────────────


def test_the_projection_folds_a_decision_and_keeps_the_same_object_when_it_repeats():
    """dsh's same-reference rule, extended to decisions.

    A decision already held is not a change, so a replay of a tail the checkpoint
    already covered costs neither a re-render nor a checkpoint write.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    before = proj.nodes()
    edge = EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a")
    proj.apply_edge(edge)
    after = proj.nodes()
    assert after["A"].parent_slot == "D"
    assert after is not before
    proj.apply_edge(edge)
    assert proj.nodes() is after


def test_the_projection_refuses_a_decision_that_arrives_out_of_order():
    """A replay walks units in directory order, so an older decision can arrive last.

    Taking the last arrival would re-parent a session that has already been released,
    with nothing to correct it until the process restarted.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot=None, at=200, sid="s-a"))
    stale = proj.nodes()
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.nodes() is stale
    assert proj.nodes()["A"].parent_slot is None


def test_a_checkpoint_round_trip_keeps_the_decisions():
    """The checkpoint is a shortcut, and a shortcut that lost adoptions would be an
    authority for the wrong answer: it would load as "nothing was ever adopted".

    Real units on disk, because the replay drops a held record whose unit is gone --
    so a checkpoint of hand-built records would be reconciled away before the decision
    could be asserted, and the test would pass or fail for the wrong reason.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.flush_checkpoint() is True
    payload = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
    assert payload["edges"] == [{"slot": "A", "at": 100, "sid": "s-a", "parent": "D"}]

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["A"].parent_slot == "D"


def test_a_checkpoint_with_no_edges_key_is_discarded_rather_than_read_as_no_adoptions():
    """The one wrong answer that looks exactly like a right one.

    This build writes the key whether or not anything was adopted, so a payload that
    lacks it is damaged rather than old -- and reading it as "no adoptions" would serve
    a tree with a takeover silently missing.
    """
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.flush_checkpoint() is True
    path = _checkpoint_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("edges")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert stp._load_checkpoint() is None


def test_forgetting_a_unit_drops_the_decisions_it_recorded():
    """A unit that is gone is no longer evidence for where its slot hangs."""
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.nodes()["A"].parent_slot == "D"
    proj.forget("s-a")
    assert "A" not in proj.nodes()


# ── the store read, and cold-scan parity ───────────────────────────────────


def _unit_with_decision(sid: str, slot: str, *, parent: str | None, adopt_to: str | None):
    """A real unit on disk whose decision sits PAST its head lines.

    The turns in between are the point: a reader of the head cannot see the decision,
    which is why the store grew a tail read for it.
    """
    handle = _log(sid, slot)
    _opened(handle, slot, parent=parent)
    for turn in range(3):
        handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    if adopt_to is None:
        handle.append("session/released", {}, src=GATEWAY)
    else:
        handle.append("session/adopted", {"parent": {"slot": adopt_to}}, src=GATEWAY)
    del handle


def test_the_store_reads_a_decision_that_sits_behind_later_entries():
    """The head read cannot see it, so the tail read must."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)
    entry = read_last_tree_edge(segment)
    assert entry is not None and entry.type == "session/adopted"
    assert entry.data["parent"]["slot"] == "D"


def test_the_store_reads_the_newest_decision_when_a_log_holds_two():
    """A session adopted and then released reads as released."""
    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append("session/released", {"previous_parent": {"slot": "D"}}, src=GATEWAY)
    del handle
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/released"


def test_a_decision_is_not_a_lifecycle_entry_so_retention_still_sees_the_unit_as_open():
    """The set that decides open-versus-closed authorizes a DELETE, so an adoption must
    not be in it: a unit whose newest such entry was an adoption would never expire.
    """
    assert "session/adopted" not in crew_store._LIFECYCLE_TYPES
    assert "session/released" not in crew_store._LIFECYCLE_TYPES
    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    tail = crew_store._scan_tail(crew_store.newest_segment(directory))
    assert crew_store._last_lifecycle_entry(tail).type == "session/closed"


def test_a_cold_scan_with_no_checkpoint_reaches_the_same_tree_as_the_fold():
    """The claim that lets the checkpoint be a shortcut rather than an authority.

    Written to disk, then read by a scanner that has never seen a checkpoint -- with
    the decisions sitting past each unit's head lines, which is the case a head-only
    scan gets wrong.
    """
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    _unit_with_decision("s-b", "B", parent="A", adopt_to=None)
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    scanned = SessionTree().reading(with_edges=True)
    assert _parents(scanned.nodes) == {"A": "D", "B": None, "D": None}

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert _parents(proj.nodes()) == _parents(scanned.nodes)


def test_a_scan_without_edges_reports_where_each_slot_was_opened():
    """The default is the cheaper read and a DIFFERENT question, so it is pinned
    rather than left to be discovered by a caller that wanted the other one."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle
    assert _parents(SessionTree().reading().nodes) == {"A": None, "D": None}


def test_a_stale_checkpoint_is_corrected_by_the_replay_for_a_unit_it_already_holds():
    """The reason the decision pass covers held names and not only new ones.

    A decision lives at the END of a log, so the unit a checkpoint already holds is
    exactly where a decision it missed will be. A replay that skipped those names would
    make the checkpoint authoritative for adoptions, and a stale one would leave a
    session hanging under a parent that released it.
    """
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    # A checkpoint that knows both units and NO decisions -- the shape a process that
    # died between the adopt append and the debounced write leaves behind. Written by
    # hand, because a projection seeded from THIS disk would have read the decision
    # already and its checkpoint would not be stale.
    seeded = SessionTreeProjection()
    seeded.ensure_seeded()
    assert seeded.flush_checkpoint() is True
    path = _checkpoint_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["edges"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    stp.reset_for_tests()

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["A"].parent_slot == "D"


def test_a_removed_unit_takes_its_decision_out_of_the_replay():
    """The same rule ``forget`` applies, on the cold path."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle
    first = SessionTreeProjection()
    first.ensure_seeded()
    assert first.nodes()["A"].parent_slot == "D"
    assert first.flush_checkpoint() is True

    crew_store.remove_unit(lg.KIND_SESSION, "s-a", guard=lambda _directory: True)
    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert "A" not in revived.nodes()


# ── the emitter ────────────────────────────────────────────────────────────


def test_the_emitter_writes_the_entry_and_advances_the_fold():
    """Durability first, then memory: the fold is advanced from inside the job, after
    the append returned -- so the disk can never hold a decision the memory lacks."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_opened(
        "s-d", agent="kirocrew", slot="D", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted("s-a", slot="A", parent_slot="D", parent_sid="s-d")
    assert emit.flush(timeout=5.0) is True

    assert stp.projection().nodes()["A"].parent_slot == "D"
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/adopted"
    assert entry.data == {"parent": {"slot": "D", "sid": "s-d"}}


def test_the_emitter_records_the_parent_a_takeover_replaced():
    """``previous_parent`` is audit and the fold ignores it, so the fold is asserted to
    show only the NEW parent while the entry keeps both."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted(
        "s-a",
        slot="A",
        parent_slot="NEW",
        previous_parent_slot="OLD",
        previous_parent_sid="s-old",
    )
    assert emit.flush(timeout=5.0) is True
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry.data["previous_parent"] == {"slot": "OLD", "sid": "s-old"}
    assert stp.projection().nodes()["A"].parent_slot == "NEW"


def test_a_release_written_by_the_emitter_returns_the_session_to_a_root():
    emit.on_session_opened(
        "s-d", agent="kirocrew", slot="D", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_opened(
        "s-a",
        agent="kirocrew",
        slot="A",
        model="opus",
        cwd="/w",
        owner="raymond",
        parent_slot="D",
    )
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().nodes()["A"].parent_slot == "D"

    emit.on_session_released("s-a", slot="A", previous_parent_slot="D", previous_parent_sid="s-d")
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().nodes()["A"].parent_slot is None
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry.data == {"previous_parent": {"slot": "D", "sid": "s-d"}}


def test_the_emitter_writes_nothing_without_both_sides_of_the_edge():
    """A decision with no slot names nothing a reader could fold, so it is not
    written -- rather than written and silently ignored."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted("s-a", slot="", parent_slot="D")
    emit.on_session_adopted("s-a", slot="A", parent_slot="")
    emit.on_session_released("s-a", slot="")
    assert emit.flush(timeout=5.0) is True
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    assert read_last_tree_edge(crew_store.newest_segment(directory)) is None
    assert _store_name("s-a") == directory.name
