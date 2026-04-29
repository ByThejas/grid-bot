"""
Tests for src.dashboard.server — Phase 11B.

Each test wires a temporary STATUS.json + log file + HALT path under
tmp_path, builds the app via build_app(), drives it with aiohttp's
TestClient, and asserts on response shape. No real port binding, no
process forking — fast and deterministic.
"""

import json
import pathlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.dashboard.server import build_app, render_page


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_status() -> dict:
    return {
        "env_mode": "testnet",
        "uptime_seconds": 305.0,
        "utc_day": "2026-04-29",
        "per_symbol": {
            "BTCUSDT": {"placed_total": 4, "filled_total": 0,
                        "recentered_total": 0, "blocked_by_risk": 0,
                        "paused_by_liquidity": 0, "paused_by_sentiment": 0},
            "ETHUSDT": {"placed_total": 4, "filled_total": 0,
                        "recentered_total": 0, "blocked_by_risk": 0,
                        "paused_by_liquidity": 0, "paused_by_sentiment": 0},
        },
        "risk": {
            "halted": False, "halt_reason": None, "open_order_count": 8,
            "realized_pnl_24h": 0.0, "daily_loss_limit_usd": 500.0,
        },
    }


def _build_cfg(tmp_path: pathlib.Path, *, write_status: bool = True,
               write_log: bool = True, halt: bool = False) -> dict:
    status_path = tmp_path / "STATUS.json"
    log_path = tmp_path / "grid_bot.log"
    halt_path = tmp_path / "HALT"
    if write_status:
        status_path.write_text(json.dumps(_sample_status()))
    if write_log:
        log_path.write_text(
            "\n".join(f"line-{i:03d}" for i in range(120)) + "\n",
        )
    if halt:
        halt_path.touch()
    return {
        "host": "127.0.0.1",
        "port": 0,
        "status_path": str(status_path),
        "log_path": str(log_path),
        "halt_path": str(halt_path),
        "log_lines": 50,
        "refresh_seconds": 5,
        "stale_after_s": 660.0,
    }


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_status_returns_view_with_running_state(tmp_path):
    cfg = _build_cfg(tmp_path)
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.get("/api/status")
        assert resp.status == 200
        body = await resp.json()
    assert body["snapshot_present"] is True
    assert body["halted"] is False
    assert body["status"]["env_mode"] == "testnet"
    assert "BTCUSDT" in body["status"]["per_symbol"]
    # Last 50 of 120 lines
    assert body["log_line_count"] == 50
    assert body["log_lines"][-1] == "line-119"


@pytest.mark.asyncio
async def test_api_status_handles_missing_status_file(tmp_path):
    cfg = _build_cfg(tmp_path, write_status=False)
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.get("/api/status")
        assert resp.status == 200
        body = await resp.json()
    assert body["snapshot_present"] is False
    assert body["status"] == {}
    # Log file still tailed
    assert body["log_line_count"] == 50


@pytest.mark.asyncio
async def test_api_status_reflects_halted_state(tmp_path):
    cfg = _build_cfg(tmp_path)
    s = _sample_status()
    s["risk"]["halted"] = True
    s["risk"]["halt_reason"] = "kill switch file present: runtime/HALT"
    pathlib.Path(cfg["status_path"]).write_text(json.dumps(s))
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.get("/api/status")
        body = await resp.json()
    assert body["halted"] is True
    assert body["status"]["risk"]["halt_reason"].startswith("kill switch")


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_returns_html_with_state_summary(tmp_path):
    cfg = _build_cfg(tmp_path)
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.get("/")
        assert resp.status == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        body = await resp.text()
    assert "<!doctype html>" in body
    assert "grid bot status" in body
    assert "testnet" in body
    assert "BTCUSDT" in body
    assert "ETHUSDT" in body
    # auto-refresh meta tag carries the configured interval
    assert 'http-equiv="refresh"' in body
    assert 'content="5"' in body


