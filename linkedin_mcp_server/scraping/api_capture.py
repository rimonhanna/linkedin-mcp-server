"""Passive capture of the API responses a LinkedIn web app makes from one page."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, Self, TypeVar

import asyncio
import logging

import anyio
import anyio.lowlevel

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.pacing import note_throttle_signal
from linkedin_mcp_server.response_structure import (
    ResponseLog,
    response_record,
    track,
)

logger = logging.getLogger(__name__)


class ApiCall(Protocol):
    @property
    def resource(self) -> str: ...


_C = TypeVar("_C", bound=ApiCall)


@dataclass(frozen=True, slots=True)
class CapturedResponse(Generic[_C]):
    call: _C
    status: int
    payload: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class ApiResponseCapture(Generic[_C]):
    """Collect the parsed API responses one page emits.

    Subclasses say which responses are theirs (``_parse``), whether to read a
    body (``_wants_body``) and may re-classify a call once its full request is
    known (``_refine``).
    """

    # Names the app in the timeout message.
    label: ClassVar[str]

    def __init__(self, page: Any):
        self._page = page
        self.responses: list[CapturedResponse[_C]] = []
        self.seen_resources: set[str] = set()
        self._event = asyncio.Event()
        self._pending: list[asyncio.Task[None]] = []
        self.structure = ResponseLog(self.label)
        # Event emitters remove listeners by the registered callable identity.
        self._listeners: tuple[tuple[str, Callable[..., None]], ...] = (
            ("response", self._on_response),
            *self._extra_listeners(),
        )

    async def __aenter__(self) -> Self:
        track(self.structure)
        for event, listener in self._listeners:
            self._page.on(event, listener)
        return self

    async def __aexit__(self, exc_type: object, *_exc: object) -> None:
        for event, listener in self._listeners:
            try:
                self._page.remove_listener(event, listener)
            except Exception:
                pass
        await self._drain_pending()

    def _extra_listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return ()

    def _parse(self, response: Any) -> _C | None:
        """The response's call, or None when it is not one to capture."""
        raise NotImplementedError

    def _wants_body(self, call: _C) -> bool:
        return True

    async def _refine(self, response: Any, call: _C) -> _C | None:
        """The call as its full request shows it; None keeps it out of ``responses``."""
        return call

    def _note_throttle(self, signal: str, **kwargs: bool) -> None:
        note_throttle_signal(signal, **kwargs)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self._pending.append(asyncio.create_task(coro))

    def _on_response(self, response: Any) -> None:
        first = self._parse(response)
        if first is None:
            return
        self.seen_resources.add(first.resource)
        status = getattr(response, "status", 0)
        if status == 429:
            self._note_throttle("http_429")

        async def read() -> None:
            call = await self._refine(response, first)
            if call is None or status == 429:
                self.structure.add(
                    response_record(call or first, response, body="not read")
                )
                return
            payload = None
            body = "not read"
            if self._wants_body(call):
                try:
                    payload = await response.json()
                    body = "json"
                except Exception:
                    payload = None
                    body = "not json"
            self.structure.add(
                response_record(call, response, body=body, payload=payload)
            )
            self.responses.append(CapturedResponse(call, status, payload))
            self._event.set()

        self._spawn(read())

    def matching(
        self, predicate: Callable[[CapturedResponse[_C]], bool], after: int = 0
    ) -> list[CapturedResponse[_C]]:
        return [item for item in self.responses[after:] if predicate(item)]

    async def wait_for(
        self,
        predicate: Callable[[CapturedResponse[_C]], bool],
        *,
        after: int = 0,
        timeout: float = 12.0,
        throttle: bool | Callable[[], bool] = True,
        wanted: Iterable[str] = (),
    ) -> CapturedResponse[_C]:
        """Wait for the first captured response, from ``after`` on, that matches.

        ``throttle=False`` is for a wait whose miss has another explanation
        (an optional request the page may not make): no throttle evidence.
        A callable decides once the wait has timed out, from what arrived.
        ``wanted`` names the resources the predicate looks for, so a failure's
        notes show every response of theirs.
        """
        self.structure.want(wanted)
        self._event.clear()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    found = self.matching(predicate, after)
                    if found:
                        return found[0]
                    await self._event.wait()
                    self._event.clear()
        except TimeoutError as exc:
            seen = ", ".join(sorted(self.seen_resources)) or "none"
            logger.warning("No matching %s response; seen: %s", self.label, seen)
            if throttle if isinstance(throttle, bool) else throttle():
                # A half signal: one missing reply is also what a slow proxy
                # looks like; two within ten minutes make a throttle.
                self._note_throttle("payload_timeout", half=True)
            raise LinkedInScraperException(
                f"{self.label} did not return the expected data in time "
                f"(responses seen: {seen})."
            ) from exc

    async def settle(self, timeout: float = 1.0) -> None:
        """Let response reads already emitted by the browser finish parsing."""
        active = [task for task in self._pending if not task.done()]
        if active:
            await asyncio.wait(active, timeout=timeout)

    async def _drain_pending(self) -> None:
        if not self._pending:
            return
        try:
            await asyncio.wait(self._pending, timeout=2.0)
        finally:
            for task in self._pending:
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait(self._pending, timeout=1.0)
                finally:
                    for task in self._pending:
                        if task.done() and not task.cancelled():
                            task.exception()
        await anyio.lowlevel.checkpoint()
