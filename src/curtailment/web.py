"""Minimal read-only status web server (stdlib only)."""

from __future__ import annotations

import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

logger = logging.getLogger("curtailment.web")


def _cell(v: Any) -> str:
    if isinstance(v, dict):
        return "<br>".join(f"{html.escape(str(k))}: {html.escape(str(x))}" for k, x in v.items())
    return html.escape(str(v))


def _table(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<em>none</em>"
    cols = list(items[0])
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(it.get(c, ''))}</td>" for c in cols) + "</tr>" for it in items
    )
    return f'<table class="sub"><tr>{head}</tr>{body}</table>'


def _render_html(status: dict[str, Any]) -> str:
    banner = ""
    bridge = status.get("evcc_bridge")
    if isinstance(bridge, dict):
        warn = bridge.get("vpp_mismatch") or bridge.get("last_error")
        banner = (
            f'<div class="banner"><b>Battery: {html.escape(str(bridge.get("active")))}</b>'
            f' &mdash; {html.escape(str(bridge.get("decision")))}'
            + (f'<div class="warn">{html.escape(str(warn))}</div>' if warn else "")
            + "</div>"
        )
    rows = []
    for section, values in status.items():
        rows.append(f'<tr><th colspan="2" class="sec">{html.escape(section)}</th></tr>')
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                    cell = _table(v)
                else:
                    cell = _cell(v)
                rows.append(f"<tr><td>{html.escape(str(k))}</td><td>{cell}</td></tr>")
        else:
            rows.append(f'<tr><td colspan="2">{html.escape(str(values))}</td></tr>')
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Curtailment status</title>
<style>
body{{font-family:sans-serif;margin:1em}}
table{{border-collapse:collapse}}
td,th{{padding:3px 10px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
th.sec{{background:#eee}}
table.sub td,table.sub th{{font-size:90%;padding:2px 6px}}
.banner{{padding:8px 12px;margin-bottom:1em;background:#eef5ff;border-left:4px solid #48c}}
.warn{{color:#b00;margin-top:4px}}
.ver{{color:#777;margin-top:-0.5em}}
</style></head><body>
<h1>Curtailment daemon</h1>
<p class="ver">{html.escape(str(status.get("daemon", {}).get("version", "")))}</p>
{banner}
<table>{''.join(rows)}</table>
<p><a href="/status.json">status.json</a> &middot; auto-refresh 5s</p>
</body></html>"""


class StatusServer:
    """Serves status from a callable in a background thread."""

    def __init__(self, host: str, port: int, get_status: Callable[[], dict[str, Any]]) -> None:
        get = get_status

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                try:
                    status = get()
                except Exception as e:  # never let the UI break
                    status = {"error": {"message": str(e)}}
                if self.path in ("/status.json", "/api/status"):
                    body = json.dumps(status, default=str, indent=2).encode()
                    ctype = "application/json"
                elif self.path in ("/", "/index.html"):
                    body = _render_html(status).encode()
                    ctype = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("web: " + fmt, *args)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="web", daemon=True)

    def start(self) -> None:
        self._thread.start()
        host, port = self._server.server_address[:2]
        logger.info("Status web server listening on http://%s:%d/", host, port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
