#!/usr/bin/env python3
"""Build a slim single-file docs/index.production.html for MCP Vercel deploys.

Embeds only meta/snapshot/holdings/closed with slim buy/sell bundles:
  ticket.rationale, signals[agent_id/direction/confidence/reasoning], fill price/qty/side.

Strips supporting_data and other fat fields. Caps signals and truncates reasoning
so the HTML stays under ~70KB (MCP deploy arg limits). Never embeds empty
{"reasoning":""} stubs — the viewer shows "No reasoning recorded" instead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MAX_SIGNALS = 5
MAX_REASON = 240
TARGET_HTML_BYTES = 75000


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>APHELION — Paper Orbit Ledger</title>
<meta name="description" content="Paper-only multi-agent quant book. Open + closed positions with real buy/sell reasoning."/>
<style>
:root{--bg:#0b0d10;--panel:#12161e;--line:#2a2f3a;--text:#e8eaed;--muted:#9aa3b2;--accent:#7dd3fc;--pos:#3ddc97;--neg:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,sans-serif}
header,main{max-width:960px;margin:0 auto;padding:1rem 1.1rem}
h1{font-size:1.4rem;letter-spacing:.04em;margin:.2rem 0}
h2{font-size:1.05rem;margin:1.4rem 0 .55rem}
.pill{display:inline-block;padding:.12rem .55rem;border-radius:999px;background:#1b2230;margin-left:.35rem;font-size:.78rem;color:var(--muted)}
.card{border:1px solid var(--line);border-radius:12px;padding:.85rem 1rem;margin:.55rem 0;background:var(--panel)}
.row{display:flex;justify-content:space-between;gap:.75rem;flex-wrap:wrap;align-items:baseline}
.subtle{opacity:.7;font-size:.85rem;color:var(--muted)}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.kicker{text-transform:uppercase;letter-spacing:.08em;font-size:.68rem;opacity:.55;margin:0 0 .35rem}
details{margin-top:.55rem}
summary{cursor:pointer;list-style:none;color:var(--accent);font-weight:500}
summary::-webkit-details-marker{display:none}
summary:hover{opacity:.9}
.block{margin-top:.65rem;padding-top:.55rem;border-top:1px solid var(--line)}
.thesis{margin:.25rem 0 .45rem;white-space:pre-wrap}
.agent{margin:.45rem 0;padding:.45rem .55rem;border-radius:8px;background:#0e1219;border:1px solid #1e2530}
.agent-top{display:flex;justify-content:space-between;gap:.5rem;flex-wrap:wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem}
.dir{text-transform:uppercase;font-size:.75rem;padding:.05rem .35rem;border-radius:4px;background:#1b2230}
.dir.long,.dir.buy{color:var(--pos)}.dir.short,.dir.sell{color:var(--neg)}.dir.neutral{color:var(--muted)}
.reason{margin:.3rem 0 0;color:#d5dbe6;font-size:.9rem;white-space:pre-wrap}
.empty{color:var(--muted);font-style:italic;margin:.25rem 0}
.meta{display:flex;gap:.65rem;flex-wrap:wrap;color:var(--muted);font-size:.82rem;margin-top:.3rem}
.snap{color:var(--muted);margin:.4rem 0 0}
footer{max-width:960px;margin:0 auto;padding:0 1.1rem 2rem;color:var(--muted);font-size:.8rem}
</style>
</head>
<body>
<header>
  <p class="kicker">Quantum · Robotics · Deep space</p>
  <h1>APHELION <span class="pill">paper</span></h1>
  <p class="subtle">Orbit Quant Ledger · open + closed with real buy/sell reasoning</p>
  <p class="snap" id="snap"></p>
</header>
<main>
  <section>
    <h2>Open <span class="pill" id="openN"></span></h2>
    <div id="open"></div>
  </section>
  <section>
    <h2>Closed <span class="pill" id="closedN"></span></h2>
    <div id="closed"></div>
  </section>
</main>
<footer>Paper-only. No live capital. Reasoning from Tracker tickets + agent signals.</footer>
<script>
const DATA = __DATA__;
const $ = (id) => document.getElementById(id);
const usd = (n) => (n == null || Number.isNaN(Number(n))) ? "—" : ("$" + Number(n).toFixed(2));
const cls = (n) => (n == null || Number.isNaN(Number(n))) ? "" : (Number(n) >= 0 ? "pos" : "neg");
const esc = (s) => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const conf = (n) => (n == null || Number.isNaN(Number(n))) ? "—" : Number(n).toFixed(0);

function prose(s) {
  const t = (s == null ? "" : String(s)).trim();
  return t ? esc(t) : "";
}

function fillHtml(fill) {
  if (!fill || (fill.price == null && fill.qty == null)) return "";
  const bits = [];
  if (fill.side) bits.push(esc(fill.side));
  if (fill.qty != null) bits.push(Number(fill.qty).toFixed(4) + " sh");
  if (fill.price != null) bits.push("@ " + usd(fill.price));
  return bits.length ? `<div class="meta">Fill: ${bits.join(" · ")}</div>` : "";
}

function signalsHtml(signals) {
  if (!signals || !signals.length) {
    return `<p class="empty">No agent signals recorded</p>`;
  }
  return signals.map((s) => {
    const reason = prose(s.reasoning);
    return `<div class="agent">
      <div class="agent-top">
        <span>${esc(s.agent_id || "agent")}</span>
        <span><span class="dir ${esc(s.direction || "")}">${esc(s.direction || "—")}</span> conf ${conf(s.confidence)}</span>
      </div>
      ${reason ? `<p class="reason">${reason}</p>` : `<p class="empty">No reasoning recorded</p>`}
    </div>`;
  }).join("");
}

/** Human-readable buy/sell block — never dump empty objects. */
function legHtml(bundle, title) {
  if (!bundle || typeof bundle !== "object") {
    return `<details open><summary><strong>${esc(title)}</strong></summary><p class="empty">No reasoning recorded</p></details>`;
  }
  const ticket = bundle.ticket || null;
  const rationale = prose(ticket && ticket.rationale);
  const signals = bundle.signals || [];
  const hasFill = bundle.fill && (bundle.fill.price != null || bundle.fill.qty != null);
  const hasAnything = rationale || signals.length || hasFill;
  if (!hasAnything) {
    // Also treat legacy stub {reasoning:""} as empty — never stringify it.
    const legacy = prose(bundle.reasoning) || prose(bundle.summary);
    if (legacy) {
      return `<details><summary><strong>${esc(title)}</strong> <span class="subtle">tap for details</span></summary><p class="thesis">${legacy}</p></details>`;
    }
    return `<details><summary><strong>${esc(title)}</strong> <span class="subtle">tap for details</span></summary><p class="empty">No reasoning recorded</p></details>`;
  }
  return `<details>
    <summary><strong>${esc(title)}</strong> <span class="subtle">tap for details</span></summary>
    <div class="block">
      <p class="kicker">PM ticket</p>
      ${rationale ? `<p class="thesis">${rationale}</p>` : `<p class="empty">No reasoning recorded</p>`}
      ${fillHtml(bundle.fill)}
      <p class="kicker" style="margin-top:.7rem">Agent signals</p>
      ${signalsHtml(signals)}
    </div>
  </details>`;
}

function openCard(h) {
  return `<article class="card">
    <p class="kicker">Open position · ${esc(h.sector || "")}</p>
    <div class="row"><strong>${esc(h.ticker)}</strong><span class="${cls(h.unrealized)}">${usd(h.unrealized)}</span></div>
    <div class="row subtle"><span>qty ${esc(h.qty)}</span><span>avg ${usd(h.avg_cost)}</span><span>last ${usd(h.last)}</span></div>
    ${legHtml(h.buy, "Why bought")}
  </article>`;
}

function closedCard(c) {
  return `<article class="card">
    <p class="kicker">Closed position · ${esc(c.sector || "")}</p>
    <div class="row"><strong>${esc(c.ticker)}</strong><span class="${cls(c.pnl)}">${usd(c.pnl)} PnL</span></div>
    <div class="row subtle"><span>in ${usd(c.entry)}</span><span>out ${usd(c.exit)}</span></div>
    ${legHtml(c.buy, "Why bought")}
    ${legHtml(c.sell, "Why sold")}
  </article>`;
}

const snap = DATA.snapshot || {};
const meta = DATA.meta || {};
$("snap").textContent = `Equity ${usd(snap.equity)} · cash ${usd(snap.cash)} · exported ${meta.exported_at || "—"}`;
$("openN").textContent = String((DATA.holdings || []).length);
$("closedN").textContent = String((DATA.closed || []).length);
$("open").innerHTML = (DATA.holdings || []).map(openCard).join("") || '<p class="subtle">No open</p>';
$("closed").innerHTML = (DATA.closed || []).map(closedCard).join("") || '<p class="subtle">No closed</p>';
</script>
</body>
</html>
"""


