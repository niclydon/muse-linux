"""Connection layer: ephemeral Link sessions from a local pairing store.

The pattern is the one proven by existing Link tooling: load the pairing
created by the Muse Gadget SDK installer, fetch the account's leased
VMs with the device token, and open a LinkSession with
``register_device=False``. An ephemeral session never sends
``link.register``, so it does not displace the machine's gadget service
and cannot receive device commands. It can send chat messages, subscribe
to chat events, and make the small set of tunnelled HTTP requests the
credential's scopes allow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

STATE_DIR_ENV = "MUSEGADGET_STATE_DIR"
DEFAULT_NOISE_HOST = "hatch.metaaivm.com"


def find_state_dir() -> Path | None:
    """Locate a musegadget pairing store, in priority order."""
    candidates: list[Path] = []
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/var/lib/musegadget"))
    candidates.append(Path.home() / ".musegadget")
    for candidate in candidates:
        if (candidate / "pairing.json").exists():
            return candidate
    return None


def load_pairing(state_dir: Path | None = None) -> dict:
    """Load pairing.json. Token values stay in this process only."""
    state_dir = state_dir or find_state_dir()
    if state_dir is None:
        raise RuntimeError(
            "no musegadget pairing found: pair this machine with the "
            "Muse app first (see the Muse Gadget SDK for Linux)")
    pairing = json.loads((state_dir / "pairing.json").read_text())
    if not pairing.get("access_token"):
        raise RuntimeError("pairing store has no access token")
    return pairing


def load_node_id(state_dir: Path | None = None) -> str:
    state_dir = state_dir or find_state_dir()
    if state_dir is None:
        raise RuntimeError("no musegadget state directory found")
    try:
        # The SDK derives the node id from the identity (on disk it may
        # hold only a MAC address); let it do the derivation.
        import os as _os

        previous = _os.environ.get(STATE_DIR_ENV)
        _os.environ[STATE_DIR_ENV] = str(state_dir)
        try:
            from musegadget import identity as sdk_identity

            return sdk_identity.load_or_create().node_id
        finally:
            if previous is None:
                _os.environ.pop(STATE_DIR_ENV, None)
            else:
                _os.environ[STATE_DIR_ENV] = previous
    except Exception:
        pass
    identity = json.loads((state_dir / "identity.json").read_text())
    node_id = identity.get("node_id") or identity.get("id")
    if not node_id:
        raise RuntimeError("identity store has no node id")
    return node_id


def pick_vm(vms: list[dict]) -> dict:
    defaults = [vm for vm in vms if vm.get("is_default")]
    if defaults:
        return defaults[0]
    if len(vms) == 1:
        return vms[0]
    raise RuntimeError(
        f"cannot pick a VM unambiguously: {len(vms)} leased, none default")


async def fetch_vms(pairing: dict) -> tuple[list[dict], int | None]:
    from musegadget import muse_api

    return await asyncio.to_thread(
        muse_api.fetch_vms_with_status,
        pairing["access_token"],
        muse_api.api_root(pairing.get("api_url_v2", "")),
    )


async def _build_session(state_dir: Path | None, display_name: str):
    """A ready-to-run ephemeral LinkSession for the default VM."""
    from musegadget import __version__ as mg_version
    from musegadget.link_client import DeviceDescription, LinkSession
    try:
        from musegadget.service import DEFAULT_NOISE_HOST as host_default
    except Exception:
        host_default = DEFAULT_NOISE_HOST

    # WebSocket debug logging can include the Authorization header.
    logging.getLogger("websockets").setLevel(logging.WARNING)

    pairing = load_pairing(state_dir)
    node_id = load_node_id(state_dir)
    vms, status = await fetch_vms(pairing)
    if status != 200 or not vms:
        raise ConnectionError(
            f"VM lookup failed (HTTP {status}); the paired device "
            "credentials may need a refresh or a re-pair")
    vm = pick_vm(vms)
    return LinkSession(
        noise_host=pairing.get("noise_host") or host_default,
        vm_id=vm["vm_id"],
        vm_auth_token=vm["vm_auth_token"],
        app_id="hatch-web",
        register_device=False,
        device=DeviceDescription(node_id, display_name, mg_version, {}),
        run_command=lambda *unused: {"ok": False,
                                     "error": "unsupported command"},
    )


async def _stop_session(stop: asyncio.Event, connection: asyncio.Task):
    stop.set()
    try:
        await asyncio.wait_for(connection, 3)
    except Exception:
        connection.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(connection, return_exceptions=True), 5)
        except Exception:
            pass


@asynccontextmanager
async def open_session(state_dir: Path | None = None,
                       display_name: str = "muse-linux") -> AsyncIterator:
    """One ready ephemeral LinkSession. Yields the session object."""
    session = await _build_session(state_dir, display_name)
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    try:
        await session.wait_transport_ready(35)
        yield session
    finally:
        await _stop_session(stop, connection)


class Connection:
    """A long-lived ephemeral session plus a way to notice it ending.

    ``session`` is the LinkSession; ``wait_closed()`` returns once the
    transport has gone away (network loss, VM restart, server close),
    so a caller can reconnect.
    """

    def __init__(self, session, connection: asyncio.Task):
        self.session = session
        self._connection = connection

    async def wait_closed(self) -> None:
        await asyncio.shield(self._connection)


@asynccontextmanager
async def connect(state_dir: Path | None = None,
                  display_name: str = "muse-linux") -> AsyncIterator:
    """Like open_session, for callers that hold the session open and
    must react when it drops. Yields a :class:`Connection`."""
    session = await _build_session(state_dir, display_name)
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    try:
        await session.wait_transport_ready(35)
        yield Connection(session, connection)
    finally:
        await _stop_session(stop, connection)


async def doctor(state_dir: Path | None = None) -> dict:
    """Read-only health summary. Never prints token material."""
    result: dict = {"state_dir": None, "paired": False,
                    "vm_fetch_status": None, "vm_count": 0}
    found = state_dir or find_state_dir()
    if found is None:
        return result
    result["state_dir"] = str(found)
    try:
        pairing = load_pairing(found)
        result["paired"] = True
        result["node_id"] = load_node_id(found)
        vms, status = await fetch_vms(pairing)
        result["vm_fetch_status"] = status
        result["vm_count"] = len(vms)
    except Exception as exc:  # surfaced, not hidden
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
