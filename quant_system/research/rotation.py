"""Cold/active research rotation — keep ~10 active per theme; cold weak names temporarily.

State is stored in Tracker system_state under key ``research_rotation`` (JSON blob).
Paper research only; never colds currently held positions.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from quant_system.config.loader import AppConfig, RotationConfig, UniverseConfig
from quant_system.tracker.api import Tracker

STATE_KEY = "research_rotation"


def _today(as_of: date | datetime | str | None = None) -> date:
    if as_of is None:
        return datetime.now(timezone.utc).date()
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return date.fromisoformat(str(as_of)[:10])


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def default_rotation_state(universe: UniverseConfig) -> dict[str, Any]:
    """Seed runtime active sets from config; empty cold map."""
    active: dict[str, list[str]] = {}
    for theme, members in universe.themes.items():
        active[theme] = [t.upper() for t in members.active]
    return {"active": active, "cold": {}, "version": 1}


def load_rotation_state(tracker: Tracker, universe: UniverseConfig) -> dict[str, Any]:
    raw = tracker.get_state(STATE_KEY, "")
    if not raw:
        return default_rotation_state(universe)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return default_rotation_state(universe)
    if not isinstance(data, dict):
        return default_rotation_state(universe)
    active = data.get("active") or {}
    if not isinstance(active, dict) or not active:
        return default_rotation_state(universe)
    out_active: dict[str, list[str]] = {}
    for theme in universe.themes:
        names = active.get(theme) or universe.active_seed(theme)
        out_active[theme] = [str(t).upper() for t in names]
    cold_raw = data.get("cold") or {}
    cold: dict[str, Any] = {}
    if isinstance(cold_raw, dict):
        for t, meta in cold_raw.items():
            if isinstance(meta, dict):
                cold[str(t).upper()] = dict(meta)
    return {"active": out_active, "cold": cold, "version": int(data.get("version") or 1)}


def save_rotation_state(tracker: Tracker, state: dict[str, Any]) -> None:
    tracker.set_state(STATE_KEY, json.dumps(state, default=str))


def held_tickers(holdings: list[dict[str, Any]] | None) -> set[str]:
    held: set[str] = set()
    for p in holdings or []:
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            held.add(str(p.get("ticker") or "").upper())
    return held


def _rotation_cfg(cfg: AppConfig) -> RotationConfig:
    return cfg.universe.rotation


def _cycle_timestamps(tracker: Tracker, lookback: int) -> list[str]:
    """Newest-first run_start timestamps (one per research cycle)."""
    rows = tracker.list_decisions(decision_type="run_start", limit=max(lookback, 1))
    return [str(r["created_at"]) for r in rows if r.get("created_at")]


def _signals_in_cycles(
    tracker: Tracker,
    ticker: str,
    cycle_starts: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Signals for ticker in lookback windows; count of cycles with ≥1 signal.

    ``cycle_starts`` is newest-first.
    """
    if not cycle_starts:
        return [], 0
    oldest = min(cycle_starts)
    all_sigs = tracker.list_signals(ticker=ticker.upper(), since=oldest, limit=5000)
    cycles_with_data = 0
    matched: list[dict[str, Any]] = []
    chronological = list(reversed(cycle_starts))
    for i, start in enumerate(chronological):
        end = chronological[i + 1] if i + 1 < len(chronological) else None
        cycle_sigs = []
        for s in all_sigs:
            ts = str(s.get("created_at") or "")
            if ts < start:
                continue
            if end is not None and ts >= end:
                continue
            cycle_sigs.append(s)
        if cycle_sigs:
            cycles_with_data += 1
            matched.extend(cycle_sigs)
    return matched, cycles_with_data


def _stats_from_signals(sigs: list[dict[str, Any]], n_cycles: int) -> dict[str, Any]:
    confidences = [float(s.get("confidence") or 0) for s in sigs]
    directions = [str(s.get("direction") or "").lower() for s in sigs]
    long_frac = sum(1 for d in directions if d == "long") / max(len(directions), 1)
    return {
        "n_signals": len(sigs),
        "n_cycles": n_cycles,
        "median_confidence": float(statistics.median(confidences)),
        "long_fraction": float(long_frac),
        "mean_confidence": float(statistics.mean(confidences)),
    }


def ticker_signal_stats(
    tracker: Tracker,
    ticker: str,
    *,
    lookback_cycles: int,
    require_full_history: bool = True,
) -> dict[str, Any] | None:
    """Aggregate stats over last K run cycles.

    When ``require_full_history`` is True (cold eligibility), needs ≥ lookback_cycles
    run_starts and ≥ lookback_cycles cycles with signals for the ticker.
    When False (checkup), any signals in the window are enough.
    """
    starts = _cycle_timestamps(tracker, lookback_cycles)
    if require_full_history and len(starts) < lookback_cycles:
        return None
    if not starts:
        # fallback: recent signals without cycle gating
        sigs = tracker.list_signals(ticker=ticker.upper(), limit=max(lookback_cycles * 5, 10))
        if not sigs:
            return None
        return _stats_from_signals(sigs, n_cycles=0)

    window = starts[:lookback_cycles] if len(starts) >= lookback_cycles else starts
    sigs, cycles_with = _signals_in_cycles(tracker, ticker, window)
    if require_full_history and cycles_with < lookback_cycles:
        return None
    if not sigs:
        return None
    return _stats_from_signals(sigs, n_cycles=cycles_with)


