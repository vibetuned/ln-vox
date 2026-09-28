"""Tests for LLMClient's transport-retry policy (no real server involved).

Runnable directly (`python tests/test_llm_client_retry.py`) or under pytest.
The OpenAI SDK client is replaced by a stub whose `create()` replays a
scripted sequence of exceptions / responses; `time.sleep` is stubbed out.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import openai
from pydantic import BaseModel

from lnvox.llm import client as client_mod
from lnvox.llm.client import LLMClient, is_transport_error

_REQ = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")


class _Answer(BaseModel):
    value: int


def _ok(content: str, finish: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning_content=None),
            finish_reason=finish,
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _status(code: int) -> openai.APIStatusError:
    resp = httpx.Response(code, request=_REQ)
    cls = openai.InternalServerError if code >= 500 else openai.BadRequestError
    return cls(f"HTTP {code}", response=resp, body=None)


class _StubCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _client(script):
    c = LLMClient()
    stub = _StubCompletions(script)
    c.client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return c, stub


class _NoSleep:
    def __enter__(self):
        self.delays: list[float] = []
        self._orig = time.sleep
        time.sleep = self.delays.append
        return self

    def __exit__(self, *exc):
        time.sleep = self._orig


def _call(c, **kw):
    return c.structured(system="s", user="u", schema=_Answer, max_tokens=100, **kw)


def test_sdk_retries_disabled():
    # Regression: the SDK default (max_retries=2) re-sent timed-out
    # generations, so one attempt could wait ~3x the per-call timeout.
    assert LLMClient().client.max_retries == 0


def test_connection_error_is_retried_then_succeeds():
    c, stub = _client([openai.APIConnectionError(request=_REQ), _ok('{"value": 7}')])
    with _NoSleep() as s:
        assert _call(c).value == 7
    assert len(stub.calls) == 2
    assert s.delays == [LLMClient._TRANSPORT_RETRY_BACKOFF_S[0]]
    # The re-sent request is identical (nothing was generated).
    assert stub.calls[0] == stub.calls[1]


def test_503_is_retried():
    c, stub = _client([_status(503), _ok('{"value": 1}')])
    with _NoSleep():
        assert _call(c).value == 1
    assert len(stub.calls) == 2


def test_connection_retries_are_bounded_and_reraise():
    n = len(LLMClient._TRANSPORT_RETRY_BACKOFF_S) + 1
    errs = [openai.APIConnectionError(request=_REQ) for _ in range(n)]
    salvaged = []
    c, stub = _client(errs + [_ok('{"value": 1}')])
    with _NoSleep() as s:
        try:
            _call(c, salvage=lambda raw: salvaged.append(raw))
        except openai.APIConnectionError:
            pass
        else:
            raise AssertionError("expected APIConnectionError")
    assert len(stub.calls) == n
    assert s.delays == list(LLMClient._TRANSPORT_RETRY_BACKOFF_S)
    assert salvaged == []  # salvage is for bad answers, not a dead server
    assert is_transport_error(errs[-1])


def test_timeout_is_not_retried():
    # APITimeoutError subclasses APIConnectionError but must NOT be re-sent:
    # the same generation would just time out again.
    c, stub = _client([openai.APITimeoutError(request=_REQ), _ok('{"value": 1}')])
    with _NoSleep() as s:
        try:
            _call(c)
        except openai.APITimeoutError:
            pass
        else:
            raise AssertionError("expected APITimeoutError")
    assert len(stub.calls) == 1 and s.delays == []


def test_other_status_errors_are_not_retried():
    for code in (400, 500):
        c, stub = _client([_status(code), _ok('{"value": 1}')])
        with _NoSleep() as s:
            try:
                _call(c)
            except openai.APIStatusError as e:
                assert e.status_code == code
            else:
                raise AssertionError(f"expected HTTP {code}")
        assert len(stub.calls) == 1 and s.delays == []


def test_validation_retries_unchanged_and_independent():
    # A transport retry doesn't consume a validation attempt: invalid JSON,
    # then a connection error, then a valid answer = 2 validation attempts.
    c, stub = _client([
        _ok('{"value": "nope"}'),
        openai.APIConnectionError(request=_REQ),
        _ok('{"value": 3}'),
    ])
    with _NoSleep():
        assert _call(c, attempts=2).value == 3
    assert len(stub.calls) == 3
    # The validation retry still carries the corrective feedback.
    assert "IMPORTANT (retry 1)" in stub.calls[2]["messages"][1]["content"]
    # Exhausted validation attempts still raise ValueError.
    c, stub = _client([_ok("{}"), _ok("{}")])
    try:
        _call(c, attempts=2)
    except ValueError as e:
        assert "after 2 attempt(s)" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_timeout_passed_per_call():
    c, stub = _client([_ok('{"value": 1}')])
    _call(c)
    assert stub.calls[0]["timeout"] == c._timeout_for(100)
    assert client_mod._is_retryable_transport_error(ConnectionError()) is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
