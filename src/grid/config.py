"""
GridEngineConfig — per-symbol parameters loaded from settings.yaml.

WHY a dedicated dataclass (and not just pass the raw config dict)?
- The engine has ~8 parameters. Passing them as kwargs is noisy; passing
  a dict means stringly-typed access (config["order_size"]) that loses
  autocomplete.
- from_yaml() centralises the mapping from settings.yaml's structure to
  engine inputs. When settings.yaml changes shape, only this file moves.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GridEngineConfig:
    symbol: str
    levels_above: int
    levels_below: int
    base_spacing_pct: float
    min_spacing_pct: float
    max_spacing_pct: float
    order_size: float             # base-asset quantity per grid level
    recenter_threshold_pct: float
    drift_check_interval_s: float = 5.0   # how often to poll ticker for drift

    @classmethod
    def from_yaml(cls, symbol: str, raw: dict) -> "GridEngineConfig":
        """
        Build from the loaded settings.yaml root. `raw` is the full config
        dict; we dig into `raw["grid"]` and `raw["grid"]["order_size"][symbol]`.
        """
        g = raw["grid"]
        order_size_map = g.get("order_size", {})
        if symbol not in order_size_map:
            raise ValueError(f"no grid.order_size configured for {symbol}")
        return cls(
            symbol=symbol,
            levels_above=int(g["levels_above"]),
            levels_below=int(g["levels_below"]),
            base_spacing_pct=float(g["base_spacing_pct"]),
            min_spacing_pct=float(g["min_spacing_pct"]),
            max_spacing_pct=float(g["max_spacing_pct"]),
            order_size=float(order_size_map[symbol]),
            recenter_threshold_pct=float(g["recenter_threshold_pct"]),
        )