@pytest.mark.asyncio
async def test_index_marks_halted_in_html(tmp_path):
    cfg = _build_cfg(tmp_path)
    s = _sample_status()
    s["risk"]["halted"] = True
    s["risk"]["halt_reason"] = "kill switch file present: runtime/HALT"
    pathlib.Path(cfg["status_path"]).write_text(json.dumps(s))
    async with TestClient(TestServer(build_app(cfg))) as client:
        body = await (await client.get("/")).text()
    assert "halted" in body
    assert "kill switch file present" in body


@pytest.mark.asyncio
async def test_index_disables_halt_button_when_sentinel_present(tmp_path):
    cfg = _build_cfg(tmp_path, halt=True)
    async with TestClient(TestServer(build_app(cfg))) as client:
        body = await (await client.get("/")).text()
    assert "<button" in body
    assert "disabled" in body
    assert "sentinel already present" in body


@pytest.mark.asyncio
async def test_index_renders_when_no_status_yet(tmp_path):
    cfg = _build_cfg(tmp_path, write_status=False)
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
    assert "no snapshot" in body


# ---------------------------------------------------------------------------
# POST /api/halt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_halt_creates_sentinel(tmp_path):
    cfg = _build_cfg(tmp_path)
    halt_path = pathlib.Path(cfg["halt_path"])
    assert not halt_path.exists()
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.post("/api/halt")
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert halt_path.exists()


@pytest.mark.asyncio
async def test_api_halt_is_idempotent(tmp_path):
    cfg = _build_cfg(tmp_path)
    halt_path = pathlib.Path(cfg["halt_path"])
    halt_path.write_text("pre-existing")
    pre_size = halt_path.stat().st_size
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.post("/api/halt")
        body = await resp.json()
    assert body["ok"] is True
    # touch(exist_ok=True) does not truncate
    assert halt_path.stat().st_size == pre_size


@pytest.mark.asyncio
async def test_api_halt_creates_parent_dir_if_missing(tmp_path):
    cfg = _build_cfg(tmp_path)
    cfg["halt_path"] = str(tmp_path / "subdir" / "deep" / "HALT")
    async with TestClient(TestServer(build_app(cfg))) as client:
        resp = await client.post("/api/halt")
        body = await resp.json()
    assert body["ok"] is True
    assert pathlib.Path(cfg["halt_path"]).exists()


# ---------------------------------------------------------------------------
# render_page (pure function)
# ---------------------------------------------------------------------------


def test_render_page_escapes_log_lines_with_html_chars():
    """
    A log line containing <script>alert(1)</script> must NOT execute as
    HTML. This is the most important security property of the dashboard.
    """
    view = {
        "rendered_at_utc": "2026-04-29T12:00:00Z",
        "snapshot_present": True,
        "snapshot_age_seconds": 1.0,
        "snapshot_is_stale": False,
        "stale_after_s": 660.0,
        "status": {"env_mode": "testnet", "risk": {}, "per_symbol": {}},
        "halted": False,
        "halt_sentinel_present": False,
        "halt_sentinel_path": "runtime/HALT",
        "log_lines": ["<script>alert('xss')</script>", "normal line"],
        "log_line_count": 2,
    }
    body = render_page(view, refresh_seconds=5)
    assert "<script>alert" not in body
    assert "&lt;script&gt;alert" in body
    assert "normal line" in body


def test_render_page_works_with_empty_view():
    view = {
        "rendered_at_utc": "2026-04-29T12:00:00Z",
        "snapshot_present": False,
        "snapshot_age_seconds": None,
        "snapshot_is_stale": False,
        "stale_after_s": 660.0,
        "status": {},
        "halted": False,
        "halt_sentinel_present": False,
        "halt_sentinel_path": "runtime/HALT",
        "log_lines": [],
        "log_line_count": 0,
    }
    body = render_page(view, refresh_seconds=5)
    assert "no snapshot" in body
    assert "no log lines" in body
