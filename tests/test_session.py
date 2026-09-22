import pytest

from eikocode.providers.base import Message, Role
from eikocode.session import Session


def test_add_user_then_complete_assistant():
    s = Session()
    s.add_user("第一问")
    s.complete_assistant("第一答")

    messages = s.messages()
    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT]
    assert messages[1].content == "第一答"
    assert s.turns == 1


def test_assistant_never_enters_on_empty_reply():
    s = Session()
    s.add_user("提问")
    s.complete_assistant("   ")

    assert s.message_count == 1
    assert s.turns == 1


def test_drop_last_rolls_back():
    s = Session()
    s.add_user("提问")
    s.drop_last()

    assert s.message_count == 0
    assert s.turns == 0


def test_drop_last_on_empty_session_is_a_noop():
    s = Session()
    s.drop_last()
    assert s.message_count == 0


def test_clear_resets_everything():
    s = Session()
    s.add_user("一")
    s.complete_assistant("二")
    s.add_user("三")
    s.clear()

    assert s.message_count == 0
    assert s.turns == 0
    assert s.messages() == ()


def test_turns_counts_user_messages_only():
    s = Session()
    s.add_user("一")
    s.complete_assistant("答一")
    s.add_user("二")

    assert s.turns == 2
    assert s.message_count == 3


def test_messages_returns_a_copy():
    s = Session()
    s.add_user("提问")
    snapshot = list(s.messages())
    snapshot.append(Message(role=Role.USER, content="外部塞进来的"))

    assert s.message_count == 1
