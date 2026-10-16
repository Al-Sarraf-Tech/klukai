"""A random loopback proxy whose upstream is a newly created test container."""

from __future__ import annotations

import asyncio


class LoopbackProxy:
    def __init__(self, host: str, port: int):
        self.upstream = (host, port)
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.url = ""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task:
            self.tasks.add(task)
        upstream = None
        try:
            remote, upstream = await asyncio.open_connection(*self.upstream)

            async def copy(
                source: asyncio.StreamReader, target: asyncio.StreamWriter
            ) -> None:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
                target.close()

            await asyncio.gather(copy(reader, upstream), copy(remote, writer))
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            await asyncio.gather(
                writer.wait_closed(),
                *([upstream.wait_closed()] if upstream else []),
                return_exceptions=True,
            )
            if task:
                self.tasks.discard(task)

    async def close(self) -> None:
        if self.server:
            self.server.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        if self.tasks:
            raise RuntimeError("Disposable loopback proxy tasks leaked")
