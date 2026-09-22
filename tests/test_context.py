import pytest

from eikocode.context import (
    BLOCK_RATIO,
    WARN_RATIO,
    Level,
    estimate_messages,
    estimate_tokens,
    level_for,
    ratio,
)
from eikocode.providers.base import Message, Role


def test_estimate_tokens_treats_wide_chars_as_one_token():
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("") == 0


def test_estimate_tokens_bundles_narrow_chars():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_estimate_messages_adds_per_message_overhead():
    messages = (Message(role=Role.USER, content="abcd"),)
    assert estimate_messages(messages) == 1 + 4


def test_ratio_guard_against_zero_limit():
    assert ratio(10, 0) == 1.0
    assert ratio(10, 100) == 0.1


def test_level_thresholds():
    assert level_for(0, 100) is Level.OK
    assert level_for(79, 100) is Level.OK
    assert level_for(80, 100) is Level.WARN
    assert level_for(94, 100) is Level.WARN
    assert level_for(95, 100) is Level.BLOCK
    assert level_for(100, 100) is Level.BLOCK


def test_thresholds_match_documented_ratios():
    assert WARN_RATIO == 0.80
    assert BLOCK_RATIO == 0.95


def test_level_is_ordinal():
    assert Level.OK < Level.WARN < Level.BLOCK
