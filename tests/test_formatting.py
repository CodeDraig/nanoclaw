"""Tests for router.py and formatting — ported from formatting.test.ts.

Tests XML escaping, message formatting, output formatting,
and trigger pattern gating.  Adapted for Python router.py API.
"""

from __future__ import annotations

import pytest

from nanoclaw.config import ASSISTANT_NAME, TRIGGER_PATTERN
from nanoclaw.router import escape_xml, format_messages, format_outbound
from nanoclaw.types import NewMessage


def make_msg(**overrides) -> NewMessage:
    defaults = {
        "id": "1",
        "chat_id": "-1001234567890",
        "sender": "user-123",
        "sender_name": "Alice",
        "content": "hello",
        "timestamp": "2024-01-01T00:00:00.000Z",
    }
    defaults.update(overrides)
    return NewMessage(**defaults)


# ── escapeXml ─────────────────────────────────────────────────


class TestEscapeXml:
    def test_escapes_ampersands(self):
        assert escape_xml("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert escape_xml("a < b") == "a &lt; b"

    def test_escapes_greater_than(self):
        assert escape_xml("a > b") == "a &gt; b"

    def test_escapes_double_quotes(self):
        assert escape_xml('"hello"') == "&quot;hello&quot;"

    def test_handles_multiple_special_chars(self):
        assert escape_xml('a & b < c > d "e"') == 'a &amp; b &lt; c &gt; d &quot;e&quot;'

    def test_passes_through_normal_strings(self):
        assert escape_xml("hello world") == "hello world"

    def test_handles_empty_string(self):
        assert escape_xml("") == ""


# ── formatMessages ────────────────────────────────────────────


class TestFormatMessages:
    def test_formats_single_message(self):
        result = format_messages([make_msg()])
        assert 'sender="Alice"' in result
        assert ">hello</message>" in result

    def test_formats_multiple_messages(self):
        msgs = [
            make_msg(id="1", sender_name="Alice", content="hi", timestamp="t1"),
            make_msg(id="2", sender_name="Bob", content="hey", timestamp="t2"),
        ]
        result = format_messages(msgs)
        assert 'sender="Alice"' in result
        assert 'sender="Bob"' in result
        assert ">hi</message>" in result
        assert ">hey</message>" in result

    def test_escapes_sender_names(self):
        result = format_messages([make_msg(sender_name="A & B <Co>")])
        assert 'sender="A &amp; B &lt;Co&gt;"' in result

    def test_escapes_content(self):
        result = format_messages([make_msg(content='<script>alert("xss")</script>')])
        assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in result

    def test_handles_empty_array(self):
        result = format_messages([])
        assert result == ""


# ── TRIGGER_PATTERN ───────────────────────────────────────────


class TestTriggerPattern:
    def test_matches_at_start(self):
        assert TRIGGER_PATTERN.search("@Andy hello")

    def test_matches_case_insensitively(self):
        assert TRIGGER_PATTERN.search("@andy hello")
        assert TRIGGER_PATTERN.search("@ANDY hello")

    def test_no_match_mid_message(self):
        assert not TRIGGER_PATTERN.search("hello @Andy")

    def test_no_match_partial_name(self):
        assert not TRIGGER_PATTERN.search("@Andrew hello")

    def test_matches_with_apostrophe(self):
        assert TRIGGER_PATTERN.search("@Andy's thing")

    def test_matches_alone(self):
        assert TRIGGER_PATTERN.search("@Andy")


# ── formatOutbound ────────────────────────────────────────────


class TestFormatOutbound:
    def test_strips_whitespace(self):
        assert format_outbound("  hello world  ") == "hello world"

    def test_passthrough(self):
        assert format_outbound("hello world") == "hello world"

    def test_empty_string(self):
        assert format_outbound("") == ""

    def test_preserves_content(self):
        assert format_outbound("The answer is 42") == "The answer is 42"


# ── Trigger gating ────────────────────────────────────────────


class TestTriggerGating:
    def _should_require_trigger(self, is_main: bool, requires_trigger: bool | None) -> bool:
        return not is_main and requires_trigger is not False

    def _should_process(self, is_main: bool, requires_trigger: bool | None, messages: list[NewMessage]) -> bool:
        if not self._should_require_trigger(is_main, requires_trigger):
            return True
        return any(TRIGGER_PATTERN.search(m.content.strip()) for m in messages)

    def test_main_always_processes(self):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(True, None, msgs)

    def test_main_processes_even_with_trigger_true(self):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(True, True, msgs)

    def test_non_main_default_requires_trigger(self):
        msgs = [make_msg(content="hello no trigger")]
        assert not self._should_process(False, None, msgs)

    def test_non_main_trigger_true_requires_trigger(self):
        msgs = [make_msg(content="hello no trigger")]
        assert not self._should_process(False, True, msgs)

    def test_non_main_processes_with_trigger(self):
        msgs = [make_msg(content="@Andy do something")]
        assert self._should_process(False, True, msgs)

    def test_non_main_trigger_false_always_processes(self):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(False, False, msgs)
