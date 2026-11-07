"""In-process fake flicd server used by the tests."""

from __future__ import annotations

import asyncio
from typing import Any

from custom_components.flicbutton.flicd import (
    BdAddrType,
    BluetoothControllerState,
    FrameReader,
    decode_command,
    encode_event,
)


class FakeFlicd:
    """Minimal flicd speaking the real wire protocol on localhost."""

    def __init__(self) -> None:
        self.port = 0
        self.bd_addr = "00:11:22:33:44:55"
        self.buttons: dict[str, dict[str, Any]] = {}
        self.controller_state = BluetoothControllerState.Attached
        self.answer_pings = True
        self.answer_get_info = True
        self.answer_button_info = True
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.scan_wizard_script: list[tuple[str, dict[str, Any]]] = []
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._waiters: list[tuple[str, asyncio.Future[dict[str, Any]]]] = []
        self._handlers: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.drop_clients()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)

    async def send(self, event: str, /, **fields: Any) -> None:
        await self.send_raw(encode_event(event, **fields))

    async def send_raw(self, data: bytes) -> None:
        for writer in list(self._writers):
            writer.write(data)
            await writer.drain()

    async def drop_clients(self) -> None:
        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()

    async def wait_for_command(self, name: str, timeout: float = 2.0) -> dict[str, Any]:
        """Return the fields of the first (possibly past) command called ``name``."""
        for cmd, fields in self.commands:
            if cmd == name:
                return fields
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiters.append((name, future))
        return await asyncio.wait_for(future, timeout)

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._handlers.add(asyncio.current_task())  # type: ignore[arg-type]
        self._writers.append(writer)
        frames = FrameReader()
        try:
            while data := await reader.read(4096):
                for payload in frames.feed(data):
                    name, fields = decode_command(payload)
                    self.commands.append((name, fields))
                    for waiter in [w for w in self._waiters if w[0] == name]:
                        self._waiters.remove(waiter)
                        if not waiter[1].done():
                            waiter[1].set_result(fields)
                    await self._respond(writer, name, fields)
        except (ConnectionError, OSError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    async def _reply(
        self, writer: asyncio.StreamWriter, event: str, /, **fields: Any
    ) -> None:
        writer.write(encode_event(event, **fields))
        await writer.drain()

    async def _respond(
        self, writer: asyncio.StreamWriter, name: str, fields: dict[str, Any]
    ) -> None:
        if name == "CmdGetInfo" and self.answer_get_info:
            await self._reply(
                writer,
                "EvtGetInfoResponse",
                bluetooth_controller_state=self.controller_state,
                my_bd_addr=self.bd_addr,
                my_bd_addr_type=BdAddrType.PublicBdAddrType,
                max_pending_connections=3,
                max_concurrently_connected_buttons=20,
                current_pending_connections=0,
                currently_no_space_for_new_connection=0,
                bd_addr_of_verified_buttons=list(self.buttons),
            )
        elif name == "CmdPing" and self.answer_pings:
            await self._reply(writer, "EvtPingResponse", ping_id=fields["ping_id"])
        elif name == "CmdGetButtonInfo" and self.answer_button_info:
            info = {
                "uuid": None,
                "color": None,
                "serial_number": None,
                "flic_version": 2,
                "firmware_version": 1,
                **self.buttons.get(fields["bd_addr"], {}),
            }
            await self._reply(
                writer, "EvtGetButtonInfoResponse", bd_addr=fields["bd_addr"], **info
            )
        elif name == "CmdDeleteButton":
            self.buttons.pop(fields["bd_addr"], None)
            await self._reply(
                writer,
                "EvtButtonDeleted",
                bd_addr=fields["bd_addr"],
                deleted_by_this_client=True,
            )
        elif name == "CmdCreateScanWizard":
            for event, event_fields in self.scan_wizard_script:
                await self._reply(
                    writer,
                    event,
                    scan_wizard_id=fields["scan_wizard_id"],
                    **event_fields,
                )
