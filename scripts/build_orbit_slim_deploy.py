#!/usr/bin/env python3
"""Build MCP-friendly slim docs deploy tree from docs/data/latest.json."""
from __future__ import annotations
import argparse, json, shutil
from pathlib import Path

def trunc(s, n=250):
    if not isinstance(s, str):
        return s
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

def slim_signal(s, reason_n=200):
    out = {
        "agent_id": s.get("agent_id"),
        "ticker": s.get("ticker"),
        "direction": s.get("direction"),
        "confidence": s.get("confidence"),
        "reasoning": trunc(s.get("reasoning") or "", reason_n),
        # Keep the export's summarized research metrics in the slim deploy.
        # The dashboard detail dialog relies on this field being present.
        "supporting_data": s.get("supporting_data"),
        "created_at": s.get("created_at"),
    }
    if s.get("signal_id") is not None:
        out["signal_id"] = s.get("signal_id")
    return out

def slim_ticket(t):
    if not isinstance(t, dict):
        return t
    keep = ["id", "ticker", "side", "qty", "order_type", "rationale", "signal_ids", "status", "limit_price", "created_at"]
    out = {k: t.get(k) for k in keep if k in t}
    if "rationale" in out:
        out["rationale"] = trunc(out["rationale"], 400)
    return out

def slim_buy_sell(bs):
    if not isinstance(bs, dict):
        return bs
    out = {}
    if "ticket" in bs:
        out["ticket"] = slim_ticket(bs["ticket"])
    if isinstance(bs.get("fill"), dict):
        f = bs["fill"]
        out["fill"] = {k: f.get(k) for k in ["id", "ticker", "side", "qty", "price", "filled_at", "status"] if k in f}
    elif "fill" in bs:
        out["fill"] = bs["fill"]
    if isinstance(bs.get("signals"), list):
        out["signals"] = [slim_signal(s, 280) for s in bs["signals"]]
    return out

def slim_decision(row, reason_n=160):
    if not isinstance(row, dict):
        return row
    out = {k: row.get(k) for k in ["id", "decision_type", "actor", "ticker", "created_at"] if k in row}
    out["reasoning"] = trunc(row.get("reasoning") or "", reason_n)
    p = row.get("payload")
    if isinstance(p, dict):
        tiny = {k: p[k] for k in ["ticket_id", "ticker", "side", "qty", "order_type", "error", "status"] if k in p}
        if "error" in tiny:
            tiny["error"] = trunc(str(tiny["error"]), 160)
        if tiny:
            out["payload"] = tiny
    return out

def top_signals(sigs, n=18):
    ranked = sorted(sigs or [], key=lambda s: abs(float(s.get("confidence") or 0)), reverse=True)
    picked = []
    for s in ranked:
        t = s.get("ticker")
        if sum(1 for x in picked if x.get("ticker") == t) >= 2 and len(picked) < n:
            continue
        picked.append(s)
        if len(picked) >= n:
            break
    if len(picked) < n:
        for s in ranked:
            if s not in picked:
                picked.append(s)
            if len(picked) >= n:
                break
    return picked


def slim_review(r):
    """Pass through trade review; truncate long rationales/reasoning only."""
    if not isinstance(r, dict):
        return r
    keep = [
        "ticker", "qty", "entry", "exit", "pnl", "pnl_pct",
        "opened_at", "closed_at", "hold_hours",
        "exit_reason", "exit_rationale",
        "entry_ticket_id", "exit_ticket_id", "entry_fill_id", "exit_fill_id",
        "entry_score", "agents_at_entry", "mix", "path",
        "verdict", "verdict_tags", "completeness", "evidence", "mode", "sector",
    ]
    out = {k: r.get(k) for k in keep if k in r}
    if "exit_rationale" in out:
        out["exit_rationale"] = trunc(out.get("exit_rationale") or "", 220)
    if "verdict" in out:
        out["verdict"] = trunc(out.get("verdict") or "", 500)
    agents = out.get("agents_at_entry")
    if isinstance(agents, list):
        slim_agents = []
        for a in agents:
            if not isinstance(a, dict):
                continue
            sa = {k: a.get(k) for k in ["agent_id", "direction", "confidence", "signal_id", "created_at"] if k in a}
            if "reasoning_trunc" in a:
                sa["reasoning_trunc"] = trunc(a.get("reasoning_trunc") or "", 140)
            slim_agents.append(sa)
        out["agents_at_entry"] = slim_agents
    ev = out.get("evidence")
    if isinstance(ev, list):
        slim_ev = []
        for e in ev[:40]:
            if not isinstance(e, dict):
                continue
            slim_ev.append({
                "kind": e.get("kind"),
                "id": e.get("id"),
                "label": e.get("label"),
                "detail": trunc(e.get("detail") or "", 220),
            })
        out["evidence"] = slim_ev
    return out