def is_awful(stats: dict[str, Any], rot: RotationConfig) -> bool:
    return (
        float(stats["median_confidence"]) < float(rot.min_median_confidence)
        and float(stats["long_fraction"]) < float(rot.max_long_fraction)
    )


def cold_eligible(
    ticker: str,
    *,
    active: set[str],
    holdings: set[str],
    stats: dict[str, Any] | None,
    rot: RotationConfig,
) -> bool:
    """All cold eligibility gates (strict history)."""
    u = ticker.upper()
    if u not in active:
        return False
    if u in holdings:
        return False
    if stats is None:
        return False
    return is_awful(stats, rot)


def _bench_available(
    theme: str,
    universe: UniverseConfig,
    active_map: dict[str, list[str]],
    cold: dict[str, Any],
) -> list[str]:
    """Bench names not currently active and not cold."""
    seed = universe.bench_seed(theme)
    active = {t.upper() for t in active_map.get(theme) or []}
    cold_set = {t.upper() for t in cold}
    return [t for t in seed if t.upper() not in active and t.upper() not in cold_set]


def _ensure_theme_active(
    active_map: dict[str, list[str]],
    cold: dict[str, Any],
    theme: str,
    universe: UniverseConfig,
) -> str | None:
    """Guarantee ≥1 active name. Returns promoted ticker if any."""
    active = active_map.setdefault(theme, [])
    if active:
        return None
    for cand in _bench_available(theme, universe, active_map, cold):
        active.append(cand.upper())
        return cand.upper()
    seed = universe.active_seed(theme)
    if seed:
        # prefer a seed name that is not cold
        for s in seed:
            u = s.upper()
            if u not in cold:
                active.append(u)
                return u
        active.append(seed[0].upper())
        cold.pop(seed[0].upper(), None)
        return seed[0].upper()
    return None


def select_research_universe(
    tracker: Tracker,
    cfg: AppConfig,
    as_of_date: date | datetime | str | None = None,
) -> list[str]:
    """Active tickers plus checkup-due cold names for this cycle only."""
    state = load_rotation_state(tracker, cfg.universe)
    today = _today(as_of_date)
    out: list[str] = []
    seen: set[str] = set()
    for _theme, names in (state.get("active") or {}).items():
        for t in names:
            u = str(t).upper()
            if u not in seen:
                seen.add(u)
                out.append(u)
    cold = state.get("cold") or {}
    for ticker, meta in cold.items():
        checkup = _parse_date((meta or {}).get("checkup_date"))
        if checkup is not None and checkup <= today:
            u = str(ticker).upper()
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out or list(cfg.universe.tickers)


