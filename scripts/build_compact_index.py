#!/usr/bin/env python3
"""Build a single-file docs/index.compact.html for Vercel size-limited redeploys.

Inlines CSS + JS + a slimmed latest.json (INLINE_DATA) and a compressed observatory hero.
Keeps closed/holdings reasoning fields. Does not invent data.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from io import BytesIO
from pathlib import Path


def _slim_payload(payload: dict, *, mission_limit: int = 15, watch_limit: int = 8) -> dict:
    slim = dict(payload)
    if isinstance(slim.get("mission_log"), list):
        slim["mission_log"] = slim["mission_log"][:mission_limit]
    if isinstance(slim.get("watchlist"), list):
        slim["watchlist"] = slim["watchlist"][:watch_limit]
    # Cap per-agent today signals for size; keep closed/holdings reasoning intact.
    agents = slim.get("agents_today") or {}
    if isinstance(agents, dict):
        capped = {}
        for aid, block in agents.items():
            if not isinstance(block, dict):
                capped[aid] = block
                continue
            b = dict(block)
            sigs = b.get("signals") or []
            if isinstance(sigs, list) and len(sigs) > 12:
                b["signals"] = sigs[:12]
            capped[aid] = b
        slim["agents_today"] = capped
    return slim


def _observatory_data_uri(art_path: Path, max_side: int = 480, quality: int = 35) -> str:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Pillow required: pip install pillow") from exc
    im = Image.open(art_path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def build(docs_dir: Path, out_path: Path | None = None) -> Path:
    docs = docs_dir.resolve()
    html = (docs / "index.html").read_text()
    css = (docs / "styles.css").read_text()
    js = (docs / "app.js").read_text()
    latest = json.loads((docs / "data" / "latest.json").read_text())
    assert "closed" in latest and "holdings" in latest
    assert "agents_today" in latest and "agents_history" in latest

    slim = _slim_payload(latest)
    slim_json = json.dumps(slim, separators=(",", ":"), default=str)

    if "const HAS_INLINE" not in js:
        raise SystemExit("docs/app.js missing HAS_INLINE support — update viewer first")

    # Define INLINE_DATA before the IIFE consumes HAS_INLINE.
    js_out = "const INLINE_DATA = " + slim_json + ";\n" + js

    obs = docs / "art" / "observatory.jpg"
    html2 = html.replace('src="./art/observatory.jpg"', 'src="' + _observatory_data_uri(obs) + '"', 1)
    html2 = html2.replace(
        '<link rel="stylesheet" href="./styles.css" />',
        "<style>\n" + css + "\n</style>",
        1,
    )
    html2 = html2.replace(
        '<script src="./app.js" defer></script>',
        "<script>\n" + js_out + "\n</script>",
        1,
    )
    for needle in ("APHELION", "INLINE_DATA", "closedCards", "openPositionDetail", "agents_history"):
        if needle not in html2:
            raise SystemExit(f"compact build missing {needle}")

    out = out_path or (docs / "index.compact.html")
    out.write_text(html2)
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
        help="output path (default: docs/index.compact.html)",
    )
    args = ap.parse_args()
    out = build(args.docs, args.output)
    print(json.dumps({"compact_index": str(out.resolve()), "bytes": out.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
