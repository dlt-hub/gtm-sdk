from __future__ import annotations

from libs.fathom.models import ActionItem, Assignee, CalendarInvitee
from src.fathom.utils import (
    build_meeting_description,
    fathom_summary_title,
    render_action_items_markdown,
    select_note_parent_email,
)


def _invitee(*, email: str, is_external: bool) -> CalendarInvitee:
    return CalendarInvitee(
        email=email,
        email_domain=email.split("@", 1)[-1],
        is_external=is_external,
        matched_speaker_display_name=None,
        name=email.split("@", 1)[0],
    )


def _item(
    *,
    name: str = "Alex",
    email: str | None = None,
    description: str = "Send deck",
    completed: bool = False,
    playback_url: str = "https://fathom.video/calls/1/?t=754",
    timestamp: str = "12:34",
) -> ActionItem:
    return ActionItem(
        assignee=Assignee(name=name, email=email, team=None),
        completed=completed,
        description=description,
        recording_playback_url=playback_url,
        recording_timestamp=timestamp,
        user_generated=False,
    )


def test_summary_title_with_template_name() -> None:
    assert fathom_summary_title("General") == "Fathom summary — General"


def test_summary_title_empty_template_falls_back() -> None:
    assert fathom_summary_title("") == "Fathom summary"


def test_summary_title_none_template_falls_back() -> None:
    assert fathom_summary_title(None) == "Fathom summary"


def test_summary_title_strips_whitespace() -> None:
    assert fathom_summary_title("   ") == "Fathom summary"


def test_summary_title_preserves_internal_whitespace() -> None:
    assert fathom_summary_title("Sales Discovery") == "Fathom summary — Sales Discovery"


def test_action_items_renders_unchecked_box() -> None:
    rendered = render_action_items_markdown([_item(completed=False)])
    assert rendered.startswith("- [ ] **Alex** — Send deck")


def test_action_items_renders_checked_box() -> None:
    rendered = render_action_items_markdown([_item(completed=True)])
    assert rendered.startswith("- [x] **Alex** — Send deck")


def test_action_items_appends_email_when_present() -> None:
    rendered = render_action_items_markdown([_item(email="alex@x.com")])
    assert "**Alex** (alex@x.com)" in rendered


def test_action_items_appends_playback_link_when_valid() -> None:
    rendered = render_action_items_markdown(
        [_item(playback_url="https://fathom.video/calls/1/?t=754", timestamp="12:34")],
    )
    assert rendered.endswith(" [▶ 12:34](https://fathom.video/calls/1/?t=754)")


def test_action_items_skips_link_for_non_https_url() -> None:
    rendered = render_action_items_markdown(
        [_item(playback_url="http://fathom.video/calls/1/?t=754", timestamp="12:34")],
    )
    assert "[▶" not in rendered


def test_action_items_skips_link_for_bad_timestamp() -> None:
    rendered = render_action_items_markdown(
        [_item(playback_url="https://fathom.video/calls/1/?t=754", timestamp="abc")],
    )
    assert "[▶" not in rendered


def test_action_items_omits_blank_items() -> None:
    rendered = render_action_items_markdown(
        [
            _item(name="Alex", description="Send deck"),
            _item(name="", description=""),
            _item(name="Sarah", description="Confirm budget"),
        ],
    )
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert "**Alex**" in lines[0]
    assert "**Sarah**" in lines[1]


def test_action_items_empty_list_returns_empty_string() -> None:
    assert render_action_items_markdown([]) == ""


def test_build_meeting_description_with_summary_and_footer() -> None:
    desc = build_meeting_description(
        summary_markdown="## Summary\nWent well.",
        fallback_title="Internal sync",
        recording_url="https://fathom.video/calls/123",
        recording_id=123,
        transcript_language="en",
    )
    assert desc.startswith("## Summary\nWent well.")
    assert "\n\n---\n" in desc
    assert "🎥 [Watch the Fathom recording](https://fathom.video/calls/123)" in desc
    assert "Fathom recording #123" in desc
    assert "language: en" in desc


def test_build_meeting_description_falls_back_to_title() -> None:
    desc = build_meeting_description(
        summary_markdown=None,
        fallback_title="Internal sync",
        recording_url="https://fathom.video/calls/123",
        recording_id=123,
        transcript_language="en",
    )
    assert desc.startswith("Internal sync")
    assert "Fathom recording #123" in desc


def test_build_meeting_description_blank_summary_uses_title() -> None:
    desc = build_meeting_description(
        summary_markdown="   ",
        fallback_title="Internal sync",
        recording_url=None,
        recording_id=123,
        transcript_language=None,
    )
    assert desc.startswith("Internal sync")
    # No https url → no watch link, but the recording id still anchors it.
    assert "🎥" not in desc
    assert "Fathom recording #123" in desc


def test_select_note_parent_prefers_first_external_invitee() -> None:
    invitees = [
        _invitee(email="host@dlthub.com", is_external=False),
        _invitee(email="buyer@acme.com", is_external=True),
        _invitee(email="other@globex.com", is_external=True),
    ]
    email = select_note_parent_email(
        calendar_invitees=invitees,
        participant_emails=[i.email for i in invitees],
        recorder_email="host@dlthub.com",
    )
    assert email == "buyer@acme.com"


def test_select_note_parent_falls_back_to_recorder_when_no_external() -> None:
    invitees = [_invitee(email="host@dlthub.com", is_external=False)]
    email = select_note_parent_email(
        calendar_invitees=invitees,
        participant_emails=[i.email for i in invitees],
        recorder_email="host@dlthub.com",
    )
    assert email == "host@dlthub.com"


def test_select_note_parent_falls_back_to_recorder_with_no_invitees() -> None:
    # No invitees → participants is just the recorder (the /v2/meetings fallback).
    email = select_note_parent_email(
        calendar_invitees=[],
        participant_emails=["host@dlthub.com"],
        recorder_email="host@dlthub.com",
    )
    assert email == "host@dlthub.com"


def test_select_note_parent_uses_participant_when_recorder_absent() -> None:
    # ai-gez regression (roborev): invitees present, none external, and the
    # recorder is NOT among them. The recorder is therefore not a meeting
    # participant and won't be auto-created — so the parent must be a real
    # participant (the first invitee), never the unresolvable recorder.
    invitees = [
        _invitee(email="teammate@dlthub.com", is_external=False),
        _invitee(email="other@dlthub.com", is_external=False),
    ]
    email = select_note_parent_email(
        calendar_invitees=invitees,
        participant_emails=[i.email for i in invitees],
        recorder_email="absent-recorder@dlthub.com",
    )
    assert email == "teammate@dlthub.com"
