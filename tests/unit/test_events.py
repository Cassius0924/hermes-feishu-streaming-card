import pytest

from hermes_feishu_card.events import EventValidationError, SidecarEvent


def valid_payload(event="thinking.delta", sequence=2):
    return {
        "schema_version": "1",
        "event": event,
        "conversation_id": "chat-1",
        "message_id": "msg-1",
        "chat_id": "oc_abc",
        "platform": "feishu",
        "sequence": sequence,
        "created_at": 1777017600.0,
        "data": {"text": "我在分析。"},
    }


def test_parses_valid_event():
    event = SidecarEvent.from_dict(valid_payload())
    assert event.event == "thinking.delta"
    assert event.sequence == 2


def test_parses_optional_turn_id_and_exposes_canonical_turn_id():
    payload = valid_payload(event="answer.delta", sequence=1)
    payload["data"] = {"text": "x"}
    payload["message_id"] = "om_anchor"
    payload["turn_id"] = "  om_turn  "

    event = SidecarEvent.from_dict(payload)

    assert event.turn_id == "om_turn"
    assert event.canonical_turn_id == "om_turn"


def test_missing_turn_id_falls_back_to_message_id():
    event = SidecarEvent.from_dict(valid_payload(event="answer.delta", sequence=1))

    assert event.turn_id == ""
    assert event.canonical_turn_id == event.message_id


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_rejects_non_string_turn_id(value):
    payload = valid_payload()
    payload["turn_id"] = value

    with pytest.raises(EventValidationError, match="turn_id"):
        SidecarEvent.from_dict(payload)


def test_event_exposes_optional_exact_display_status():
    payload = valid_payload(event="message.completed")
    payload["data"] = {"answer": "稍后继续", "display_status": "in_progress"}

    event = SidecarEvent.from_dict(payload)

    assert event.display_status == "in_progress"


@pytest.mark.parametrize("value", [None, "", "running", "COMPLETED", " completed "])
def test_event_ignores_invalid_optional_display_status(value):
    payload = valid_payload(event="message.completed")
    payload["data"] = {"answer": "最终答案", "display_status": value}

    event = SidecarEvent.from_dict(payload)

    assert event.display_status == ""


@pytest.mark.parametrize(
    "event_name",
    ["interaction.requested", "interaction.completed", "interaction.failed"],
)
def test_parses_interaction_events(event_name):
    payload = valid_payload(event=event_name)
    payload["data"] = {
        "interaction_id": "approval-1",
        "kind": "approval",
        "prompt": "允许执行命令吗？",
    }

    event = SidecarEvent.from_dict(payload)

    assert event.event == event_name
    assert event.data["interaction_id"] == "approval-1"


def test_parses_system_notice_event():
    payload = valid_payload(event="system.notice")
    payload["data"] = {
        "title": "上下文窗口提示",
        "content": "Codex gpt-5.5 caps context at 272K.",
        "level": "info",
        "notice_id": "context-cap",
    }

    event = SidecarEvent.from_dict(payload)

    assert event.event == "system.notice"
    assert event.data["notice_id"] == "context-cap"


def test_rejects_unknown_event_name():
    with pytest.raises(EventValidationError, match="unknown event"):
        SidecarEvent.from_dict(valid_payload(event="bad.event"))


@pytest.mark.parametrize("event", [[], "", {}, "   "])
def test_rejects_invalid_event_name_type(event):
    with pytest.raises(EventValidationError, match="event"):
        SidecarEvent.from_dict(valid_payload(event=event))


def test_rejects_missing_chat_id():
    payload = valid_payload()
    del payload["chat_id"]
    with pytest.raises(EventValidationError, match="chat_id"):
        SidecarEvent.from_dict(payload)


def test_rejects_non_feishu_platform():
    payload = valid_payload()
    payload["platform"] = "slack"
    with pytest.raises(EventValidationError, match="platform"):
        SidecarEvent.from_dict(payload)


@pytest.mark.parametrize("sequence", [True, -1, "2"])
def test_rejects_invalid_sequence(sequence):
    payload = valid_payload(sequence=sequence)
    with pytest.raises(EventValidationError, match="sequence"):
        SidecarEvent.from_dict(payload)


def test_rejects_invalid_created_at():
    payload = valid_payload()
    payload["created_at"] = "abc"
    with pytest.raises(EventValidationError, match="created_at"):
        SidecarEvent.from_dict(payload)


@pytest.mark.parametrize("created_at", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_created_at(created_at):
    payload = valid_payload()
    payload["created_at"] = created_at
    with pytest.raises(EventValidationError, match="created_at"):
        SidecarEvent.from_dict(payload)


def test_rejects_non_object_data():
    payload = valid_payload()
    payload["data"] = "not-an-object"
    with pytest.raises(EventValidationError, match="data"):
        SidecarEvent.from_dict(payload)


def test_rejects_non_object_payload():
    with pytest.raises(EventValidationError, match="payload must be an object"):
        SidecarEvent.from_dict("not-an-object")


@pytest.mark.parametrize("field", ["conversation_id", "message_id", "chat_id"])
@pytest.mark.parametrize("value", [None, "", "   ", 123])
def test_rejects_invalid_id_fields(field, value):
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(EventValidationError, match=field):
        SidecarEvent.from_dict(payload)


def test_allows_extra_fields():
    payload = valid_payload()
    payload["extra"] = "ignored"
    event = SidecarEvent.from_dict(payload)
    assert event.event == "thinking.delta"


def test_parses_optional_thread_id():
    payload = valid_payload()
    payload["thread_id"] = "omt_thread"

    event = SidecarEvent.from_dict(payload)

    assert event.thread_id == "omt_thread"


def test_event_accepts_optional_group_routing_context():
    payload = valid_payload()
    payload["data"] = {
        "chat_type": "group",
        "tenant_key": "tenant_a",
        "agent_id": "reserved-agent",
        "profile_id": "reserved-profile",
    }

    event = SidecarEvent.from_dict(payload)

    assert event.data["chat_type"] == "group"
    assert event.data["tenant_key"] == "tenant_a"
