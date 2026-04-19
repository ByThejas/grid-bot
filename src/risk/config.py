"""
RiskConfig — caps and limits loaded from settings.yaml `risk:` section.

WHY a dedicated dataclass:
The risk layer has a small handful of independently-tunable knobs that the
operator might want to change without touching code. Centralising the
mapping from YAML to typed fields keeps the engine integration tidy.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    # Hard cap on the total number of open orders across ALL engines.
    # Stops a runaway loop from carpeting the book during a bug.
    max_open_orders: int

    # Daily realized loss floor (USDT, positive number). When realized PnL
    # for the rolling 24h window drops below -daily_loss_limit_usd, the
    # kill switch trips. Realized PnL is FIFO-matched buy→sell pairs.
    daily_loss_limit_usd: float

    # Sentinel file. If this path exists on disk the engine treats it as
    # an external HALT signal — stops new placements and triggers shutdown.
    # Operator can `touch runtime/HALT` from outside the process.
    kill_switch_path: str = "runtime/HALT"

    # How often the runner polls the kill switch file + checks loss floor.
    poll_interval_s: float = 2.0

    @classmethod
    def from_yaml(cls, raw: dict) -> "RiskConfig":
        r = raw.get("risk", {}) or {}
        return cls(
            max_open_orders=int(r.get("max_open_orders", 50)),
            daily_loss_limit_usd=float(r.get("daily_loss_limit_usd", 500.0)),
            kill_switch_path=str(r.get("kill_switch_path", "runtime/HALT")),
            poll_interval_s=float(r.get("poll_interval_s", 2.0)),
        )