def _trunc(s: object, n: int = MAX_REASON) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    if len(t) > n:
        return t[: n - 1] + "…"
    return t


def _slim_fill(fill: object) -> dict | None:
    if not isinstance(fill, dict):
        return None
    out = {}
    for k in ("price", "qty", "side"):
        if fill.get(k) is not None and fill.get(k) != "":
            out[k] = fill[k]
    return out or None


def _slim_signals(signals: object, *, max_signals: int, max_reason: int) -> list:
    if not isinstance(signals, list):
        return []
    out = []
    for s in signals[:max_signals]:
        if not isinstance(s, dict):
            continue
        row = {}
        if s.get("agent_id") is not None:
            row["agent_id"] = s["agent_id"]
        if s.get("direction") is not None:
            row["direction"] = s["direction"]
        if s.get("confidence") is not None:
            row["confidence"] = s["confidence"]
        reason = _trunc(s.get("reasoning"), max_reason)
        if reason:
            row["reasoning"] = reason
        if row:
            out.append(row)
    return out


def slim_leg(bundle: object, *, max_signals: int = MAX_SIGNALS, max_reason: int = MAX_REASON) -> dict | None:
    """Slim buy/sell to ticket.rationale + signals + fill. Never emit {reasoning:\"\"}."""
    if not isinstance(bundle, dict):
        return None
    out: dict = {}
    ticket = bundle.get("ticket")
    if isinstance(ticket, dict):
        rationale = _trunc(ticket.get("rationale"), max_reason)
        if rationale:
            out["ticket"] = {"rationale": rationale}
    signals = _slim_signals(bundle.get("signals"), max_signals=max_signals, max_reason=max_reason)
    if signals:
        out["signals"] = signals
    fill = _slim_fill(bundle.get("fill"))
    if fill:
        out["fill"] = fill
    # Preserve rare legacy prose fields only if non-empty (never empty string stubs).
    for k in ("reasoning", "summary"):
        prose = _trunc(bundle.get(k), max_reason)
        if prose and "ticket" not in out and "signals" not in out:
            out[k] = prose
    return out or None


