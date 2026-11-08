"""Parity-ceiling probes: which VM routes the gadget credential allows.

Every probe is read-only. Results are structural only: HTTP status and
a classification. A 403 whose body is the scope message means the route
is outside this credential's scopes; it says nothing about whether the
route exists for other credentials, which the nonsense-route control
probe demonstrates by returning the identical response.
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPE_MESSAGE = "route not allowed for this credential's scopes"


@dataclass
class Probe:
    name: str
    method: str
    path: str
    body: dict | None = None
    note: str = ""


PROBES: list[Probe] = [
    Probe("history-main", "GET", "/chat/history?limit=1",
          note="canonical transcript for the main chat"),
    Probe("history-side", "GET",
          "/chat/history?limit=1&session_id={session_id}",
          note="canonical transcript for a known side chat"),
    Probe("enum-list", "GET", "/chat/list",
          note="chat enumeration candidate"),
    Probe("enum-chats", "GET", "/chat/chats",
          note="chat enumeration candidate"),
    Probe("enum-threads", "GET", "/chat/threads",
          note="chat enumeration candidate"),
    Probe("enum-sessions", "GET", "/chat/sessions",
          note="chat enumeration candidate"),
    Probe("fs-read", "POST", "/fs/read",
          body={"path": "gadget-data/example.txt", "offset": 0, "len": 64},
          note="VM file read from the SDK README"),
    Probe("control-nonsense", "GET", "/chat/nonexistent-route-zzz123",
          note="control: proves a scope 403 is not an existence signal"),
]


def classify(status: int | None, response) -> str:
    if status is not None and 200 <= status < 300:
        return "allowed"
    if status == 403 and response == SCOPE_MESSAGE:
        return "scope_refused"
    if status == 403:
        return "forbidden_other"
    if status is None:
        return "transport_error"
    return f"http_{status}"


async def run_probes(session, session_id: str | None = None) -> list[dict]:
    import json

    from musegadget.noise import Header

    results = []
    for probe in PROBES:
        path = probe.path
        if "{session_id}" in path:
            if not session_id:
                results.append({"probe": probe.name, "path": path,
                                "classification": "skipped_no_session"})
                continue
            path = path.format(session_id=session_id)
        body = b""
        headers = None
        if probe.body is not None:
            body = json.dumps(probe.body).encode()
            headers = [Header("Content-Type", "application/json")]
        try:
            result = await session.request(
                probe.method, path, body, headers=headers)
            verdict = classify(result.get("status"), result.get("response"))
            status = result.get("status")
        except Exception as exc:
            verdict, status = f"error_{type(exc).__name__}", None
        results.append({"probe": probe.name, "method": probe.method,
                        "path": path, "status": status,
                        "classification": verdict, "note": probe.note})
    return results
