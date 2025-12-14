import asyncio
import json
from typing import Dict, Optional
import time

import websockets
from config_starz import RCON_ENABLED

# =========================
# NOISY COMMAND FILTER
# =========================
# Used ONLY to silence log spam (does NOT block execution)
def _is_noisy_command(cmd: str) -> bool:
    c = (cmd or "").lower().strip()
    return (
        c.startswith("server.printpos")
        or c.startswith("playerlist")
    )


# ======================================================
# RCON CONFIG – one entry per server (s1–s10)
# ======================================================
# ✅ Kept INLINE as requested
RCON_CONFIGS: Dict[str, Dict] = {
    "s1":  {"host": "209.126.11.83",     "port": 29316, "password": "nTjwvYvg"},
   # "s2":  {"host": "45.137.247.28",     "port": 28016, "password": "KzlgSdIa"},
    "s3":  {"host": "94.72.116.55",      "port": 29516, "password": "xU5OAm24"},
    "s4":  {"host": "147.93.160.201",    "port": 28016, "password": "DJAJ5KWW"},
    "s5":  {"host": "147.93.161.130",    "port": 29216, "password": "BdwIkooa"},
    "s6":  {"host": "207.244.244.91",    "port": 28516, "password": "ATPxVXYN"},
   # "s7":  {"host": "144.126.136.210",   "port": 29716, "password": "y8YUK93z"},
    "s8":  {"host": "144.126.137.59",    "port": 30716, "password": "9faIRNLz"},
    "s9":  {"host": "45.137.244.53",     "port": 31816, "password": "EE6CIT41"},
    "s10": {"host": "46.250.243.156",    "port": 28016, "password": "uee3itkf"},
}


class WebRconClient:
    """
    Handles a single Rust Console server via WebRCON.
    Uses URL format: ws://HOST:PORT/PASSWORD/
    """

    def __init__(self, host: str, port: int, password: str, name: str = ""):
        self.host = host
        self.port = port
        self.password = password
        self.name = name or f"{host}:{port}"
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/{self.password}/"

    async def connect(self):
        if self.ws is not None:
            return

        print(f"[RCON:{self.name}] Connecting to {self.url} ...")
        try:
            self.ws = await websockets.connect(self.url, ping_interval=None)
            print(f"[RCON:{self.name}] ✅ Connected")
        except Exception as e:
            print(f"[RCON:{self.name}] ❌ Connection FAILED")
            print(f"   Error: {e}\n")
            self.ws = None
            raise

    async def close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception as e:
                print(f"[RCON:{self.name}] error closing: {e}")
            finally:
                self.ws = None

    async def _recv_until_id(self, identifier: int, timeout: float = 5.0) -> dict:
        assert self.ws is not None

        while True:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(msg)

            if data.get("Identifier") == identifier:
                return data

    async def send_command(self, command: str, timeout: float = 5.0) -> dict:
        """
        Send a command and return the matching response JSON.
        HARD timeout so slash commands never hang forever.
        """
        async with self._lock:
            await self.connect()
            assert self.ws is not None

            identifier = self._next_id
            self._next_id += 1

            # Optional: quiet spammy commands like server.printpos
            quiet = command.strip().lower().startswith("server.printpos")
            if not quiet:
                print(f"[RCON:{self.name}] → Sending command: {command}")

            payload = {
                "Identifier": identifier,
                "Message": command,
                "Name": "WebRcon",
            }

            await self.ws.send(json.dumps(payload))

            try:
                resp = await self._recv_until_id(identifier, timeout=timeout)
            except asyncio.TimeoutError:
                # Force-close socket so next command reconnects cleanly
                try:
                    await self.close()
                except Exception:
                    pass
                raise asyncio.TimeoutError(f"Timeout waiting for RCON response ({self.name}) for: {command}")

            return resp



class RconManager:
    def __init__(self, configs: Dict[str, Dict]):
        self.clients: Dict[str, WebRconClient] = {}
        for key, cfg in configs.items():
            self.clients[key.lower()] = WebRconClient(
                host=cfg["host"],
                port=cfg["port"],
                password=cfg["password"],
                name=key.upper(),
            )

    def get(self, server_key: str) -> WebRconClient:
        key = server_key.lower()
        if key not in self.clients:
            raise KeyError(
                f"Unknown server key '{server_key}'. "
                f"Valid keys: {', '.join(self.clients.keys())}"
            )
        return self.clients[key]

    async def send(self, server_key: str, command: str, timeout: float = 5.0) -> dict:
        return await self.get(server_key).send_command(command, timeout=timeout)

    async def broadcast(self, command: str, timeout: float = 5.0) -> Dict[str, dict]:
        if not _is_noisy_command(command):
            print(f"[RCON:BROADCAST] Sending '{command}' to all servers...")

        results: Dict[str, dict] = {}
        for key, client in self.clients.items():
            try:
                resp = await client.send_command(command, timeout=timeout)
                results[key] = resp
            except Exception as e:
                print(f"[RCON:{key}] error broadcasting {command!r}: {e}")
        return results

    async def close_all(self):
        for client in self.clients.values():
            await client.close()


# Global manager
rcon_manager = RconManager(RCON_CONFIGS)


async def check_rcon_health_on_startup() -> list[str]:
    failures: list[str] = []

    if not RCON_ENABLED:
        print("[RCON] DISABLED BY MASTER SWITCH — skipping all RCON connections.\n")
        return failures

    print("[RCON] Enabled → checking connections...\n")
    for key, client in rcon_manager.clients.items():
        try:
            if client.ws is None or client.ws.closed:
                await client.connect()
            print(f"✅ {key.upper()} connected → {client.host}:{client.port}")
        except Exception as e:
            msg = f"{key.upper()} @ {client.host}:{client.port} → {e}"
            failures.append(msg)
            print(f"❌ {msg}\n")

    print("🔧 RCON check complete.\n")
    return failures


async def run_rcon_command(command: str, client_key: str = "s1") -> Optional[dict]:
    if not RCON_ENABLED:
        print(f"[RCON] Skipped command (RCON disabled): {command}")
        return None

    try:
        client = rcon_manager.get(client_key)
    except KeyError:
        print(f"[RCON] No RCON client configured for key '{client_key}'.")
        return None

    try:
        resp = await client.send_command(command)

        if not _is_noisy_command(command):
            print(f"[RCON] Sent → [{client_key.upper()}] {command}")
            msg_preview = (resp.get("Message") or "").strip()
            if msg_preview:
                print(f"[RCON:{client_key.upper()}] Response: {msg_preview!r}")

        return resp
    except Exception as e:
        print(f"[RCON] FAILED → [{client_key.upper()}] {command}")
        print(f"   Error: {e}")
        return None


async def rcon_send_all(command: str, timeout: float = 5.0) -> None:
    if not RCON_ENABLED:
        print(f"[RCON] Skipped broadcast (RCON disabled): {command!r}")
        return

    await rcon_manager.broadcast(command, timeout=timeout)

    if not _is_noisy_command(command):
        print(f"[RCON] Broadcast complete for: {command!r}")