def _slim_holding(h: dict, *, max_signals: int, max_reason: int) -> dict:
    keep = ("ticker", "qty", "avg_cost", "last", "sector", "unrealized", "updated_at")
    out = {k: h[k] for k in keep if k in h}
    buy = slim_leg(h.get("buy"), max_signals=max_signals, max_reason=max_reason)
    if buy is not None:
        out["buy"] = buy
    sell = slim_leg(h.get("sell"), max_signals=max_signals, max_reason=max_reason)
    if sell is not None:
        out["sell"] = sell
    return out


def _slim_closed(c: dict, *, max_signals: int, max_reason: int) -> dict:
    keep = ("ticker", "qty", "entry", "exit", "pnl", "sector", "closed_at", "opened_at")
    out = {k: c[k] for k in keep if k in c}
    buy = slim_leg(c.get("buy"), max_signals=max_signals, max_reason=max_reason)
    if buy is not None:
        out["buy"] = buy
    sell = slim_leg(c.get("sell"), max_signals=max_signals, max_reason=max_reason)
    if sell is not None:
        out["sell"] = sell
    return out


def slim_payload(
    payload: dict,
    *,
    max_signals: int = MAX_SIGNALS,
    max_reason: int = MAX_REASON,
) -> dict:
    meta_in = payload.get("meta") or {}
    meta = {
        k: meta_in[k]
        for k in ("exported_at", "account", "starting_equity", "paper", "day_start_utc")
        if k in meta_in
    }
    snap_in = payload.get("snapshot") or {}
    snapshot = {
        k: snap_in[k]
        for k in (
            "equity",
            "cash",
            "peak_equity",
            "daily_pnl",
            "drawdown",
            "halted",
            "day_halted",
            "halt_reason",
        )
        if k in snap_in
    }
    holdings = [
        _slim_holding(h, max_signals=max_signals, max_reason=max_reason)
        for h in (payload.get("holdings") or [])
        if isinstance(h, dict)
    ]
    closed = [
        _slim_closed(c, max_signals=max_signals, max_reason=max_reason)
        for c in (payload.get("closed") or [])
        if isinstance(c, dict)
    ]
    return {"meta": meta, "snapshot": snapshot, "holdings": holdings, "closed": closed}


