"""Shared pytest fixtures for Estfeed tests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001
    """Enable loading of custom_components/ in every test."""
    yield


@pytest.fixture(autouse=True)
def verify_cleanup(  # type: ignore[override]
    event_loop: asyncio.AbstractEventLoop,
    expected_lingering_tasks: bool,
    expected_lingering_timers: bool,  # noqa: ARG001
) -> Generator[None]:
    """Override HA verify_cleanup to allow asyncio executor shutdown threads.

    Python 3.12's event_loop.shutdown_default_executor() spawns a daemon thread
    named '_run_safe_shutdown_loop'. The upstream HA plugin calls that method
    inside its own verify_cleanup teardown and then asserts no new threads exist —
    a self-created race. We allow those threads through here.
    """

    threads_before = frozenset(threading.enumerate())
    tasks_before = asyncio.all_tasks(event_loop)
    yield

    event_loop.run_until_complete(event_loop.shutdown_default_executor())

    tasks = asyncio.all_tasks(event_loop) - tasks_before
    for task in tasks:
        if expected_lingering_tasks:
            pass
        else:
            pytest.fail(f"Lingering task after test {task!r}")
        task.cancel()
    if tasks:
        event_loop.run_until_complete(asyncio.wait(tasks))

    threads_after = frozenset(threading.enumerate())
    for thread in threads_after - threads_before:
        assert (
            isinstance(thread, threading._DummyThread)
            or thread.name.startswith("waitpid-")
            or (thread.name.startswith("Thread-") and "_run_safe_shutdown_loop" in thread.name)
        ), f"Unexpected lingering thread: {thread.name!r}"
