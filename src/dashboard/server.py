"""
server.py — aiohttp.web app for the local dashboard (Phase 11B).

USAGE
    python -m src.dashboard.server                       # 127.0.0.1:8080
    python -m src.dashboard.server --port 9000           # alt port
    python -m src.dashboard.server --status path/STATUS  # alt status path

The server is a SECOND PROCESS the operator runs alongside the bot. It
shares no in-memory state with grid_runner — it just reads STATUS.json
(written by HeartbeatMonitor every interval_s) and the log file (written
by the bot's logger). The HALT button writes the runtime/HALT sentinel,
which the bot's risk monitor polls and trips the kill switch on.

ROUTES
    GET  /              HTML page (auto-refresh every 5s)
    GET  /api/status    JSON: same view dict the HTML renders from
    POST /api/halt      Create runtime/HALT sentinel (idempotent)

NOT EXPOSED in v1:
    DELETE /api/halt — halt is a one-way trapdoor by design (RiskManager
        sticky halt). Removing the file does not un-halt a running bot.
        Restart the bot to clear halt state.
    Any /unhalt or /resume — same reasoning.
    Auth — localhost-only binding is enough for laptop ops. Cloud
        migration (Phase 11C) will add a token / firewall layer.

WHY aiohttp.web INSTEAD OF FASTAPI
We already pulled in aiohttp for the Phase 11A Telegram client. Adding
FastAPI would bring 3-4 transitive deps (starlette, uvicorn, pydantic,
typing-extensions pinning) for what's essentially three handlers and one
HTML template. aiohttp.web is leaner and matches the prior choice.
"""

import argparse
import html
import json
import pathlib
from typing import Optional

from aiohttp import web

from src.dashboard.data import (
    DEFAULT_STALE_AFTER_S,
    build_view,
    read_status,
    status_age_seconds,
    tail_lines,
)
from src.utils.logger import get_logger

logger = get_logger("dashboard")

# aiohttp >= 3.9 prefers typed AppKey over string keys for app["..."]
# storage. Module-level constant so handlers and tests share the same key.
CFG_KEY: web.AppKey = web.AppKey("cfg", dict)


# Default paths and config. CLI flags override; settings.yaml is read at
# CLI bootstrap (in main()) and merged on top of these so a tweak in YAML
# beats the in-code defaults but a flag beats both.
_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8080,
    "status_path": "runtime/STATUS.json",
    "log_path": "logs/grid_bot.log",
    "halt_path": "runtime/HALT",
    "log_lines": 50,
    "refresh_seconds": 5,
    "stale_after_s": DEFAULT_STALE_AFTER_S,
}


# ---------------------------------------------------------------------------
# View construction (uses the data layer)
# ---------------------------------------------------------------------------


