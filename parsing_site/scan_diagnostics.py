"""Timing for occupied-square scans, written through the application logger."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging import getLogger
from time import monotonic

_LOG = getLogger(__name__)
_pass_id: ContextVar[int | None] = ContextVar("scan_pass_id", default=None)


@contextmanager
def scan_pass(pass_id: int | None) -> Iterator[None]:
    token = _pass_id.set(pass_id)
    try:
        yield
    finally:
        _pass_id.reset(token)


def scan_progress(event: str, *args: object) -> None:
    pass_id = _pass_id.get()
    if pass_id is not None:
        _LOG.info("scan pass=%s " + event, pass_id, *args)


def timed_call[**P, T](
    step: str, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs,
) -> T:
    if _pass_id.get() is None:
        return operation(*args, **kwargs)
    started = monotonic()
    scan_progress("step=%s status=start", step)
    try:
        result = operation(*args, **kwargs)
    except Exception as exc:
        scan_progress("step=%s status=error elapsed_ms=%.0f type=%s",
                      step, (monotonic() - started) * 1000, type(exc).__name__)
        raise
    scan_progress("step=%s status=done elapsed_ms=%.0f",
                  step, (monotonic() - started) * 1000)
    return result
