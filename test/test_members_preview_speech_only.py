"""The Crew Members roster previews SPEECH only.

A member's chat draws only what the member says (crew-mode.md, "A crewmate's
chat"), so the roster row beside it must quote the same thing. A patroller
whose newest rows are an auto-nudge turn, a shell call and a say-nothing reply
would otherwise sit under a row quoting `gh issue list …` while its chat says
it has not spoken yet -- the blind reader concluded the conversation was lost.

Two paths produce that preview and both are pinned here: the cold roster read
(`ConversationLog.last_message_info(speech_only=True)`) and the live
`member/message` projection, which must keep the last thing SAID when a
machinery row bumps recency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.system_notices import is_speech_row
from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
from kiro_crew.eventlog.service import get_service, set_service
from kiro_crew.eventlog.types import Event
from kiro_crew.history import ConversationLog

KEY = "dashboard:member-radar"


def _patrol_log(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    log.append(KEY, "user", "Anything from the weekend queue?")
    log.append(KEY, "assistant", "Triaged 7 new issues; 2 need your call.")
    log.append(KEY, "nudge", "[auto-nudge cycle 41]\nPatrol the issue queue.")
    log.append(KEY, "tool", "🔧 gh issue list --label needs-triage")
    log.append(KEY, "tool", "✅ done")
    log.append(KEY, "assistant", "\u200b")  # the say-nothing reply
    return ConversationLog(base_dir=tmp_path)  # fresh: no warm cache


class TestRosterPreviewIsSpeechOnly:
    def test_speech_only_skips_machinery_and_the_empty_reply(self, tmp_path: Path) -> None:
        log = _patrol_log(tmp_path)
        preview, _, stopped = log.last_message_info(KEY, speech_only=True)
        assert preview == "Triaged 7 new issues; 2 need your call."
        assert stopped is False

    def test_default_read_is_unchanged(self, tmp_path: Path) -> None:
        """The Sessions sidebar keeps its preview rule: newest row with text."""
        log = _patrol_log(tmp_path)
        preview, _, _ = log.last_message_info(KEY)
        assert preview.startswith("✅ done")

    def test_recency_still_reads_the_newest_row(self, tmp_path: Path) -> None:
        """A patrol IS activity: the roster orders by the newest row's epoch."""
        log = _patrol_log(tmp_path)
        _, speech_epoch, _ = log.last_message_info(KEY, speech_only=True)
        _, default_epoch, _ = log.last_message_info(KEY)
        # The speech-only walk records the newest row's epoch on the very first
        # skipped row (the say-nothing reply); the default walk only reaches the
        # "✅ done" row, one row older. Newer or equal, never older.
        assert speech_epoch >= default_epoch

    def test_assistant_role_status_rows_are_not_speech(self, tmp_path: Path) -> None:
        """A compaction notice and a workflow envelope ride the assistant role
        but are status; the roster must not quote them over the last real reply."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "assistant", "Triaged 7 new issues.")
        # `append` has no meta kwarg; the gateway's flush writes these rows with
        # `meta` inline, so write them the way the file holds them.
        with open(log._path(KEY), "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "Context summary: …",
                        "ts": "2026-09-22T06:00:00+00:00",
                        "meta": {"kind": "compaction"},
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "[Workflow completion event]\nWorkflow `x` (wf_1) → **finished**",
                        "ts": "2026-09-22T06:00:01+00:00",
                    }
                )
                + "\n"
            )
        fresh = ConversationLog(base_dir=tmp_path)
        preview, _, _ = fresh.last_message_info(KEY, speech_only=True)
        assert preview == "Triaged 7 new issues."

    def test_all_machinery_previews_empty(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "nudge", "[auto-nudge cycle 1]\nPatrol.")
        log.append(KEY, "tool", "🔧 gh issue list")
        log.append(KEY, "assistant", "\u200b")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch, _ = fresh.last_message_info(KEY, speech_only=True)
        assert preview == ""
        assert epoch > 0


FIXTURE = Path(__file__).parent / "fixtures" / "crewmate_speech_rows.json"


class TestSpeechTwinsAgree:
    """The backend half of the shared pin: one verdict per fixture row.

    ``website/src/components/chat/crewmateBubbles.test.ts`` reads the same file
    and asserts the frontend twin (``isCrewmateSpeech``) reaches the same
    ``speech`` verdict, so a status kind learned on one side fails the other.
    """

    def test_every_row_matches_the_fixture_verdict(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        assert len(cases) >= 10
        for case in cases:
            if case.get("frontend_only"):
                continue
            got = is_speech_row(case["role"], case["content"], case.get("meta"))
            assert got is case["speech"], case["name"]


def _message_event(data: dict) -> Event:
    return {"type": types.MEMBER_MESSAGE, "seq": 1, "time": 0, "data": data}


class TestLiveProjectionKeepsTheLastThingSaid:
    def test_event_without_preview_bumps_recency_only(self) -> None:
        proj = RosterProjection()
        state = {"last_message": "Triaged 7 new issues.", "last_active_ts": 100.0}
        # A machinery row's event: recency only, no preview key.
        out = proj.apply(state, _message_event({"ts": 200.0}))
        assert out["last_active_ts"] == 200.0
        assert out["last_message"] == "Triaged 7 new issues."

    def test_event_with_preview_replaces_it(self) -> None:
        proj = RosterProjection()
        state = {"last_message": "old", "last_active_ts": 100.0}
        out = proj.apply(state, _message_event({"ts": 200.0, "preview": "new words"}))
        assert out["last_message"] == "new words"
        assert out["last_active_ts"] == 200.0


# ---------------------------------------------------------------------------
# Legacy machinery previews are corrected on the roster read
# ---------------------------------------------------------------------------
CREW = "radar"


@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


class TestLegacyMachineryPreviewIsCorrectedOnRead:
    """A `member/message` event written BEFORE the preview became speech-only
    (or by any writer that skipped `is_speech_row`) folds a tool line into the
    roster's `last_message`. The roster read reconciles the fold against the
    transcript's speech-only answer -- including an explicit EMPTY correction
    for a member that has never spoken -- and appends nothing on a second read.
    """

    @pytest.mark.asyncio
    async def test_never_spoken_patroller_is_corrected_to_blank(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        # The thread: machinery only, never a word to the user.
        state.conversation_log.append(key, "nudge", "[auto-nudge cycle 1]\nPatrol.")
        state.conversation_log.append(key, "tool", "\U0001f527 gh issue list --label needs-triage")
        state.conversation_log.append(key, "assistant", "\u200b")
        # The legacy fold: a pre-speech-only writer stamped the tool line.
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(
            slug,
            types.MEMBER_MESSAGE,
            {"ts": 1.0, "preview": "\U0001f527 gh issue list --label needs-triage"},
        )
        assert svc.snapshot(slug)["values"]["roster"]["last_message"].startswith("\U0001f527")

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == ""
        assert row["projections"]["values"]["roster"]["last_message"] == ""

        seq = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.last_seq(slug) == seq, "preview reconcile is not idempotent"

    @pytest.mark.asyncio
    async def test_stale_preview_is_corrected_to_the_last_speech(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="kirocrew",
            memory_stores={},
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "assistant", "Triaged 7 new issues.")
        state.conversation_log.append(key, "tool", "\U0001f527 gh issue list")
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "\U0001f527 gh issue list"})

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_message"] == "Triaged 7 new issues."
        assert row["projections"]["values"]["roster"]["last_message"] == "Triaged 7 new issues."
