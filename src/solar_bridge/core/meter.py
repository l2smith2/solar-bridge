"""Per-phase meter model shared by the Solar API meter endpoint and the Modbus meter.

Derivations when a per-phase sensor is not configured:
  P    no per-phase power sensors → total split equally (3-phase) or all on phase 1
  V    nominal DEFAULT_GRID_VOLTAGE (only used for derivations; not reported by HTTP)
  I    P / V
  S    I * V  (credible when P≈0 but I/Q are not)
  Q    0
  PF   P / S, or ±1 by direction when S is not positive
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_GRID_VOLTAGE = 240.0


@dataclass(frozen=True, slots=True)
class PhaseReading:
    """Electrical values for one phase."""

    p: float
    i: float
    v: float
    v_measured: float | None
    s: float
    q: float
    pf: float


@dataclass(frozen=True, slots=True)
class MeterReading:
    """Electrical values for the whole meter. `phases` holds the active phases only."""

    p: float
    i: float
    s: float
    q: float
    pf: float
    phases: tuple[PhaseReading, ...]
    wh_imp: float
    wh_exp: float


def power_factor(p: float, s: float, measured: float | None = None) -> float:
    """Measured PF if available, else P/S, else ±1 by power direction."""
    if measured is not None:
        return measured
    return round(p / s, 3) if s > 0 else (1.0 if p >= 0 else -1.0)


def read_meter(data: Mapping[str, Any]) -> MeterReading:
    """Build the meter model from coordinator data (P_Grid positive = import)."""
    p_total = data.get("P_Grid") or 0.0
    names = "ABC" if int(data.get("grid_phases") or 1) == 3 else "A"

    measured_p = [data.get(f"P_Grid_{x}") for x in names]
    if all(p is None for p in measured_p):
        powers = [p_total / len(names)] * len(names)
    else:
        powers = [p or 0.0 for p in measured_p]

    phases = []
    for x, p in zip(names, powers, strict=True):
        v_measured = data.get(f"V_Grid_{x}")
        v = v_measured or DEFAULT_GRID_VOLTAGE
        i = data.get(f"I_Grid_{x}")
        if i is None:
            i = p / v
        s = i * v
        phases.append(
            PhaseReading(
                p=p,
                i=i,
                v=v,
                v_measured=v_measured,
                s=s,
                q=data.get(f"Q_Grid_{x}") or 0.0,
                pf=power_factor(p, s, data.get(f"PF_Grid_{x}")),
            )
        )

    s_total = sum(ph.s for ph in phases)
    return MeterReading(
        p=p_total,
        i=sum(ph.i for ph in phases),
        s=s_total,
        q=sum(ph.q for ph in phases),
        pf=power_factor(p_total, s_total),
        phases=tuple(phases),
        wh_imp=data.get("_tot_wh_imp") or 0.0,
        wh_exp=data.get("_tot_wh_exp") or 0.0,
    )
