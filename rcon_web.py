# rcon_web.py
# All WebRCON-related logic lives here.

import asyncio
import json
from typing import Dict, Optional

import websockets

# =========================
# MASTER RCON SWITCH
# =========================
RCON_ENABLED = False  # ❌ OFF — bot will NOT connect to RCON
# RCON_ENABLED = True   # ✅ ON — bot WILL connect to RCON

# ======================================================
# RCON CONFIG – one entry per server (s1–s10)
# ======================================================

RCON_CONFIGS: Dict[str, Dict] = {
    "s1":  {"host": "209.126.11.83",     "port": 29316, "password": "nTjwvYvg"},
    "s2":  {"host": "45.137.247.28",     "port": 28016, "password": "KzlgSdIa"},
    "s3":  {"host": "94.72.116.55",      "port": 29516, "password": "xU5OAm24"},
    "s4":  {"host": "147.93.160.201",    "port": 28016, "password": "DJAJ5KWW"},
    "s5":  {"host": "147.93.161.130",    "port": 29216, "password": "BdwIkooa"},
    "s6":  {"host": "207.244.244.91",    "port": 28516, "password": "ATPxVXYN"},
    "s7":  {"host": "144.126.136.210",   "port": 29716, "password": "y8YUK93z"},
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
        # IMPORTANT: Rust Console wants password in the URL path, with trailing slash
        return f"ws://{self.host}:{self.port}/{self.password}/"

    async def connect(self):
        """Open (or reopen) a WebRCON connection with detailed logs."""
        if self.ws and not self.ws.closed:
            return

        print(f"[RCON:{self.name}] Connecting to {self.url} ...")
        try:
            self.ws = await websockets.connect(self.url, ping_interval=None)
            print(f"[RCON:{self.name}] ✅ Connected")
        except Exception as e:
            print(f"[RCON:{self.name}] ❌ Connection FAILED")
            print(f"   Error: {e}\n")
            raise

    async def _recv_until_id(self, identifier: int, timeout: float = 5.0) -> dict:
        """
        Read messages until we see our Identifier or hit timeout.
        The server will send other console logs with Identifier 0 – we ignore those.
        """
        assert self.ws is not None
        while True:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(msg)

            # Ignore generic console spam
            if data.get("Identifier") == identifier:
                return data

            # Optional: see other logs coming in
            # print(f"[RCON:{self.name} LOG]", data)

    async def send_command(self, command: str, timeout: float = 5.0) -> dict:
        """
        Send a command (e.g. 'status', 'banid "...""', etc.)
        and return the matching response JSON from the server.
        """
        async with self._lock:
            await self.connect()
            assert self.ws is not None

            identifier = self._next_id
            self._next_id += 1

            print(f"[RCON:{self.name}] → Sending command: {command}")

            payload = {
                "Identifier": identifier,
                "Message": command,
                "Name": "WebRcon",
            }

            await self.ws.send(json.dumps(payload))

            try:
                resp = await self._recv_until_id(identifier, timeout=timeout)
                msg = (resp.get("Message") or "").replace("\u0000", "")
                print(f"[RCON:{self.name}] ← Response: {msg}")
                return resp
            except Exception as e:
                print(f"[RCON:{self.name}] ❌ Error waiting for response: {command}")
                print(f"   Error: {e}")
                await self.close()
                raise

    async def close(self):
        if self.ws and not self.ws.closed:
            await self.ws.close()
        self.ws = None


class RconManager:
    """
    Holds up to 10 WebRconClient instances, one per server key (s1–s10).
    """

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
        client = self.get(server_key)
        return await client.send_command(command, timeout=timeout)

    async def broadcast(self, command: str, timeout: float = 5.0) -> Dict[str, dict]:
        """
        Send a command to ALL configured servers.
        Returns a dict mapping key -> response.
        """
        print(f"[RCON:BROADCAST] Sending '{command}' to all servers...")
        results: Dict[str, dict] = {}
        for key, client in self.clients.items():
            print(f"[RCON:BROADCAST] → {key.upper()}")
            try:
                resp = await client.send_command(command, timeout=timeout)
                results[key] = resp
            except Exception as e:
                print(f"[RCON:{key}] error broadcasting {command!r}: {e}")
        return results

    async def close_all(self):
        for client in self.clients.values():
            await client.close()


# Global RCON manager you can use in the rest of your bot
rcon_manager = RconManager(RCON_CONFIGS)


async def check_rcon_health_on_startup() -> list[str]:
    """
    Run RCON health checks at startup.

    Returns a list of short failure descriptions like:
      ["S1: timeout", "S2: auth failed"]
    so on_ready() can show them in the embed.
    """
    failures: list[str] = []

    if not RCON_ENABLED:
        print("[RCON] DISABLED BY MASTER SWITCH — skipping all RCON connections.\n")
        # Not treated as a 'failure' here – just config
        return failures

    print("[RCON] Enabled → checking connections...\n")
    for key, client in rcon_manager.clients.items():
        try:
            await client.connect()
            print(f"✅ {key.upper()} connected → {client.host}:{client.port}")
        except Exception as e:
            msg = f"{key.upper()} @ {client.host}:{client.port} → {e}"
            failures.append(msg)
            print(f"❌ {msg}\n")

    print("🔧 RCON check complete.\n")
    return failures


async def run_rcon_command(command: str, client_key: str = "s1") -> None:
    """
    Master RCON command helper.
    - Respects RCON_ENABLED master switch
    - Routes through rcon_manager.clients
    """
    if not RCON_ENABLED:
        print(f"[RCON] Skipped command (RCON disabled): {command}")
        return

    try:
        client = rcon_manager.get(client_key)  # uses .lower() internally
    except KeyError:
        print(f"[RCON] No RCON client configured for key '{client_key}'.")
        return

    try:
        # send_command already calls connect() internally
        await client.send_command(command)
        print(f"[RCON] Sent → [{client_key.upper()}] {command}")
    except Exception as e:
        print(f"[RCON] FAILED → [{client_key.upper()}] {command}")
        print(f"   Error: {e}")


async def rcon_send_all(command: str, timeout: float = 5.0) -> None:
    """
    Broadcast a command to all configured RCON servers.
    This replaces the old TCP RCON implementation.
    """
    if not RCON_ENABLED:
        print(f"[RCON] Skipped broadcast (RCON disabled): {command!r}")
        return

    await rcon_manager.broadcast(command, timeout=timeout)
    print(f"[RCON] Broadcast complete for: {command!r}")