"""Tests for container-runner output parsing — ported from container-runner.test.ts.

Tests the OUTPUT_START/END marker protocol for parsing container results.
Rather than mocking subprocess, tests the output parsing logic directly.
"""

from __future__ import annotations

import json

import pytest


OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"


def parse_container_output(raw_stdout: str) -> list[dict]:
    """Extract structured outputs from container stdout.

    Mirrors the parsing logic in container_runner.py.
    """
    results: list[dict] = []
    lines = raw_stdout.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip() == OUTPUT_START_MARKER:
            json_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != OUTPUT_END_MARKER:
                json_lines.append(lines[i])
                i += 1
            json_text = "\n".join(json_lines).strip()
            if json_text:
                try:
                    results.append(json.loads(json_text))
                except json.JSONDecodeError:
                    pass
        i += 1
    return results


class TestContainerOutputParsing:
    def test_parses_success_output(self):
        output = {
            "status": "success",
            "result": "Here is my response",
            "newSessionId": "session-123",
        }
        raw = f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
        results = parse_container_output(raw)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["result"] == "Here is my response"
        assert results[0]["newSessionId"] == "session-123"

    def test_parses_error_output(self):
        output = {
            "status": "error",
            "result": None,
            "error": "something went wrong",
        }
        raw = f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
        results = parse_container_output(raw)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert results[0]["error"] == "something went wrong"

    def test_parses_multiple_outputs(self):
        out1 = {"status": "success", "result": "first", "newSessionId": "s-1"}
        out2 = {"status": "success", "result": "second", "newSessionId": "s-2"}
        raw = (
            f"{OUTPUT_START_MARKER}\n{json.dumps(out1)}\n{OUTPUT_END_MARKER}\n"
            f"some noise\n"
            f"{OUTPUT_START_MARKER}\n{json.dumps(out2)}\n{OUTPUT_END_MARKER}\n"
        )
        results = parse_container_output(raw)
        assert len(results) == 2
        assert results[0]["result"] == "first"
        assert results[1]["result"] == "second"

    def test_handles_noise_between_markers(self):
        output = {"status": "success", "result": "clean"}
        raw = (
            "some stderr noise\n"
            "another line\n"
            f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
            "trailing noise\n"
        )
        results = parse_container_output(raw)
        assert len(results) == 1
        assert results[0]["result"] == "clean"

    def test_handles_empty_output(self):
        raw = ""
        results = parse_container_output(raw)
        assert len(results) == 0

    def test_handles_no_markers(self):
        raw = "just some output\nwith no markers\n"
        results = parse_container_output(raw)
        assert len(results) == 0

    def test_null_result(self):
        output = {"status": "success", "result": None, "newSessionId": "s-1"}
        raw = f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
        results = parse_container_output(raw)
        assert len(results) == 1
        assert results[0]["result"] is None
