"""
SSE broadcaster — notifies all connected kiosk clients when the sign-in list changes.
"""
import asyncio
from typing import AsyncGenerator


class EventBroadcaster:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.remove(q)

    async def broadcast(self, event: str = "update") -> None:
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    async def stream(self, q: asyncio.Queue) -> AsyncGenerator[str, None]:
        try:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=30)
                yield f"event: {event}\ndata: \n\n"
        except asyncio.TimeoutError:
            # Send a keep-alive comment so the connection stays alive
            yield ": keep-alive\n\n"


broadcaster = EventBroadcaster()
