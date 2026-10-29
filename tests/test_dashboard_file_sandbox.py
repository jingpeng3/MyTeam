"""dashboard /file 产物响应带 CSP sandbox + nosniff：LLM 写的 html/svg 不能在看板同源下跑脚本。"""

from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from agent_team.config import Paths
from agent_team.dashboard import _make_handler

_HTML_BODY = b"<html><script>fetch('/api/state')</script></html>"
_SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture
def server(tmp_path):
    (tmp_path / "team.yaml").write_text("agents: []\n", encoding="utf-8")
    paths = Paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    art = paths.artifacts_dir / "g1" / "t1"
    art.mkdir(parents=True)
    (art / "report.html").write_bytes(_HTML_BODY)
    (art / "chart.svg").write_bytes(_SVG_BODY)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(paths, host="127.0.0.1"))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        yield httpd.server_address[1], art
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize(("name", "body", "ctype"), [
    ("report.html", _HTML_BODY, "text/html; charset=utf-8"),
    ("chart.svg", _SVG_BODY, "image/svg+xml"),
])
def test_file_artifact_sandboxed(server, name, body, ctype):
    port, art = server
    status, headers, got = _get(port, "/file?p=" + quote(str(art / name)))
    assert status == 200
    assert got == body                                  # 内容照常提供
    assert headers["Content-Type"] == ctype
    csp = headers["Content-Security-Policy"]
    assert csp == "sandbox allow-scripts"
    assert "allow-same-origin" not in csp               # 关键：不能回到看板同源
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_non_file_endpoint_unaffected(server):
    port, _ = server
    status, headers, _body = _get(port, "/api/state")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert "Content-Security-Policy" not in headers
    assert "X-Content-Type-Options" not in headers
