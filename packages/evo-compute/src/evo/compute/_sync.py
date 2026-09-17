#  Copyright © 2026 Bentley Systems, Incorporated
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""The bridge :class:`~evo.compute.engine.SyncComputeClient` blocks on.

Every call in the SDK is a coroutine, so running one without ``await`` means running it
somewhere a loop already turns. That somewhere is a single daemon thread, started the first
time :func:`run_sync` is called and owning a private event loop for the life of the process::

    from evo.compute import run_sync

    manager = ServiceManagerWidget.with_auth_code(client_id="...")
    run_sync(manager.login())

``asyncio.run`` is not an alternative: it closes the loop it made, so the second call finds a
connector bound to a loop that no longer exists, and inside a notebook it refuses outright
because the kernel already has one running. ``nest_asyncio`` and ``run_until_complete`` on a
running loop are re-entrancy tricks played on a loop somebody else owns, and are equally out.

**Authenticate through the bridge.** ``aiohttp`` binds a session to the loop it was opened on,
so a context first used on a different loop -- the notebook kernel's, which is what
``await manager.login()`` uses -- cannot be driven from here. It fails deep inside the retry
wrapper as ``Future attached to a different loop``; :func:`run_sync` recognises that and
re-raises it as a :class:`~evo.compute.exceptions.SyncBridgeError` naming the two ways out.
Running the login through the bridge as well keeps everything on one loop and the question
never comes up.

**A notebook cell is itself inside a running loop.** ``asyncio.get_running_loop()`` succeeds
there, so refusing whenever a loop is running would refuse the case this exists for -- and it
is not the hazard anyway, because the bridge turns its own loop on its own thread and cannot
be starved by a caller blocking theirs. The one call that really would deadlock, made from
inside the bridge's own loop, is refused by name.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from .exceptions import SyncBridgeError

__all__ = [
    "run_sync",
]

T = TypeVar("T")

_THREAD_NAME = "evo-compute-sync"

# How asyncio and aiohttp word it when something is driven from a loop other than its own.
_FOREIGN_LOOP = ("attached to a different loop", "bound to a different event loop")


class _Bridge:
    """A daemon thread turning a private event loop, started the first time one is wanted."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def thread(self) -> threading.Thread | None:
        """The thread turning the loop, or ``None`` while the bridge has never been used."""
        return self._thread

    def loop(self) -> asyncio.AbstractEventLoop:
        """The bridge's event loop, starting the thread that turns it if it is not running yet."""
        with self._mutex:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._turn, args=(self._loop,), name=_THREAD_NAME, daemon=True)
                self._thread.start()
            return self._loop

    @staticmethod
    def _turn(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop and join the thread. A later call simply starts a fresh one.

        Registered with :mod:`atexit`, so nothing is asked of the caller. The thread is a
        daemon, so a process that dies without running exit handlers is not held up either.
        """
        with self._mutex:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout)
        loop.close()


_BRIDGE = _Bridge()
atexit.register(_BRIDGE.stop)


def run_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion on the bridge's loop and return what it returned.

    Everything the coroutine awaits runs on that one loop, so a connector first opened
    through :func:`run_sync` stays usable through :func:`run_sync`.

    :param coroutine: The coroutine to run.

    :return: The coroutine's result.

    :raises SyncBridgeError: If called from inside the bridge's own loop, where waiting for
        the result would deadlock, or if the coroutine touched something belonging to a
        different event loop.

    Any other exception raised inside the coroutine propagates as itself, with the frames
    from the bridge thread still attached -- there is no ``concurrent.futures`` wrapper.
    """
    if threading.current_thread() is _BRIDGE.thread:
        coroutine.close()
        raise SyncBridgeError(
            "run_sync() was called from inside the event loop it runs coroutines on, so it "
            "would wait for a result that only this thread can produce. Await the coroutine "
            "here instead."
        )
    future = asyncio.run_coroutine_threadsafe(coroutine, _BRIDGE.loop())
    try:
        return future.result()
    except KeyboardInterrupt:
        # The caller has stopped waiting, so stop the work rather than leaving it running.
        future.cancel()
        raise
    except Exception as error:
        if _from_another_loop(error):
            raise SyncBridgeError(
                "this connection was first used on a different event loop, so it cannot be "
                "driven from a blocking call. Authenticate through the same bridge -- "
                "`run_sync(manager.login())` rather than `await manager.login()` -- or use "
                "the asynchronous `ComputeClient` and await its calls."
            ) from error
        raise


def _from_another_loop(error: BaseException, seen: set[int] | None = None) -> bool:
    """Whether ``error``, or anything it wraps, is asyncio objecting to a foreign event loop.

    The complaint is raised per attempt inside the transport's retry wrapper, so by the time
    it reaches a caller it is a ``TransportError`` holding a ``RetryError`` holding the real
    ones. Cause, context, ``caused_by`` and grouped sub-exceptions are all followed.
    """
    seen = set() if seen is None else seen
    if id(error) in seen:
        return False
    seen.add(id(error))
    if any(phrase in str(error) for phrase in _FOREIGN_LOOP):
        return True
    wrapped = [
        *getattr(error, "exceptions", ()),
        getattr(error, "caused_by", None),
        error.__cause__,
        error.__context__,
    ]
    return any(inner is not None and _from_another_loop(inner, seen) for inner in wrapped)
