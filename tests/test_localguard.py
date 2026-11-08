"""Web pages must not be able to drive the localhost API or shim."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from muselinux import localguard
from muselinux import server as server_module
from muselinux import shim as shim_module
from muselinux.index import ChatIndex


def test_host_rules(monkeypatch):
    monkeypatch.delenv(localguard.ALLOWED_HOSTS_ENV, raising=False)
    hosts = localguard.allowed_hosts("127.0.0.1")
    for ok in ("127.0.0.1:8770", "localhost:8770", "[::1]:8770", "127.0.0.2"):
        assert localguard.host_allowed(ok, hosts), ok
    for bad in ("evil.example:8770", "127.0.0.1.evil.example", "", None):
        assert not localguard.host_allowed(bad, hosts), bad
    monkeypatch.setenv(localguard.ALLOWED_HOSTS_ENV, "box.tail.ts.net, other")
    hosts = localguard.allowed_hosts("127.0.0.1")
    assert localguard.host_allowed("box.tail.ts.net:8445", hosts)
    assert localguard.host_allowed("other", hosts)


def test_origin_rules(monkeypatch):
    monkeypatch.delenv(localguard.ALLOWED_HOSTS_ENV, raising=False)
    hosts = localguard.allowed_hosts("127.0.0.1", ["box.tail.ts.net"])
    assert localguard.origin_allowed(None, hosts)
    assert localguard.origin_allowed("http://127.0.0.1:8770", hosts)
    assert localguard.origin_allowed("https://box.tail.ts.net:8445", hosts)
    assert localguard.origin_allowed("tauri://localhost", hosts)
    for bad in ("https://evil.example", "null", "file://", "http://127.0.0.1.evil.example"):
        assert not localguard.origin_allowed(bad, hosts), bad
    assert localguard.cors_origin("tauri://localhost") == "tauri://localhost"
    assert localguard.cors_origin("http://127.0.0.1:8770") is None


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    result = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return result


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.delenv(localguard.ALLOWED_HOSTS_ENV, raising=False)
    httpd = _serve(server_module.make_handler(ChatIndex(tmp_path / "c.json")))
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_api_has_no_wildcard_cors_and_refuses_foreign_pages(api):
    status, headers, _ = _req(api, "GET", "/api/chats")
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers
    status, headers, _ = _req(api, "GET", "/api/chats",
                              headers={"Origin": "https://evil.example"})
    assert status == 403 and "Access-Control-Allow-Origin" not in headers
    status, _, _ = _req(api, "OPTIONS", "/api/chats",
                        headers={"Origin": "https://evil.example"})
    assert status == 403


def test_api_refuses_rebound_host_names(api):
    status, _, _ = _req(api, "GET", "/api/chats",
                        headers={"Host": "attacker.example"})
    assert status == 403


def test_api_writes_need_a_json_content_type(api):
    # a cross-site form or text/plain fetch is a "simple" request that
    # skips the CORS preflight; it must not create anything
    status, _, _ = _req(api, "POST", "/api/chats",
                        body=json.dumps({"label": "x"}),
                        headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = _req(api, "POST", "/api/chats",
                        body=json.dumps({"label": "x"}),
                        headers={"Content-Type": "application/json"})
    assert status == 201


def test_desktop_shell_origin_gets_cors(api):
    status, headers, _ = _req(api, "GET", "/api/chats",
                              headers={"Origin": "tauri://localhost"})
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == "tauri://localhost"


def test_shim_refuses_foreign_pages_and_plain_text(tmp_path, monkeypatch):
    monkeypatch.delenv(localguard.ALLOWED_HOSTS_ENV, raising=False)
    httpd = _serve(shim_module.make_handler(ChatIndex(tmp_path / "c.json")))
    port = httpd.server_address[1]
    try:
        assert _req(port, "GET", "/v1/models")[0] == 200
        assert _req(port, "GET", "/v1/models",
                    headers={"Origin": "https://evil.example"})[0] == 403
        assert _req(port, "POST", "/v1/chat/completions", body="{}",
                    headers={"Content-Type": "text/plain"})[0] == 415
        assert _req(port, "GET", "/v1/models",
                    headers={"Host": "attacker.example"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hosts_given_as_urls_are_reduced_to_the_name(monkeypatch):
    monkeypatch.setenv(localguard.ALLOWED_HOSTS_ENV,
                       "https://box.tail.ts.net:8445/, http://other/path")
    hosts = localguard.allowed_hosts("127.0.0.1")
    assert {"box.tail.ts.net", "other"} <= hosts


def test_cross_site_requests_without_an_origin_are_refused(api):
    # an <img> or no-cors fetch from another site carries no Origin
    status, _, _ = _req(api, "GET", "/api/chats",
                        headers={"Sec-Fetch-Site": "cross-site",
                                 "Sec-Fetch-Mode": "no-cors"})
    assert status == 403
    # a foreign page framing the GUI navigates too, but into a frame
    status, _, _ = _req(api, "GET", "/api/chats",
                        headers={"Sec-Fetch-Site": "cross-site",
                                 "Sec-Fetch-Mode": "navigate",
                                 "Sec-Fetch-Dest": "iframe"})
    assert status == 403
    # the user following a link, or typing the address, is fine
    for headers in ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                     "Sec-Fetch-Dest": "document"},
                    {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"},
                    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}):
        assert _req(api, "GET", "/api/chats", headers=headers)[0] == 200


def test_tauri_preflight_gets_cors_and_foreign_preflight_does_not(api):
    status, headers, _ = _req(api, "OPTIONS", "/api/chats",
                              headers={"Origin": "tauri://localhost",
                                       "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert headers.get("Access-Control-Allow-Origin") == "tauri://localhost"


def test_allowed_proxy_name_works_for_host_and_origin(tmp_path, monkeypatch):
    monkeypatch.setenv(localguard.ALLOWED_HOSTS_ENV, "box.tail.ts.net")
    httpd = _serve(server_module.make_handler(ChatIndex(tmp_path / "c.json")))
    port = httpd.server_address[1]
    try:
        assert _req(port, "GET", "/api/chats", headers={
            "Host": "box.tail.ts.net",
            "Origin": "https://box.tail.ts.net"})[0] == 200
        # a proxy that rewrites Host still passes the browser's Origin
        assert _req(port, "GET", "/api/chats", headers={
            "Origin": "https://box.tail.ts.net"})[0] == 200
        assert _req(port, "GET", "/api/chats", headers={
            "Host": "box.tail.ts.net",
            "Origin": "https://evil.example"})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_voice_notes_need_a_wav_content_type(api):
    status, _, _ = _req(api, "POST", "/api/chats/main/voice",
                        body=b"RIFF....", headers={"Content-Type": "text/plain"})
    assert status == 415


def test_shim_reads_allowed_hosts_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(localguard.ALLOWED_HOSTS_ENV, "host.docker.internal")
    httpd = _serve(shim_module.make_handler(ChatIndex(tmp_path / "c.json")))
    port = httpd.server_address[1]
    try:
        assert _req(port, "GET", "/v1/models",
                    headers={"Host": "host.docker.internal:8771"})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_request_host_headers_are_not_read_as_urls(monkeypatch):
    monkeypatch.delenv(localguard.ALLOWED_HOSTS_ENV, raising=False)
    hosts = localguard.allowed_hosts("127.0.0.1")
    assert not localguard.host_allowed("x://127.0.0.1", hosts)
