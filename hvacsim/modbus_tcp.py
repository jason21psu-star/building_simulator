"""
Minimal Modbus TCP server (and client) on asyncio, no extra dependencies.

The server behaves like a Modbus TCP/RTU gateway: one TCP port, one "unit"
(slave) per unit ID.  Units are register providers:

    provider.online() -> bool                       (False: gateway answers 0x0B)
    provider.read(register) -> Optional[int]        (register as 4xxxx; None = not implemented)
    provider.write(register, value) -> bool         (False = not writable)

Register 4xxxx is PDU address xxxx - 1 (40010 -> 9).  Functions 03 / 04
(read holding / input registers, same table), 06 and 16.  Reads and writes are
1-40 contiguous registers, as in the Cummins register mapping manual.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("hvacsim")

MAX_REGS = 40
ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE, GATEWAY_NO_RESPONSE = 0x01, 0x02, 0x03, 0x0B


class ModbusTcpServer:
    def __init__(self, units: Dict[int, Any], bind: str = "0.0.0.0", port: int = 502, name: str = "modbus"):
        self.units = units
        self.bind = bind
        self.port = port
        self.name = name
        self.server: Optional[asyncio.base_events.Server] = None
        self.log: deque = deque(maxlen=40)
        self.requests = 0
        self.errors = 0
        self.clients = 0
        self.status = "not started"

    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(self._client, self.bind, self.port)
        except OSError as err:
            # e.g. port 502 needs privileges on Linux: fall back to a free port and say so
            log.warning("%s: cannot listen on %s:%s (%s) - using a free port instead", self.name, self.bind, self.port, err)
            self.server = await asyncio.start_server(self._client, self.bind, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.status = f"listening on {self.bind}:{self.port}"
        log.info("%s: Modbus TCP %s (unit IDs %s)", self.name, self.status, sorted(self.units))

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
            self.status = "stopped"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.clients += 1
        try:
            while True:
                head = await reader.readexactly(7)
                tid, pid, length, uid = struct.unpack(">HHHB", head)
                if length < 2 or length > 260:
                    break
                pdu = await reader.readexactly(length - 1)
                if pid != 0:
                    continue
                resp = self.handle(uid, pdu, peer)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.clients -= 1
            writer.close()

    def _note(self, peer, uid: int, fc: int, start: int, count: int, result: str) -> None:
        self.requests += 1
        if result != "ok":
            self.errors += 1
        who = peer[0] if isinstance(peer, tuple) else str(peer or "")
        self.log.appendleft({"t": time.strftime("%H:%M:%S"), "client": who, "unit": uid, "fc": fc,
                             "start": 40001 + start if start >= 0 else None, "count": count, "result": result})

    def handle(self, uid: int, pdu: bytes, peer=None) -> bytes:
        """One request PDU -> response PDU (also used directly by tests)."""
        fc = pdu[0] if pdu else 0
        exc = lambda code, start=-1, count=0, why="": (self._note(peer, uid, fc, start, count, why or f"exception {code:02X}")
                                                     or bytes([fc | 0x80, code]))
        unit = self.units.get(uid)
        if unit is None or not unit.online():
            return exc(GATEWAY_NO_RESPONSE, why="no response (exception 0B)")
        if fc in (3, 4):
            if len(pdu) < 5:
                return exc(ILLEGAL_VALUE)
            start, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= MAX_REGS:
                return exc(ILLEGAL_VALUE, start, count, f"count {count} (1-{MAX_REGS}) - exception 03")
            vals = []
            for i in range(count):
                v = unit.read(40001 + start + i)
                if v is None:
                    return exc(ILLEGAL_ADDRESS, start, count, f"{40001 + start + i} not implemented - exception 02")
                vals.append(int(v) & 0xFFFF)
            self._note(peer, uid, fc, start, count, "ok")
            return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *vals)
        if fc == 6:
            if len(pdu) < 5:
                return exc(ILLEGAL_VALUE)
            addr, val = struct.unpack(">HH", pdu[1:5])
            if not unit.write(40001 + addr, val):
                return exc(ILLEGAL_ADDRESS, addr, 1, f"{40001 + addr} not writable - exception 02")
            self._note(peer, uid, fc, addr, 1, "ok")
            return pdu[:5]
        if fc == 16:
            if len(pdu) < 6:
                return exc(ILLEGAL_VALUE)
            start, count, nbytes = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= MAX_REGS or nbytes != 2 * count or len(pdu) < 6 + nbytes:
                return exc(ILLEGAL_VALUE, start, count)
            vals = struct.unpack(f">{count}H", pdu[6:6 + nbytes])
            for i in range(count):
                if unit.read(40001 + start + i) is None:
                    return exc(ILLEGAL_ADDRESS, start, count, f"{40001 + start + i} not implemented - exception 02")
            for i, v in enumerate(vals):
                if not unit.write(40001 + start + i, v):
                    return exc(ILLEGAL_ADDRESS, start, count, f"{40001 + start + i} not writable - exception 02")
            self._note(peer, uid, fc, start, count, "ok")
            return pdu[:5]
        return exc(ILLEGAL_FUNCTION, why=f"function {fc} - exception 01")

    def snapshot(self) -> Dict[str, Any]:
        return {"status": self.status, "port": self.port, "bind": self.bind, "units": sorted(self.units),
                "requests": self.requests, "errors": self.errors, "clients": self.clients, "log": list(self.log)[:15]}


class ModbusTcpClient:
    """Small Modbus TCP client (demo controller, tests, tools)."""

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.tid = 0

    async def _ensure(self) -> None:
        if self.writer is None:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)

    async def request(self, uid: int, pdu: bytes) -> bytes:
        await self._ensure()
        self.tid = (self.tid + 1) & 0xFFFF
        self.writer.write(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, uid) + pdu)
        await self.writer.drain()
        try:
            head = await asyncio.wait_for(self.reader.readexactly(7), self.timeout)
            _tid, _pid, length, _uid = struct.unpack(">HHHB", head)
            return await asyncio.wait_for(self.reader.readexactly(length - 1), self.timeout)
        except Exception:
            self.close()
            raise

    async def read(self, uid: int, register: int, count: int = 1, fc: int = 3) -> List[int]:
        resp = await self.request(uid, struct.pack(">BHH", fc, register - 40001, count))
        if resp[0] & 0x80:
            raise ModbusError(resp[1])
        return list(struct.unpack(f">{count}H", resp[2:2 + 2 * count]))

    async def write(self, uid: int, register: int, value: int) -> None:
        resp = await self.request(uid, struct.pack(">BHH", 6, register - 40001, int(value) & 0xFFFF))
        if resp[0] & 0x80:
            raise ModbusError(resp[1])

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None


class ModbusError(Exception):
    def __init__(self, code: int):
        super().__init__(f"Modbus exception {code:02X}")
        self.code = code


def s16(v: float) -> int:
    """Signed value -> 16-bit register."""
    return int(round(v)) & 0xFFFF


def u32(v: float) -> Tuple[int, int]:
    """Unsigned 32-bit value -> (high, low) registers."""
    n = max(0, int(v)) & 0xFFFFFFFF
    return n >> 16, n & 0xFFFF
