"""SentimentAgent — news-headline polarity with credibility + pump heuristics."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from quant_system.market_data.interface import MarketData
from quant_system.models.signal import Direction, Signal
from quant_system.research.base import ResearchAgent

# Reputable / known finance outlets (substring match on publisher, lowercased).
REPUTABLE_SOURCES: frozenset[str] = frozenset(
    {
        "reuters",
        "bloomberg",
        "wall street journal",
        "wsj",
        "financial times",
        "ft.com",
        "associated press",
        "ap news",
        "cnbc",
        "marketwatch",
        "barron",
        "yahoo finance",
        "yahoo",
        "the street",
        "thestreet",
        "motley fool",
        "seeking alpha",
        "investopedia",
        "forbes",
        "business insider",
        "nyt",
        "new york times",
        "washington post",
        "bbc",
        "guardian",
        "s&p",
        "dow jones",
        "benzinga",
        "zacks",
        "morningstar",
        "simply wall",
        "marketbeat",
        "investor's business daily",
        "ibd",
        "nasdaq",
        "sec.gov",
        "globe newswire",
        "pr newswire",
        "business wire",
    }
)

# Named but lower-tier finance blogs / aggregators (better than unknown, below wire services).
MID_TIER_SOURCES: frozenset[str] = frozenset(
    {
        "24/7 wall",
        "insider monkey",
        "stocktwits",
        "tipranks",
        "the fly",
        "investorplace",
        "fool.com",
    }
)

POSITIVE_WORDS: frozenset[str] = frozenset(
    {
        "surge",
        "surges",
        "soar",
        "soars",
        "rally",
        "rallies",
        "gain",
        "gains",
        "beat",
        "beats",
        "upgrade",
        "upgrades",
        "outperform",
        "bullish",
        "record",
        "growth",
        "profit",
        "profits",
        "strong",
        "breakthrough",
        "partnership",
        "win",
        "wins",
        "optimistic",
        "raise",
        "raises",
        "buy",
        "opportunity",
        "advantage",
        "expand",
        "expansion",
        "innovation",
        "success",
        "higher",
        "upside",
    }
)

NEGATIVE_WORDS: frozenset[str] = frozenset(
    {
        "plunge",
        "plunges",
        "crash",
        "crashes",
        "fall",
        "falls",
        "miss",
        "misses",
        "downgrade",
        "downgrades",
        "bearish",
        "loss",
        "losses",
        "weak",
        "lawsuit",
        "fraud",
        "probe",
        "investigation",
        "cut",
        "cuts",
        "warning",
        "risk",
        "risks",
        "decline",
        "declines",
        "layoff",
        "layoffs",
        "bankruptcy",
        "default",
        "concern",
        "concerns",
        "selloff",
        "sell-off",
        "downside",
        "slump",
        "slumps",
        "tumble",
        "tumbles",
    }
)

# Hyperbolic / pump-style language (used for manipulation heuristics).
HYPE_WORDS: frozenset[str] = frozenset(
    {
        "to the moon",
        "mooning",
        "100x",
        "1000x",
        "guaranteed",
        "skyrocket",
        "skyrockets",
        "massive gains",
        "cant miss",
        "can't miss",
        "life changing",
        "life-changing",
        "multi-bagger",
        "multibagger",
        "parabolic",
        "free money",
        "act now",
        "must buy",
        "double overnight",
        "triple overnight",
        "next tesla",
        "next nvidia",
        "unbelievable returns",
        "explode overnight",
        "easy money",
    }
)

NewsFetcher = Callable[[str], list[dict[str, Any]]]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def source_credibility(publisher: str) -> float:
    """Map publisher name to 0-1 credibility. Unknown sources score low."""
    p = (publisher or "").strip().lower()
    if not p:
        return 0.25
    for known in REPUTABLE_SOURCES:
        if known in p:
            return 0.85
    for mid in MID_TIER_SOURCES:
        if mid in p:
            return 0.55
    # Mild credit for any named outlet; still below reputable tier.
    if len(p) >= 3:
        return 0.45
    return 0.25


def fetch_yfinance_news(ticker: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Pull recent headlines via yfinance. Returns [] on any failure (offline-tolerant)."""
    try:
        import yfinance as yf

        raw = yf.Ticker(ticker).news or []
    except Exception:  # noqa: BLE001 — network / API flakiness
        return []

    out: list[dict[str, Any]] = []
    for item in raw[:limit]:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict):
            title = str(content.get("title") or "").strip()
            summary = str(content.get("summary") or content.get("description") or "").strip()
            provider = content.get("provider") or {}
            publisher = ""
            if isinstance(provider, dict):
                publisher = str(provider.get("displayName") or "").strip()
            link = ""
            cu = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            if isinstance(cu, dict):
                link = str(cu.get("url") or "").strip()
            published = str(content.get("pubDate") or content.get("displayTime") or "").strip()
        else:
            title = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
            summary = str(item.get("summary") or "").strip() if isinstance(item, dict) else ""
            publisher = str(item.get("publisher") or "").strip() if isinstance(item, dict) else ""
            link = str(item.get("link") or "").strip() if isinstance(item, dict) else ""
            published = str(item.get("providerPublishTime") or "").strip() if isinstance(item, dict) else ""

        if not title:
            continue
        out.append(
            {
                "title": title,
                "summary": summary,
                "publisher": publisher or "unknown",
                "link": link,
                "published": published,
                "credibility": source_credibility(publisher),
            }
        )
    return out


