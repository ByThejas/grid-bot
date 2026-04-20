"""
RiskConfig — caps and limits loaded from settings.yaml `risk:` section.

WHY a dedicated dataclass:
The risk layer has a small handful of independently-tunable knobs that the
operator might want to change without touching code. Centralising the
mapping from YAML to typed fields keeps the engine integration tidy.

MAINNET OVERRIDES (Phase 10)
When we're running against mainnet we want stricter defaults than testnet
— real money warrants a tighter leash until a sustained canary proves
the strategy behaves. Rather than force the operator to edit two YAML
files or keep forgetting to dial things down, `settings.yaml` carries
an optional `risk_overrides.mainnet` block whose keys shadow the base
`risk:` block only when env_mode == "mainnet". Testnet reads the base
values untouched.
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
    def from_yaml(cls, raw: dict, env_mode: str = "testnet") -> "RiskConfig":
        """
        Build from settings.yaml. Pass env_mode="mainnet" to merge the
        `risk_overrides.mainnet` block over the base `risk:` block. Unknown
        env_mode values behave like testnet (no overrides applied) — we
        don't want a typo in the env name to silently switch to looser
        limits.
        """
        base = dict(raw.get("risk", {}) or {})
        if env_mode == "mainnet":
            overrides = (raw.get("risk_overrides", {}) or {}).get("mainnet", {}) or {}
            base.update(overrides)
        return cls(
            max_open_orders=int(base.get("max_open_orders", 50)),
            daily_loss_limit_usd=float(base.get("daily_loss_limit_usd", 500.0)),
            kill_switch_path=str(base.get("kill_switch_path", "runtime/HALT")),
            poll_interval_s=float(base.get("poll_interval_s", 2.0)),
        )
