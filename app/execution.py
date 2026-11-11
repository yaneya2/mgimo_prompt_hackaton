"""Cooperative external-action boundary shared by worker, adapters and tools."""
from contextlib import contextmanager
from contextvars import ContextVar


class PauseRequested(Exception):
    """Control flow: the unfinished step must stop without becoming a failed result."""


_control = ContextVar('execution_control', default=None)


@contextmanager
def execution_control(run_id, check):
    token = _control.set((run_id, check))
    try:
        yield
    finally:
        _control.reset(token)


def before_external_action(run_id=None):
    """Call immediately before each network/process/tool action, including retries.

    Standalone adapter probes have no active worker and remain supported.
    Stage handlers and their external tools execute synchronously in the worker thread.
    """
    control = _control.get()
    if control is not None:
        active_id, check = control
        if run_id is not None and run_id != active_id:
            raise ValueError('External action belongs to another run')
        check()
