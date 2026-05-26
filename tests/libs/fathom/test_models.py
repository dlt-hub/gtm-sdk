"""Pydantic model tests for libs/fathom/models.py.

The redacted fixture at ``api/samples/fathom.recording.redacted.json`` has
zero ``action_items``, so the ``Assignee`` shape was never exercised by
the existing parse tests. Real Fathom recording webhooks routinely emit
action items with ``assignee = {"email": null, "name": null, "team":
null}`` when Fathom cannot attribute the action to a calendar invitee —
ad-hoc tasks the AI surfaces without an explicit owner.

These tests pin the null-assignee shape so a future "tighten the model"
change can't silently reintroduce the ValidationError that blocked live
recordings.
"""

from __future__ import annotations

from libs.fathom.models import ActionItem, Assignee, Webhook


def test_assignee_accepts_all_null_fields() -> None:
    a = Assignee(email=None, name=None, team=None)
    assert a.name is None
    assert a.email is None
    assert a.team is None


def test_action_item_with_null_assignee_name_parses() -> None:
    item = ActionItem.model_validate(
        {
            "assignee": {"email": None, "name": None, "team": None},
            "completed": False,
            "description": "Follow up with the team",
            "recording_playback_url": "https://fathom.video/calls/1?timestamp=10.0",
            "recording_timestamp": "00:00:10",
            "user_generated": False,
        },
    )
    assert item.assignee.name is None


def test_webhook_validates_payload_with_unassigned_action_items() -> None:
    """Smoke test of the full webhook shape with null-assignee action items.

    Mirrors the live Fathom delivery that exposed the bug: a fully populated
    recording payload whose action items have ``name = null``.
    """
    payload = {
        "action_items": [
            {
                "assignee": {"email": None, "name": None, "team": None},
                "completed": False,
                "description": "Unassigned follow-up",
                "recording_playback_url": "https://fathom.video/calls/1?t=1",
                "recording_timestamp": "00:00:01",
                "user_generated": False,
            },
        ],
        "calendar_invitees": [],
        "calendar_invitees_domains_type": "only_internal",
        "created_at": "2026-05-20T15:15:51Z",
        "crm_matches": {"companies": [], "contacts": [], "deals": []},
        "default_summary": {"markdown_formatted": "", "template_name": "General"},
        "meeting_title": "Smoke test",
        "recorded_by": {
            "email": "host@dlthub.com",
            "email_domain": "dlthub.com",
            "name": "Host",
            "team": "GTM",
        },
        "recording_end_time": "2026-05-20T15:15:42Z",
        "recording_id": 1,
        "recording_start_time": "2026-05-20T15:01:17Z",
        "scheduled_end_time": "2026-05-20T15:15:00Z",
        "scheduled_start_time": "2026-05-20T15:00:00Z",
        "share_url": "https://fathom.video/share/test",
        "title": "Smoke test",
        "transcript": [],
        "transcript_language": "en",
        "url": "https://fathom.video/calls/1",
    }
    w = Webhook.model_validate(payload)
    assert w.action_items is not None
    assert len(w.action_items) == 1
    assert w.action_items[0].assignee.name is None
