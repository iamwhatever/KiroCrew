"""Assistant-role system notices the gateway injects into a slot's feed.

Status reports -- the auto-compaction notices and the session-reload
confirmation -- not real turns. Every scan that walks for "the last real
message" (the conversation floor, the sidebar preview, backfill replay) must
skip them, and the frontend keeps a twin of this set
(``website/src/lib/systemNotice.ts``): a kind skipped on one side but not the
other leaves the sidebar showing notice boilerplate while the chat pane shows
the real turn, or vice versa.
"""

import re

from kiro_crew.preview_text import drop_format_chars

SESSION_RELOAD_KIND = "session_reload"

SYSTEM_NOTICE_KINDS: frozenset[str] = frozenset({"compaction", SESSION_RELOAD_KIND})


def is_system_notice(role: object, meta: object) -> bool:
    """True for an assistant-role system notice row.

    ``meta`` is checked with ``isinstance`` rather than the ``or {}`` idiom:
    ``append``'s ``meta: dict | None`` is not enforced at runtime, so a truthy
    non-dict would raise ``AttributeError`` on ``.get``.
    """
    return (
        role == "assistant" and isinstance(meta, dict) and meta.get("kind") in SYSTEM_NOTICE_KINDS
    )


#: The injected workflow-completion envelope (``workflow_inject.py``) is written
#: under the assistant role, like the system notices above, and is likewise
#: status rather than speech -- but only when its HEADER parses. The frontend
#: (``WorkflowCompletionCard.tsx``, ``WF_COMPLETION_RE``) deliberately draws a
#: malformed envelope as visible markdown rather than swallowing it, so a
#: prefix-only test here would hide from the roster a row the chat shows. Same
#: header shape as the frontend regex; ``test/fixtures/crewmate_speech_rows.json``
#: pins the two twins to one verdict per row.
WORKFLOW_COMPLETION_PREFIX = "[Workflow completion event]"
WORKFLOW_COMPLETION_HEADER_RE = re.compile(
    r"^\[Workflow completion event\]\s*\nWorkflow `[^`]+` \(wf_[A-Za-z0-9_]+\) → \*\*[a-z]+\*\*"
)

#: Roles whose text is something a person or the agent SAID to the other.
SPEECH_ROLES: frozenset[str] = frozenset({"user", "assistant"})


def is_speech_row(role: object, content: object, meta: object) -> bool:
    """True for a row that is speech: a user or assistant row with visible text
    that is neither a system notice nor a well-formed workflow-completion
    envelope.

    The one backend spelling of what a crew member's chat DRAWS (crew-mode.md,
    "A crewmate's chat"; the frontend twin is ``isCrewmateSpeech`` in
    ``website/src/components/chat/crewmateBubbles.ts``). The Crew Members
    roster preview reads it so the row beside the chat quotes what the chat
    shows -- a compaction summary or a workflow envelope would otherwise
    overwrite the last thing the member said while the chat hides it.
    """
    if role not in SPEECH_ROLES:
        return False
    if is_system_notice(role, meta):
        return False
    # The say-nothing reply a quiet patrol ends on is a bare U+200B: format
    # characters only, truthy as a string, invisible on screen. Not speech.
    if not (isinstance(content, str) and drop_format_chars(content).strip()):
        return False
    return not (
        isinstance(content, str)
        and content.startswith(WORKFLOW_COMPLETION_PREFIX)
        and WORKFLOW_COMPLETION_HEADER_RE.match(content) is not None
    )
