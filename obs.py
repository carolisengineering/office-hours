"""Optional Langfuse tracing.

No-op unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set in the env
(LANGFUSE_HOST optional, defaults to cloud). Every call is wrapped so a Langfuse
outage or API drift degrades to "no tracing", never a crash in the agent or eval.

    with obs.trace("office_hours_agent", input=q, metadata={"arm": arm}) as t:
        with t.span("search_kb", input=query) as s:
            ...
            s.update(output={"doc_ids": ids})
        t.update(output={"answer": ...})
    obs.score(t.trace_id, "primary_pass", 1.0)
    obs.flush()
"""

from __future__ import annotations

import contextlib
import os

_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
_client = None
_client_tried = False


def enabled() -> bool:
    return _get() is not None


def _get():
    global _client, _client_tried
    if not _ENABLED:
        return None
    if not _client_tried:
        _client_tried = True
        try:
            from langfuse import Langfuse

            _client = Langfuse()
        except Exception:  # noqa: BLE001
            _client = None
    return _client


class Null:
    """Handle returned when tracing is off - every method is a no-op."""

    trace_id = None

    def update(self, **_):
        pass

    def span(self, *_a, **_k):
        return _observe(None, None, None)


class _Handle:
    def __init__(self, lf, observation):
        self._lf = lf
        self._obs = observation

    @property
    def trace_id(self):
        try:
            return self._lf.get_current_trace_id()
        except Exception:  # noqa: BLE001
            return None

    def update(self, **kw):
        try:
            self._obs.update(**kw)
        except Exception:  # noqa: BLE001
            pass

    def span(self, name, *, input=None, metadata=None):  # noqa: A002
        return _observe(name, input, metadata)


@contextlib.contextmanager
def _observe(name, input, metadata):  # noqa: A002
    """Single-yield, never-raises span/trace. Caller-block exceptions still
    propagate (the agent handles its own)."""
    lf = _get() if name is not None else None
    ctx = None
    handle: object = Null()
    if lf is not None:
        try:
            ctx = lf.start_as_current_observation(
                as_type="span", name=name, input=input, metadata=metadata or {}
            )
            handle = _Handle(lf, ctx.__enter__())
        except Exception:  # noqa: BLE001
            ctx, handle = None, Null()
    try:
        yield handle
    finally:
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def trace(name, *, input=None, metadata=None):  # noqa: A002
    return _observe(name, input, metadata)


def score(trace_id, name, value, comment=None):
    lf = _get()
    if lf is None or not trace_id:
        return
    try:
        lf.create_score(name=name, value=value, trace_id=trace_id, comment=comment)
    except Exception:  # noqa: BLE001
        pass


def flush():
    lf = _get()
    if lf is None:
        return
    try:
        lf.flush()
    except Exception:  # noqa: BLE001
        pass