def _gather_view(cfg: dict) -> dict:
    """One round of file reads → one view dict. Called per request."""
    status_path = pathlib.Path(cfg["status_path"])
    log_path = pathlib.Path(cfg["log_path"])
    halt_path = pathlib.Path(cfg["halt_path"])
    return build_view(
        status=read_status(status_path),
        status_age_s=status_age_seconds(status_path),
        log_lines=tail_lines(log_path, int(cfg["log_lines"])),
        stale_after_s=float(cfg["stale_after_s"]),
        halt_path_exists=halt_path.exists(),
        halt_path=str(halt_path),
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
#
# A single Python f-string is enough for one page. Every dynamic value
# passes through html.escape so a stray '<script>' in a log line can't
# break out. No JS, no client-side templating; auto-refresh is a meta tag.


_PAGE_CSS = """
* { box-sizing: border-box; }
body {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  margin: 0; padding: 24px; background: #0e1116; color: #d8dde3;
  line-height: 1.45;
}
h1 { font-size: 18px; margin: 0 0 4px; color: #f5f7fa; }
h2 { font-size: 14px; margin: 24px 0 8px; color: #93a1b2;
     text-transform: uppercase; letter-spacing: 0.06em; }
.bar { display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
       padding: 12px 16px; border-radius: 6px;
       background: #1a1f29; border: 1px solid #232936; }
.bar.warn   { border-color: #6b5d22; background: #2a230d; }
.bar.danger { border-color: #6e2a2a; background: #2a1010; }
.kv { display: inline-block; margin-right: 8px; }
.kv b { color: #93a1b2; font-weight: normal; margin-right: 4px; }
.kv span { color: #f5f7fa; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #232936; }
th { color: #93a1b2; font-weight: normal; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.04em; }
pre.log { background: #0a0d12; border: 1px solid #232936; border-radius: 6px;
          padding: 12px; overflow-x: auto; max-height: 480px; overflow-y: auto;
          font-size: 12px; color: #c8cdd5; white-space: pre-wrap; word-break: break-word; }
.btn { background: #6e2a2a; color: #fff; border: 1px solid #8c3838;
       padding: 8px 14px; border-radius: 6px; font: inherit; cursor: pointer; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }
.btn:hover:not([disabled]) { background: #8c3838; }
.muted { color: #6e7990; }
.ok    { color: #5fb37c; }
.warn  { color: #d4b14a; }
.bad   { color: #e36161; }
form.halt { margin-top: 12px; }
"""


_HALT_FORM_JS = """
function confirmHalt(form) {
  if (!confirm('Create runtime/HALT? The bot will cancel all orders and exit.')) return false;
  fetch('/api/halt', {method: 'POST'})
    .then(r => r.json())
    .then(j => { document.getElementById('halt-result').textContent =
                 j.ok ? '[HALT] sentinel created — bot should stop within ~poll_interval seconds.'
                      : '[HALT] failed: ' + (j.error || 'unknown');
                 setTimeout(() => location.reload(), 1500); })
    .catch(e => { document.getElementById('halt-result').textContent = 'fetch failed: ' + e; });
  return false;
}
"""


def _fmt_seconds(s: Optional[float]) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    return f"{s/3600:.1f}h"


def render_page(view: dict, refresh_seconds: int) -> str:
    """Render the dashboard HTML. Pure function over the view dict."""
    halted = view.get("halted")
    sentinel_exists = view.get("halt_sentinel_present")
    bar_class = "danger" if halted else ("warn" if view.get("snapshot_is_stale") else "")
    state_text = "halted" if halted else (
        "stale" if view.get("snapshot_is_stale") else (
            "running" if view.get("snapshot_present") else "no snapshot"
        )
    )
    state_class = "bad" if halted else (
        "warn" if view.get("snapshot_is_stale") else (
            "ok" if view.get("snapshot_present") else "muted"
        )
    )

    status = view.get("status") or {}
    risk = status.get("risk") or {}
    per_symbol = status.get("per_symbol") or {}

    age_str = _fmt_seconds(view.get("snapshot_age_seconds"))
    rendered_at = html.escape(view.get("rendered_at_utc") or "")

    bar = f"""
    <div class="bar {bar_class}">
      <span class="kv"><b>state</b><span class="{state_class}">{html.escape(state_text)}</span></span>
      <span class="kv"><b>env</b><span>{html.escape(str(status.get('env_mode', '—')))}</span></span>
      <span class="kv"><b>uptime</b><span>{_fmt_seconds(status.get('uptime_seconds'))}</span></span>
      <span class="kv"><b>open orders</b><span>{html.escape(str(risk.get('open_order_count', '—')))}</span></span>
      <span class="kv"><b>realized 24h</b><span>{html.escape(str(risk.get('realized_pnl_24h', '—')))}</span></span>
      <span class="kv"><b>snapshot age</b><span>{html.escape(age_str)}</span></span>
    </div>
    """.strip()

    halt_reason = risk.get("halt_reason")
    halt_block = ""
    if halted:
        halt_block = (
            f'<div class="bar danger" style="margin-top:8px;">'
            f'<span class="kv"><b>halt reason</b><span>{html.escape(str(halt_reason or "unknown"))}</span></span>'
            f"</div>"
        )

    # Per-symbol counters table
    if per_symbol:
        cols = ["symbol", "placed", "filled", "recentered",
                "blocked", "paused_liq", "paused_sent"]
        rows_html = "".join(
            f"<tr><td>{html.escape(sym)}</td>"
            f"<td>{c.get('placed_total', 0)}</td>"
            f"<td>{c.get('filled_total', 0)}</td>"
            f"<td>{c.get('recentered_total', 0)}</td>"
            f"<td>{c.get('blocked_by_risk', 0)}</td>"
            f"<td>{c.get('paused_by_liquidity', 0)}</td>"
            f"<td>{c.get('paused_by_sentiment', 0)}</td></tr>"
            for sym, c in per_symbol.items()
        )
        per_symbol_table = (
            "<h2>per-symbol counters</h2>"
            f"<table><thead><tr>{''.join(f'<th>{c}</th>' for c in cols)}</tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )
    else:
        per_symbol_table = "<h2>per-symbol counters</h2><p class='muted'>no symbols yet</p>"

    # Halt button (disabled if sentinel already exists)
    halt_button_attrs = "disabled" if sentinel_exists else ""
    halt_status_line = (
        f'<p class="muted">sentinel already present at {html.escape(view.get("halt_sentinel_path", ""))}</p>'
        if sentinel_exists else ""
    )

    # Log lines
    if view.get("log_lines"):
        log_block = "<pre class='log'>" + "\n".join(
            html.escape(line) for line in view["log_lines"]
        ) + "</pre>"
    else:
        log_block = "<p class='muted'>no log lines yet</p>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{int(refresh_seconds)}">
<title>grid bot status — {html.escape(state_text)}</title>
<style>{_PAGE_CSS}</style>
<script>{_HALT_FORM_JS}</script>
</head><body>
<h1>grid bot status</h1>
<p class="muted">rendered {rendered_at} · auto-refreshes every {int(refresh_seconds)}s</p>

{bar}
{halt_block}

<h2>halt</h2>
<form class="halt" onsubmit="return confirmHalt(this)">
  <button class="btn" {halt_button_attrs}>HALT — cancel all orders &amp; stop</button>
</form>
<p id="halt-result" class="muted"></p>
{halt_status_line}

{per_symbol_table}

<h2>last {len(view.get("log_lines") or [])} log lines</h2>
{log_block}

</body></html>
"""


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_index(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    view = _gather_view(cfg)
    body = render_page(view, refresh_seconds=int(cfg["refresh_seconds"]))
    return web.Response(text=body, content_type="text/html")


async def handle_api_status(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    view = _gather_view(cfg)
    return web.json_response(view)


async def handle_api_halt(request: web.Request) -> web.Response:
    """
    Create the kill-switch sentinel. Same idempotent op as
    `touch runtime/HALT` and the Telegram /halt command — converges on
    one disk artifact so the operator always knows what tripped a halt.
    """
    cfg = request.app[CFG_KEY]
    halt_path = pathlib.Path(cfg["halt_path"])
    try:
        halt_path.parent.mkdir(parents=True, exist_ok=True)
        halt_path.touch(exist_ok=True)
        logger.warning("dashboard /api/halt — sentinel created",
                       path=str(halt_path))
        return web.json_response({"ok": True, "path": str(halt_path)})
    except OSError as e:
        logger.error("dashboard /api/halt failed",
                     path=str(halt_path), error=str(e))
        return web.json_response(
            {"ok": False, "error": str(e), "path": str(halt_path)},
            status=500,
        )


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------


def build_app(cfg: dict) -> web.Application:
    """Construct the aiohttp app. Public so tests can hit it without main()."""
    app = web.Application()
    app[CFG_KEY] = dict(cfg)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_post("/api/halt", handle_api_halt)
    return app


def _load_cfg_from_settings(settings_path: pathlib.Path) -> dict:
    """
    Read dashboard: block from settings.yaml. Missing block / file → use
    in-code defaults. Never raises; bad YAML logs a warning and falls back.
    """
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        with settings_path.open("r", encoding="utf-8") as f:
            settings = yaml.safe_load(f) or {}
    except (OSError, ValueError) as e:
        logger.warning("dashboard could not read settings.yaml — using defaults",
                       path=str(settings_path), error=str(e))
        return cfg
    block = (settings.get("dashboard") or {})
    for k in cfg.keys():
        if k in block:
            cfg[k] = block[k]
    # Allow logging.file to drive the log path, matching the runner.
    log_file = (settings.get("logging") or {}).get("file")
    if log_file and "log_path" not in block:
        cfg["log_path"] = log_file
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local dashboard for grid_bot — STATUS.json + log tail + HALT button.",
    )
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8080)")
    parser.add_argument("--status", dest="status_path", default=None,
                        help="path to STATUS.json")
    parser.add_argument("--log",    dest="log_path", default=None,
                        help="path to grid_bot.log")
    parser.add_argument("--halt",   dest="halt_path", default=None,
                        help="path to HALT sentinel")
    parser.add_argument("--log-lines", type=int, default=None,
                        help="how many tail lines to display")
    parser.add_argument("--settings", default="config/settings.yaml",
                        help="settings.yaml path (defaults inherited from there)")
    args = parser.parse_args()

    cfg = _load_cfg_from_settings(pathlib.Path(args.settings))
    # CLI flags override YAML.
    for k in ("host", "port", "status_path", "log_path", "halt_path", "log_lines"):
        v = getattr(args, k)
        if v is not None:
            cfg[k] = v

    logger.info("dashboard starting",
                host=cfg["host"], port=cfg["port"],
                status_path=cfg["status_path"],
                log_path=cfg["log_path"],
                halt_path=cfg["halt_path"])
    app = build_app(cfg)
    web.run_app(app, host=cfg["host"], port=int(cfg["port"]),
                access_log=None, print=lambda *a, **kw: None)


if __name__ == "__main__":
    main()