def slim_latest(d: dict) -> dict:
    out = {
        "meta": d.get("meta"),
        "snapshot": d.get("snapshot"),
        "holdings": [],
        "closed": [],
        "equity_curve": d.get("equity_curve") or [],
        "agents_today": {},
        "agents_history": d.get("agents_history") or {},
        "decisions_today": {},
        "auditor_flags_today": d.get("auditor_flags_today") or [],
        "mission_log": (d.get("mission_log") or [])[-20:],
        "watchlist": [],
        "sectors": d.get("sectors") or {},
        "sector_board": d.get("sector_board") or [],
        "ticker_pnl": d.get("ticker_pnl") or [],
        "trade_reviews": [slim_review(r) for r in (d.get("trade_reviews") or []) if isinstance(r, dict)],
        "rotation": d.get("rotation"),
        "cold": d.get("cold") or [],
        "zones": d.get("zones"),
        "zones_series": d.get("zones_series"),
    }
    for h in d.get("holdings") or []:
        nh = {k: h.get(k) for k in ["ticker", "qty", "avg_cost", "last", "sector", "unrealized", "updated_at", "opened_at", "fills"] if k in h}
        if "buy" in h:
            nh["buy"] = slim_buy_sell(h["buy"])
        out["holdings"].append(nh)
    for c in d.get("closed") or []:
        nc = {k: c.get(k) for k in ["ticker", "qty", "entry", "exit", "pnl", "sector", "closed_at", "opened_at", "mode", "fill_id", "ticket_id"] if k in c}
        if "buy" in c:
            nc["buy"] = slim_buy_sell(c["buy"])
        if "sell" in c:
            nc["sell"] = slim_buy_sell(c["sell"])
        if isinstance(c.get("review"), dict):
            nc["review"] = slim_review(c["review"])
        out["closed"].append(nc)
    for aid, block in (d.get("agents_today") or {}).items():
        nb = {k: block.get(k) for k in ["agent_id", "count", "long", "short", "neutral"] if k in block}
        nb["signals"] = [slim_signal(s, 180) for s in top_signals(block.get("signals") or [], 18)]
        out["agents_today"][aid] = nb
    dt = d.get("decisions_today") or {}
    out["decisions_today"] = {
        "counts": dt.get("counts") or {},
        "tickets": [slim_decision(x) for x in (dt.get("tickets") or [])],
        "holds": [slim_decision(x, 120) for x in (dt.get("holds") or [])[:25]],
        "fills": [slim_decision(x) for x in (dt.get("fills") or [])],
        "order_submitted": [slim_decision(x) for x in (dt.get("order_submitted") or [])],
        "failed": [slim_decision(x) for x in (dt.get("failed") or [])],
    }
    for w in d.get("watchlist") or []:
        nw = {"ticker": w.get("ticker"), "sector": w.get("sector")}
        s = w.get("signal")
        if isinstance(s, dict):
            nw["signal"] = {
                "agent_id": s.get("agent_id"),
                "direction": s.get("direction"),
                "confidence": s.get("confidence"),
                "reasoning": trunc(s.get("reasoning") or "", 180),
                "created_at": s.get("created_at"),
            }
        out["watchlist"].append(nw)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=Path, default=Path("docs"))
    ap.add_argument("--out", type=Path, default=Path("/workspace/orbit-full-deploy"))
    args = ap.parse_args()
    docs, out = args.docs, args.out
    (out / "art").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    d = json.loads((docs / "data" / "latest.json").read_text())
    slim = slim_latest(d)
    raw = json.dumps(slim, separators=(",", ":"))
    (out / "data" / "latest.json").write_text(raw)
    # zones_history is required for detail charts when zones_series is trimmed
    zh = docs / "data" / "zones_history.json"
    if zh.exists():
        shutil.copy2(zh, out / "data" / "zones_history.json")
    schema = docs / "SCHEMA.md"
    if schema.exists():
        shutil.copy2(schema, out / "SCHEMA.md")
    for name in ["index.html", "styles.css", "app.js", "vercel.json"]:
        shutil.copy2(docs / name, out / name)
    for img in (docs / "art").glob("*.jpg"):
        shutil.copy2(img, out / "art" / img.name)
    print(f"wrote {out} latest.json={len(raw)} bytes")

if __name__ == "__main__":
    main()
