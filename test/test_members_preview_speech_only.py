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

from pathlib import Path

from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
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
        assert speech_epoch == default_epoch

    def test_all_machinery_previews_empty(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append(KEY, "nudge", "[auto-nudge cycle 1]\nPatrol.")
        log.append(KEY, "tool", "🔧 gh issue list")
        log.append(KEY, "assistant", "\u200b")
        fresh = ConversationLog(base_dir=tmp_path)
        preview, epoch, _ = fresh.last_message_info(KEY, speech_only=True)
        assert preview == ""
        assert epoch > 0


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