def build(
    docs_dir: Path,
    out_path: Path | None = None,
    *,
    max_signals: int = MAX_SIGNALS,
    max_reason: int = MAX_REASON,
) -> Path:
    docs = docs_dir.resolve()
    latest = json.loads((docs / "data" / "latest.json").read_text())
    assert "holdings" in latest and "closed" in latest

    # Shrink if needed to stay under target.
    signals, reason = max_signals, max_reason
    html = ""
    for _ in range(6):
        slim = slim_payload(latest, max_signals=signals, max_reason=reason)
        data_json = json.dumps(slim, separators=(",", ":"), default=str, ensure_ascii=False)
        # Guard: never ship empty reasoning stubs
        if '"reasoning":""' in data_json:
            raise SystemExit("slim payload still contains empty reasoning stubs")
        html = HTML_TEMPLATE.replace("__DATA__", data_json)
        if len(html.encode("utf-8")) <= TARGET_HTML_BYTES:
            break
        # Tighten
        if reason > 120:
            reason = max(120, reason - 40)
        elif signals > 3:
            signals -= 1
        else:
            reason = max(80, reason - 20)

    out = out_path or (docs / "index.production.html")
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size
    # Sanity checks for deploy verification
    for needle in ("APHELION", "weighted_score", "Why bought", "No reasoning recorded", "IONQ"):
        if needle not in html and needle != "weighted_score":
            # weighted_score may be absent if data lacks it; others required
            if needle != "weighted_score":
                raise SystemExit(f"production build missing {needle}")
    if "JSON.stringify(t)" in html or "JSON.stringify(bundle)" in html:
        raise SystemExit("production build must not use JSON.stringify fallback")
    if '"reasoning":""' in html:
        raise SystemExit("production HTML still embeds empty reasoning stubs")
    print(
        json.dumps(
            {
                "production_index": str(out.resolve()),
                "bytes": size,
                "max_signals": signals,
                "max_reason": reason,
            }
        )
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--docs",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs",
        help="docs directory (default: repo docs/)",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output path (default: docs/index.production.html)",
    )
    ap.add_argument("--max-signals", type=int, default=MAX_SIGNALS)
    ap.add_argument("--max-reason", type=int, default=MAX_REASON)
    args = ap.parse_args()
    build(args.docs, args.output, max_signals=args.max_signals, max_reason=args.max_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