def _polarity_score(text: str) -> float:
    """Simple lexicon polarity in [-1, 1]."""
    tokens = set(_tokenize(text))
    if not tokens:
        return 0.0
    pos = len(tokens & POSITIVE_WORDS)
    neg = len(tokens & NEGATIVE_WORDS)
    if pos == 0 and neg == 0:
        return 0.0
    return (pos - neg) / max(pos + neg, 1)


def _hype_hits(text: str) -> list[str]:
    lower = text.lower()
    hits = [h for h in HYPE_WORDS if h in lower]
    # Token-level extras only for unambiguous pump slang (avoid "rocket"/"moon" alone —
    # those appear in legitimate space / lunar coverage).
    tokens = set(_tokenize(lower))
    for w in ("guaranteed", "parabolic", "multibagger", "mooning"):
        if w in tokens and w not in hits:
            hits.append(w)
    return hits


def score_headlines(headlines: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Score a list of normalized headline dicts into direction / confidence / risk."""
    items = list(headlines)
    if not items:
        return {
            "direction": Direction.NEUTRAL,
            "confidence": 20.0,
            "credibility": 0.5,
            "manipulation_risk": False,
            "manipulation_evidence": [],
            "weighted_polarity": 0.0,
            "headline_count": 0,
            "avg_source_credibility": 0.5,
            "reasoning": (
                "No recent headlines available for this ticker; "
                "emitting neutral with no manipulation flag (insufficient data, not risk)."
            ),
            "scored_headlines": [],
        }

    scored: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_total = 0.0
    creds: list[float] = []
    all_hype: list[str] = []

    for h in items:
        title = str(h.get("title") or "")
        summary = str(h.get("summary") or "")
        text = f"{title} {summary}".strip()
        cred = float(h.get("credibility", source_credibility(str(h.get("publisher") or ""))))
        pol = _polarity_score(text)
        hype = _hype_hits(text)
        all_hype.extend(hype)
        w = max(cred, 0.05)
        weighted_sum += pol * w
        weight_total += w
        creds.append(cred)
        scored.append(
            {
                "title": title,
                "publisher": h.get("publisher") or "unknown",
                "credibility": round(cred, 3),
                "polarity": round(pol, 3),
                "hype": hype,
            }
        )

    avg_cred = sum(creds) / len(creds)
    weighted_polarity = weighted_sum / weight_total if weight_total else 0.0
    # Only count clearly unknown/spammy outlets as low-cred (named mid-tier ~0.45 is OK).
    low_cred_share = sum(1 for c in creds if c < 0.35) / len(creds)
    unique_hype = sorted(set(all_hype))
    thin_coverage = len(items) <= 2
    extreme_hype = len(unique_hype) >= 2 or (len(unique_hype) >= 1 and thin_coverage)
    coordinated_low_cred = (
        low_cred_share >= 0.6
        and len(items) >= 3
        and abs(weighted_polarity) >= 0.45
        and avg_cred < 0.4
    )

    manipulation_evidence: list[str] = []
    if extreme_hype and (low_cred_share >= 0.5 or (thin_coverage and avg_cred < 0.4)):
        manipulation_evidence.append(
            f"Hyperbolic language in thin/low-cred coverage: {', '.join(unique_hype[:6])}"
        )
    if coordinated_low_cred:
        manipulation_evidence.append(
            f"Many low-credibility sources ({low_cred_share:.0%}) with extreme polarity "
            f"({weighted_polarity:+.2f})"
        )
    if thin_coverage and abs(weighted_polarity) >= 0.8 and unique_hype and avg_cred < 0.4:
        manipulation_evidence.append(
            "Very thin coverage with extreme hyperbolic claims"
        )

    manipulation_risk = bool(manipulation_evidence)

    # Direction thresholds on credibility-weighted polarity.
    if weighted_polarity >= 0.15:
        direction = Direction.LONG
    elif weighted_polarity <= -0.15:
        direction = Direction.SHORT
    else:
        direction = Direction.NEUTRAL

    # Confidence: base on |polarity|, sample size, and average credibility.
    conf = 35.0 + abs(weighted_polarity) * 45.0
    conf += min(len(items), 8) * 2.0  # more headlines -> a bit more confidence
    conf *= 0.55 + 0.45 * avg_cred  # down-weight unknown sources
    if manipulation_risk:
        conf *= 0.6
    confidence = float(max(5.0, min(90.0, conf)))

    # Aggregate credibility for the signal (prefer reputable mix).
    credibility = float(max(0.05, min(0.95, avg_cred)))

    reasons: list[str] = [
        f"Scored {len(items)} headline(s); weighted polarity {weighted_polarity:+.2f}",
        f"avg source credibility {avg_cred:.2f}",
    ]
    top = sorted(scored, key=lambda x: abs(x["polarity"]), reverse=True)[:3]
    for s in top:
        if s["polarity"] != 0:
            reasons.append(
                f"'{s['title'][:80]}' ({s['publisher']}, pol={s['polarity']:+.2f})"
            )
    if manipulation_risk:
        reasons.append("manipulation_risk flagged: " + "; ".join(manipulation_evidence))
    else:
        reasons.append("no pump/manipulation heuristics triggered")

    return {
        "direction": direction,
        "confidence": confidence,
        "credibility": credibility,
        "manipulation_risk": manipulation_risk,
        "manipulation_evidence": manipulation_evidence,
        "weighted_polarity": weighted_polarity,
        "headline_count": len(items),
        "avg_source_credibility": avg_cred,
        "low_cred_share": low_cred_share,
        "hype_terms": unique_hype,
        "reasoning": "; ".join(reasons),
        "scored_headlines": scored,
    }


class SentimentAgent(ResearchAgent):
    """News-based sentiment with credibility weighting and pump heuristics.

    Always includes credibility and manipulation_risk in supporting_data.
    manipulation_risk defaults False when data is thin/normal/absent.
    """

    agent_id = "sentiment"

    def __init__(
        self,
        market_data: MarketData,
        news_fetcher: NewsFetcher | None = None,
    ):
        super().__init__(market_data)
        self._news_fetcher = news_fetcher or fetch_yfinance_news

    def analyze(self, ticker: str, *, benchmark: str = "SPY") -> Signal:
        try:
            headlines = list(self._news_fetcher(ticker) or [])
            fetch_error = None
        except Exception as exc:  # noqa: BLE001
            headlines = []
            fetch_error = str(exc)

        result = score_headlines(headlines)

        supporting: dict[str, Any] = {
            "credibility": result["credibility"],
            "manipulation_risk": result["manipulation_risk"],
            "manipulation_evidence": result["manipulation_evidence"],
            "headlines": [
                {
                    "title": h.get("title"),
                    "publisher": h.get("publisher"),
                    "credibility": h.get("credibility"),
                    "polarity": h.get("polarity"),
                }
                for h in result["scored_headlines"][:10]
            ],
            "headline_count": result["headline_count"],
            "weighted_polarity": result["weighted_polarity"],
            "avg_source_credibility": result["avg_source_credibility"],
            "hype_terms": result.get("hype_terms", []),
            "benchmark": benchmark,
            "fetch_error": fetch_error,
            "stub": False,
        }

        return Signal(
            agent_id=self.agent_id,
            ticker=ticker,
            direction=result["direction"],
            confidence=float(result["confidence"]),
            reasoning=result["reasoning"],
            supporting_data=supporting,
        )