def _weakest_active(
    tracker: Tracker,
    theme: str,
    active_map: dict[str, list[str]],
    holdings: set[str],
    rot: RotationConfig,
) -> str | None:
    """Pick weakest non-held active in theme by median confidence (lower = weaker)."""
    actives = [t.upper() for t in active_map.get(theme) or []]
    candidates: list[tuple[float, str]] = []
    for t in actives:
        if t in holdings:
            continue
        stats = ticker_signal_stats(
            tracker, t, lookback_cycles=rot.lookback_cycles, require_full_history=False
        )
        score = float(stats["median_confidence"]) if stats else 50.0
        if stats:
            score = score - (1.0 - float(stats["long_fraction"])) * 5.0
        candidates.append((score, t))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def update_rotation_after_cycle(
    tracker: Tracker,
    cfg: AppConfig,
    signals_summary: list[dict[str, Any]] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    *,
    as_of_date: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Apply cold / revive / promote logic after a research cycle.

    Returns events: went_cold, extended, revived, promoted.
    """
    _ = signals_summary
    universe = cfg.universe
    rot = _rotation_cfg(cfg)
    state = load_rotation_state(tracker, universe)
    today = _today(as_of_date)
    held = held_tickers(holdings if holdings is not None else tracker.list_positions())

    events: dict[str, list[dict[str, Any]]] = {
        "went_cold": [],
        "extended": [],
        "revived": [],
        "promoted": [],
    }

    cold: dict[str, Any] = dict(state.get("cold") or {})
    active_map: dict[str, list[str]] = {
        th: [x.upper() for x in names]
        for th, names in (state.get("active") or {}).items()
    }
    for theme in universe.themes:
        active_map.setdefault(theme, [t.upper() for t in universe.active_seed(theme)])

    # --- Checkup-due cold names: extend or revive ---
    for ticker, meta in list(cold.items()):
        checkup = _parse_date((meta or {}).get("checkup_date"))
        if checkup is None or checkup > today:
            continue
        theme = str((meta or {}).get("theme") or universe.theme_for(ticker) or "")
        stats = ticker_signal_stats(
            tracker, ticker, lookback_cycles=rot.lookback_cycles, require_full_history=False
        )
        still_bad = stats is None or is_awful(stats, rot)
        if still_bad:
            new_check = (today + timedelta(days=rot.cold_days)).isoformat()
            cold[ticker] = {
                **(meta or {}),
                "checkup_date": new_check,
                "reason": (meta or {}).get("reason") or "extended_after_checkup",
                "extended_at": today.isoformat(),
                "theme": theme,
            }
            events["extended"].append(
                {
                    "ticker": ticker,
                    "theme": theme,
                    "checkup_date": new_check,
                    "reason": "still_awful_or_thin" if stats is None else "still_awful",
                }
            )
            continue

        # Revive
        del cold[ticker]
        theme_active = active_map.setdefault(theme, [])
        if ticker.upper() not in theme_active:
            target = rot.active_target_per_theme
            if len(theme_active) >= target:
                victim = _weakest_active(tracker, theme, active_map, held, rot)
                if victim and victim != ticker.upper() and victim not in held:
                    theme_active[:] = [x for x in theme_active if x != victim]
                    demote_check = (today + timedelta(days=rot.cold_days)).isoformat()
                    cold[victim] = {
                        "theme": theme,
                        "checkup_date": demote_check,
                        "reason": f"demoted_for_revive:{ticker}",
                        "went_cold_at": today.isoformat(),
                    }
                    events["went_cold"].append(
                        {
                            "ticker": victim,
                            "theme": theme,
                            "checkup_date": demote_check,
                            "reason": f"demoted_for_revive:{ticker}",
                        }
                    )
            theme_active.append(ticker.upper())
        events["revived"].append(
            {
                "ticker": ticker,
                "theme": theme,
                "reason": "checkup_improved",
                "median_confidence": stats.get("median_confidence"),
                "long_fraction": stats.get("long_fraction"),
            }
        )

    # --- New cold from active set (cap per theme) ---
    for theme in universe.themes:
        theme_active = list(active_map.get(theme) or [])
        active_set = set(theme_active)
        newly_cold = 0
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for t in theme_active:
            stats = ticker_signal_stats(
                tracker, t, lookback_cycles=rot.lookback_cycles, require_full_history=True
            )
            if not cold_eligible(
                t, active=active_set, holdings=held, stats=stats, rot=rot
            ):
                continue
            assert stats is not None
            ranked.append((float(stats["median_confidence"]), t, stats))
        ranked.sort()
        for _score, t, stats in ranked:
            if newly_cold >= rot.max_new_cold_per_theme_per_cycle:
                break
            if t in held or t in cold:
                continue
            promote_from = _bench_available(theme, universe, active_map, cold)
            remaining_after = [x for x in theme_active if x != t]
            if not remaining_after and not promote_from:
                continue
            checkup = (today + timedelta(days=rot.cold_days)).isoformat()
            reason = (
                f"awful:median_conf={stats['median_confidence']:.1f},"
                f"long_frac={stats['long_fraction']:.2f}"
            )
            cold[t] = {
                "theme": theme,
                "checkup_date": checkup,
                "reason": reason,
                "went_cold_at": today.isoformat(),
            }
            theme_active = [x for x in theme_active if x != t]
            events["went_cold"].append(
                {
                    "ticker": t,
                    "theme": theme,
                    "checkup_date": checkup,
                    "reason": reason,
                }
            )
            newly_cold += 1
            if promote_from and len(theme_active) < rot.active_target_per_theme:
                promo = promote_from[0].upper()
                theme_active.append(promo)
                events["promoted"].append(
                    {
                        "ticker": promo,
                        "theme": theme,
                        "reason": f"replace_cold:{t}",
                    }
                )
            active_map[theme] = theme_active

    for theme in universe.themes:
        promoted = _ensure_theme_active(active_map, cold, theme, universe)
        if promoted:
            events["promoted"].append(
                {"ticker": promoted, "theme": theme, "reason": "ensure_nonzero_active"}
            )

    new_state = {"active": active_map, "cold": cold, "version": 1}
    save_rotation_state(tracker, new_state)
    return events


def rotation_digest_section(
    tracker: Tracker, cfg: AppConfig | None = None
) -> dict[str, Any]:
    """Compact cold/rotation view for digest / dashboard (additive)."""
    from quant_system.config.loader import load_config

    app = cfg or load_config()
    state = load_rotation_state(tracker, app.universe)
    cold_list = []
    for ticker, meta in sorted((state.get("cold") or {}).items()):
        cold_list.append(
            {
                "ticker": ticker,
                "checkup_date": (meta or {}).get("checkup_date"),
                "reason": (meta or {}).get("reason"),
                "theme": (meta or {}).get("theme") or app.universe.theme_for(ticker),
            }
        )
    active = {th: list(names) for th, names in (state.get("active") or {}).items()}
    return {
        "active": active,
        "cold": cold_list,
        "settings": app.universe.rotation.model_dump(),
    }
