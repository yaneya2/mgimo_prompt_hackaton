"""Cooperative external-action boundary shared by worker, adapters and tools."""
from contextlib import contextmanager
from contextvars import ContextVar


class PauseRequested(Exception):
    """Control flow: the unfinished step must stop without becoming a failed result."""


_control = ContextVar('execution_control', default=None)


@contextmanager
def execution_control(run_id, check, policy=None):
    token = _control.set((run_id, check, policy))
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
        active_id, check, policy = control
        if run_id is not None and run_id != active_id:
            raise ValueError('External action belongs to another run')
        check()
        if policy is not None:
            policy.check()


def current_policy(run_id=None):
    control = _control.get()
    if control is not None and run_id is not None and run_id != control[0]:
        raise ValueError('External action belongs to another run')
    return control[2] if control is not None else None


def reserve_external(run_id, kind, timeout, maximum_cost=None):
    before_external_action(run_id)
    policy = current_policy(run_id)
    return policy.reserve(kind, timeout, maximum_cost) if policy is not None else None
