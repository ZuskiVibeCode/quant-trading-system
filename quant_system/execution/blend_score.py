"""Shared agent-score blend helpers for PortfolioManager and trade review.

Momentum absolute-share cap
---------------------------
After per-agent weight floor/cap, each desk contributes::

    contrib_i = direction_i * (confidence_i / 100) * w_i

Let ``other_abs = sum(|contrib_j| for j != momentum)``. When ``other_abs > 0``
and ``|contrib_momentum| / (|contrib_momentum| + other_abs)`` would exceed
``momentum_max_abs_share`` (default 0.40), scale the Momentum contribution so
its share of the absolute blend equals that cap::

    |contrib_m'| = (max_share / (1 - max_share)) * other_abs

When every other live desk contributes 0 (all neutral / zero weight), leave
Momentum uncapped — there is nothing for it to drown. This does **not** change
live confidence floors (55 buy / 40 sell / |score| 0.05).
"""

from __future__ import annotations

from typing import Any

MOMENTUM_AGENT_ID = "momentum"
DEFAULT_MOMENTUM_MAX_ABS_SHARE = 0.40


def clamp_agent_weight(raw: float, *, floor: float, cap: float) -> float:
    """Apply positive-weight floor/cap used by PortfolioManager."""
    w = float(raw)
    if w <= 0:
        return 0.0
    return max(float(floor), min(float(cap), w))


def apply_momentum_abs_share_cap(
    contributions: list[dict[str, Any]],
    *,
    max_share: float = DEFAULT_MOMENTUM_MAX_ABS_SHARE,
) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
    """Clip Momentum's |contrib| so it is at most ``max_share`` of abs blend.

    Returns a shallow-copied contribution list (adds ``contrib_raw`` /
    ``capped`` when Momentum is scaled) plus ``(score, detail, meta)``.
    """
    share = float(max_share)
    if share <= 0 or share >= 1:
        raise ValueError(f"momentum_max_abs_share must be in (0, 1); got {max_share!r}")

    detail: list[dict[str, Any]] = [dict(row) for row in contributions]
    mom_idxs = [
        i for i, row in enumerate(detail) if str(row.get("agent_id") or "") == MOMENTUM_AGENT_ID
    ]
    other_abs = 0.0
    mom_raw = 0.0
    for i, row in enumerate(detail):
        c = float(row.get("contrib") or 0.0)
        if i in mom_idxs:
            mom_raw += c
        else:
            other_abs += abs(c)

    meta: dict[str, Any] = {
        "momentum_max_abs_share": share,
        "momentum_raw_contrib": mom_raw,
        "other_abs_contrib": other_abs,
        "momentum_capped": False,
        "momentum_scale": 1.0,
    }

    if not mom_idxs:
        score = sum(float(row.get("contrib") or 0.0) for row in detail)
        meta["momentum_abs_share"] = 0.0
        return score, detail, meta

    if other_abs <= 0:
        score = sum(float(row.get("contrib") or 0.0) for row in detail)
        meta["momentum_abs_share"] = 1.0 if abs(mom_raw) > 0 else 0.0
        meta["cap_skipped_reason"] = "no_other_desk_abs_contrib"
        return score, detail, meta

    max_mom_abs = (share / (1.0 - share)) * other_abs
    raw_abs = abs(mom_raw)
    meta["momentum_abs_share_raw"] = raw_abs / (raw_abs + other_abs)

    if raw_abs > max_mom_abs + 1e-15:
        scale = max_mom_abs / raw_abs
        for i in mom_idxs:
            row = detail[i]
            raw_c = float(row.get("contrib") or 0.0)
            row["contrib_raw"] = raw_c
            row["contrib"] = raw_c * scale
            row["capped"] = True
        meta["momentum_capped"] = True
        meta["momentum_scale"] = scale
        mom_final = mom_raw * scale
    else:
        mom_final = mom_raw

    score = sum(float(row.get("contrib") or 0.0) for row in detail)
    final_abs = abs(mom_final)
    meta["momentum_final_contrib"] = mom_final
    meta["momentum_abs_share"] = final_abs / (final_abs + other_abs)
    return score, detail, meta
