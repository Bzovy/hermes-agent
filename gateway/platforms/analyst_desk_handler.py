"""
Analyst Desk handler for Hermes gateway (:8642).

Endpoint:
  POST /api/investments/analyst-desk/{ticker}

Runs a panel of 4 investor-persona agents on one ticker, then a Portfolio Manager
synthesizer that aggregates them into a consensus signal.

Agents:
  1. Buffett    — value / moat / margin-of-safety
  2. CathieWood — growth / disruption / innovation
  3. Technicals — trend / momentum from recent price history
  4. Risk       — volatility / concentration / drawdown

Output schema:
  {
    "ticker": "AAPL",
    "signal": "bullish" | "bearish" | "neutral",
    "confidence": 0.0-1.0,
    "agents": [
      { "name": str, "signal": ..., "confidence": ..., "rationale": str }
    ],
    "synthesis": { "signal": ..., "confidence": ..., "rationale": str },
    "cached": bool,
    "cache_age_seconds": int | null,
    "timestamp": str
  }

GUARDRAIL: Educational/research only — signals are ANALYSIS, NOT advice.
           NO trade execution.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Cache ──────────────────────────────────────────────────────────────────────
# Keyed by upper-case ticker. TTL = 15 minutes.
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 900  # 15 min


def _cache_get(ticker: str) -> Optional[Dict[str, Any]]:
    entry = _CACHE.get(ticker.upper())
    if entry and time.time() - entry["_ts"] < _CACHE_TTL:
        return entry
    return None


def _cache_set(ticker: str, result: Dict[str, Any]) -> None:
    _CACHE[ticker.upper()] = {**result, "_ts": time.time()}


# ── OpenRouter client ─────────────────────────────────────────────────────────

_OPENROUTER_BASE_URL = "https://openrouter.io/api/v1"
_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"  # Switched from deepseek-chat (403 errors)


def _openrouter_client():
    """Get an OpenAI-format client pointed at OpenRouter."""
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return AsyncOpenAI(
        base_url=_OPENROUTER_BASE_URL,
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://agentic-master-ui.local",
            "X-Title": "Analyst Desk",
        },
    )


async def _openrouter_chat(
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 500,
    timeout: float = 60,
) -> str:
    """Call OpenRouter, return the response text content."""
    try:
        client = _openrouter_client()
        resp = await client.chat.completions.create(
            model=_OPENROUTER_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        # OpenRouter account may be restricted (403 Forbidden). Fall back to deterministic analysis.
        logger.info("analyst_desk: OpenRouter call failed (%s), falling back to rule-based analysis", type(e).__name__)
        # Try to extract ticker context from user message for rule-based fallback
        raise  # Re-raise so caller can implement persona-specific fallback


# ── Persona definitions ────────────────────────────────────────────────────────

_PERSONAS = [
    {
        "name": "Warren Buffett",
        "id": "buffett",
        "focus": "value investing, economic moat, margin of safety, long-term business quality",
        "system": (
            "You are analyzing a stock through Warren Buffett's value investing lens. "
            "Focus on: (1) business quality and durable competitive moat, "
            "(2) management quality and capital allocation, "
            "(3) margin of safety vs intrinsic value (P/E, P/B, earnings yield), "
            "(4) long-term earnings power and predictability. "
            "Be skeptical of hype. Ignore short-term price noise. "
            "Output ONLY valid JSON: "
            '{"signal":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"rationale":"2 sentences max"}'
        ),
    },
    {
        "name": "Cathie Wood",
        "id": "cathie_wood",
        "focus": "disruptive innovation, exponential growth, long arc technology adoption",
        "system": (
            "You are analyzing a stock through Cathie Wood's disruptive innovation lens. "
            "Focus on: (1) is this company riding a multi-year platform/technology shift? "
            "(2) revenue growth trajectory and total addressable market expansion, "
            "(3) R&D intensity and IP moat in emerging categories, "
            "(4) is the current price accounting for the 5-year disruption arc? "
            "Be willing to tolerate near-term losses for long-term exponential payoff. "
            "Output ONLY valid JSON: "
            '{"signal":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"rationale":"2 sentences max"}'
        ),
    },
    {
        "name": "Technicals",
        "id": "technicals",
        "focus": "price trend, momentum, support/resistance, volume confirmation",
        "system": (
            "You are a technical analyst. Analyze the provided recent price history. "
            "Focus on: (1) trend direction (higher highs/lows vs lower highs/lows), "
            "(2) momentum (is price accelerating or decelerating?), "
            "(3) proximity to key levels (52-week high/low), "
            "(4) recent volatility vs historical norm. "
            "Do not speculate about fundamentals. "
            "Output ONLY valid JSON: "
            '{"signal":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"rationale":"2 sentences max"}'
        ),
    },
    {
        "name": "Risk",
        "id": "risk",
        "focus": "volatility, drawdown risk, concentration, macro sensitivity",
        "system": (
            "You are a risk analyst. Analyze the provided data for risk factors. "
            "Focus on: (1) price volatility (high/low range, recent swings), "
            "(2) maximum drawdown risk from current levels, "
            "(3) sector concentration or macro sensitivity (rates, USD, commodities), "
            "(4) valuation risk — does current price leave adequate margin for error? "
            "A bearish risk signal means HIGH risk (dangerous entry). "
            "A bullish risk signal means LOW risk (favorable risk/reward). "
            "Output ONLY valid JSON: "
            '{"signal":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"rationale":"2 sentences max"}'
        ),
    },
]

_PM_SYSTEM = (
    "You are a Portfolio Manager synthesizing the views of 4 analysts. "
    "You receive each analyst's signal (bullish/bearish/neutral) and rationale. "
    "Your job: weigh them, account for disagreements, and produce a single consensus signal. "
    "Weight: Buffett 30%, CathieWood 25%, Technicals 25%, Risk 20%. "
    "If Risk says high-risk (bearish), reduce overall confidence by 15%. "
    "Output ONLY valid JSON: "
    '{"signal":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"rationale":"2 sentences max. Mention key agreement or disagreement."}'
    " IMPORTANT: this is educational analysis only — NOT financial advice."
)


# ── Data helpers ───────────────────────────────────────────────────────────────


def _fetch_ticker_data(ticker: str) -> Dict[str, Any]:
    """Fetch price history + basic fundamentals via yfinance. Returns dict."""
    try:
        import yfinance as yf  # type: ignore[import]

        t = yf.Ticker(ticker)
        fast = t.fast_info

        # Recent price history: 3 months daily
        hist = t.history(period="3mo")
        prices: list = []
        if not hist.empty:
            prices = [
                {
                    "date": str(idx.date()),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                }
                for idx, row in hist.iterrows()
            ]

        # Basic fundamentals (fast_info is lightweight)
        current_price = float(fast.last_price) if fast.last_price else None
        year_high = float(fast.year_high) if fast.year_high else None
        year_low = float(fast.year_low) if fast.year_low else None
        market_cap = float(fast.market_cap) if fast.market_cap else None

        # Slightly heavier info for P/E etc — best-effort
        info: Dict[str, Any] = {}
        try:
            raw_info = t.info or {}
            for key in (
                "trailingPE",
                "forwardPE",
                "priceToBook",
                "dividendYield",
                "revenueGrowth",
                "returnOnEquity",
                "sector",
                "industry",
                "longBusinessSummary",
            ):
                if raw_info.get(key) is not None:
                    info[key] = raw_info[key]
        except Exception:  # noqa: BLE001
            pass

        return {
            "ticker": ticker.upper(),
            "current_price": current_price,
            "year_high": year_high,
            "year_low": year_low,
            "market_cap_billions": round(market_cap / 1e9, 1) if market_cap else None,
            "fundamentals": info,
            "price_history": prices,  # last 3mo daily
            "price_history_summary": _summarize_prices(prices),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyst_desk: yfinance fetch failed for %s: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}


def _summarize_prices(prices: list) -> str:
    """Build a compact text summary of recent price history for LLM context."""
    if not prices:
        return "No price history available."
    last30 = prices[-30:] if len(prices) >= 30 else prices
    closes = [p["close"] for p in last30]
    latest = closes[-1]
    oldest = closes[0]
    hi = max(p["high"] for p in last30)
    lo = min(p["low"] for p in last30)
    chg = ((latest - oldest) / oldest * 100) if oldest else 0
    trend = "up" if chg > 2 else ("down" if chg < -2 else "flat")
    return (
        f"Last 30 trading days: current ${latest:.2f}, range ${lo:.2f}–${hi:.2f}, "
        f"change {chg:+.1f}% ({trend} trend). "
        f"First close in window: ${oldest:.2f}."
    )


def _build_ticker_context(data: Dict[str, Any]) -> str:
    """Build a compact data-sheet string to inject into each agent prompt."""
    lines = [
        f"Ticker: {data['ticker']}",
        f"Current price: ${data.get('current_price', 'N/A')}",
        f"52-week range: ${data.get('year_low', '?')} – ${data.get('year_high', '?')}",
        f"Market cap: ${data.get('market_cap_billions', '?')}B",
    ]
    fund = data.get("fundamentals", {})
    if fund.get("trailingPE"):
        lines.append(f"Trailing P/E: {fund['trailingPE']:.1f}")
    if fund.get("forwardPE"):
        lines.append(f"Forward P/E: {fund['forwardPE']:.1f}")
    if fund.get("priceToBook"):
        lines.append(f"Price/Book: {fund['priceToBook']:.2f}")
    if fund.get("revenueGrowth"):
        lines.append(f"Revenue growth: {fund['revenueGrowth']*100:.1f}%")
    if fund.get("returnOnEquity"):
        lines.append(f"Return on equity: {fund['returnOnEquity']*100:.1f}%")
    if fund.get("sector"):
        lines.append(f"Sector: {fund['sector']}")
    if fund.get("industry"):
        lines.append(f"Industry: {fund['industry']}")
    summary = data.get("price_history_summary")
    if summary:
        lines.append(f"\nPrice action: {summary}")
    desc = fund.get("longBusinessSummary", "")
    if desc:
        # Truncate to 300 chars to keep prompt compact
        lines.append(f"\nBusiness: {desc[:300]}{'...' if len(desc) > 300 else ''}")
    return "\n".join(lines)


def _rule_based_analysis(data: Dict[str, Any], persona_id: str) -> Dict[str, Any]:
    """Generate rule-based analysis when LLM is unavailable. Returns signal + confidence + rationale."""
    fund = data.get("fundamentals", {})
    curr_price = data.get("current_price", 0)
    year_high = data.get("year_high", curr_price)
    year_low = data.get("year_low", curr_price)
    market_cap = data.get("market_cap_billions", 0)
    
    if persona_id == "buffett":
        # Value: check P/E, P/B, dividend yield
        pe = fund.get("trailingPE", 0)
        roe = fund.get("returnOnEquity", 0)
        dy = fund.get("dividendYield", 0)
        
        if pe and pe < 15:  # Low P/E = value
            signal, conf = "bullish", 0.7
            rationale = f"Low P/E ({pe:.1f}, <15) suggests valuation discount. Value signal."
        elif pe and pe > 30:  # High P/E = overvalued
            signal, conf = "bearish", 0.65
            rationale = f"High P/E ({pe:.1f}, >30) suggests premium valuation. Limited margin of safety."
        else:
            signal, conf = "neutral", 0.5
            rationale = "Valuation in fair range. Requires deeper business analysis."
        return {"signal": signal, "confidence": conf, "rationale": rationale}
    
    elif persona_id == "cathie_wood":
        # Growth: check revenue growth, market cap trajectory
        rev_growth = fund.get("revenueGrowth", 0)
        
        if rev_growth and rev_growth > 0.20:  # >20% growth
            signal, conf = "bullish", 0.75
            rationale = f"Strong revenue growth ({rev_growth*100:.0f}%) indicates disruption/scaling. Innovation premium warranted."
        elif rev_growth and rev_growth < 0:  # Shrinking
            signal, conf = "bearish", 0.6
            rationale = f"Declining revenue ({rev_growth*100:.0f}%) signals market disruption or declining relevance."
        else:
            signal, conf = "neutral", 0.5
            rationale = "Modest growth trajectory. Not yet disruptive, not declining."
        return {"signal": signal, "confidence": conf, "rationale": rationale}
    
    elif persona_id == "technicals":
        # Trend: price proximity to 52-week range
        if not curr_price or not year_high:
            return {"signal": "neutral", "confidence": 0.4, "rationale": "Insufficient price data for trend analysis."}
        
        pct_from_high = (curr_price / year_high - 1) * 100 if year_high else 0
        pct_from_low = (curr_price / year_low - 1) * 100 if year_low else 0
        
        if pct_from_high > -5:  # Within 5% of 52-week high
            signal, conf = "bullish", 0.7
            rationale = f"Trading near 52-week high ({pct_from_high:+.1f}%). Bullish momentum."
        elif pct_from_high < -30:  # 30%+ below high
            signal, conf = "bearish", 0.65
            rationale = f"Trading {pct_from_high:.1f}% below 52-week high. Downtrend signal."
        else:
            signal, conf = "neutral", 0.5
            rationale = f"Trading {pct_from_high:+.1f}% from high. Mid-range momentum."
        return {"signal": signal, "confidence": conf, "rationale": rationale}
    
    elif persona_id == "risk":
        # Risk: volatility proxy = (year_high - year_low) / year_low
        if year_low and year_high:
            volatility = (year_high - year_low) / year_low
            if volatility < 0.15:  # <15% range = low volatility = low risk
                signal, conf = "bullish", 0.65
                rationale = f"Low volatility ({volatility*100:.0f}% range). Favorable risk/reward."
            elif volatility > 0.40:  # >40% range = high volatility = high risk
                signal, conf = "bearish", 0.7
                rationale = f"High volatility ({volatility*100:.0f}% range). Elevated drawdown risk."
            else:
                signal, conf = "neutral", 0.5
                rationale = f"Moderate volatility ({volatility*100:.0f}%). Acceptable risk profile."
        else:
            signal, conf = "neutral", 0.4
            rationale = "Insufficient range data for risk assessment."
        return {"signal": signal, "confidence": conf, "rationale": rationale}
    
    # Default
    return {"signal": "neutral", "confidence": 0.5, "rationale": "Analysis unavailable."}


def _parse_agent_json(raw: str, persona_name: str) -> Dict[str, Any]:
    """Parse the agent's JSON response, with fallback on malformed output."""
    try:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        signal = str(parsed.get("signal", "neutral")).lower()
        if signal not in ("bullish", "bearish", "neutral"):
            signal = "neutral"
        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(parsed.get("rationale", ""))[:400]
        return {"signal": signal, "confidence": round(confidence, 2), "rationale": rationale}
    except Exception:  # noqa: BLE001
        logger.warning("analyst_desk: failed to parse JSON from %s: %r", persona_name, raw[:200])
        return {
            "signal": "neutral",
            "confidence": 0.3,
            "rationale": f"Analysis unavailable (parse error). Raw: {raw[:120]}",
        }


# ── Main handler ───────────────────────────────────────────────────────────────


async def handle_analyst_desk(request: Any) -> Any:
    """POST /api/investments/analyst-desk/{ticker}

    Runs 4 persona agents + PM synthesizer via NVIDIA DeepSeek.
    Results are cached 15 minutes per ticker.
    Educational analysis only — NOT financial advice.
    """
    try:
        from aiohttp import web  # type: ignore[import]
    except ImportError:
        return None

    ticker = request.match_info.get("ticker", "").upper().strip()
    if not ticker or not ticker.isalnum():
        return web.json_response({"error": "Invalid ticker symbol"}, status=400)

    # Cache hit
    cached = _cache_get(ticker)
    if cached:
        age = int(time.time() - cached["_ts"])
        result = {k: v for k, v in cached.items() if k != "_ts"}
        result["cached"] = True
        result["cache_age_seconds"] = age
        return web.json_response(result)

    # Fetch market data
    data = _fetch_ticker_data(ticker)
    if "error" in data and not data.get("current_price"):
        return web.json_response(
            {"error": f"Failed to fetch data for {ticker}: {data['error']}"}, status=422
        )

    context = _build_ticker_context(data)

    # Run 4 agents concurrently
    import asyncio

    async def run_agent(persona: Dict[str, Any]) -> Dict[str, Any]:
        try:
            content = await _openrouter_chat(
                messages=[
                    {"role": "system", "content": persona["system"]},
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this ticker for me:\n\n{context}\n\n"
                            "Respond ONLY with the JSON object as instructed. "
                            "Do not include any text outside the JSON braces."
                        ),
                    },
                ],
                temperature=0.3,
                max_tokens=500,
                timeout=60,
            )
            parsed = _parse_agent_json(content, persona["name"])
            return {"name": persona["name"], "id": persona["id"], **parsed}
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyst_desk: agent %s LLM failed (%s), using rule-based analysis", persona["name"], type(exc).__name__)
            # Fall back to rule-based analysis using fundamentals
            ruled = _rule_based_analysis(data, persona["id"])
            return {
                "name": persona["name"],
                "id": persona["id"],
                **ruled,
            }

    agent_results = await asyncio.gather(*[run_agent(p) for p in _PERSONAS])
    agents_list = list(agent_results)

    # Portfolio Manager synthesizer
    agents_summary = "\n".join(
        f"- {a['name']}: {a['signal']} (confidence {a['confidence']:.0%})\n  Rationale: {a['rationale']}"
        for a in agents_list
    )
    pm_user_msg = (
        f"Ticker: {ticker}\n\nAnalyst panel results:\n{agents_summary}\n\n"
        "Synthesize into a consensus signal. Output ONLY the JSON object."
    )

    synthesis: Dict[str, Any] = {"signal": "neutral", "confidence": 0.5, "rationale": "Synthesis unavailable."}
    try:
        pm_content = await _openrouter_chat(
            messages=[
                {"role": "system", "content": _PM_SYSTEM},
                {"role": "user", "content": pm_user_msg},
            ],
            temperature=0.2,
            max_tokens=400,
            timeout=60,
        )
        synthesis = _parse_agent_json(pm_content, "PortfolioManager")
    except Exception as exc:  # noqa: BLE001
        logger.info("analyst_desk: PM synthesizer LLM failed (%s), computing rule-based consensus", type(exc).__name__)
        # Rule-based synthesis: weighted average of agent signals
        bullish_count = sum(1 for a in agents_list if a["signal"] == "bullish")
        bearish_count = sum(1 for a in agents_list if a["signal"] == "bearish")
        neutral_count = len(agents_list) - bullish_count - bearish_count
        
        avg_confidence = sum(a["confidence"] for a in agents_list) / len(agents_list) if agents_list else 0.5
        
        if bullish_count > bearish_count + neutral_count // 2:
            syn_signal = "bullish"
            syn_conf = min(0.9, 0.6 + bullish_count * 0.1)
            syn_rationale = f"Consensus: {bullish_count}/{len(agents_list)} agents bullish. Avg confidence {avg_confidence:.1%}."
        elif bearish_count > bullish_count + neutral_count // 2:
            syn_signal = "bearish"
            syn_conf = min(0.9, 0.6 + bearish_count * 0.1)
            syn_rationale = f"Consensus: {bearish_count}/{len(agents_list)} agents bearish. Avg confidence {avg_confidence:.1%}."
        else:
            syn_signal = "neutral"
            syn_conf = 0.5
            syn_rationale = f"Consensus: mixed signals ({bullish_count}B/{bearish_count}Be/{neutral_count}N). No dominant bias."
        
        synthesis = {
            "signal": syn_signal,
            "confidence": round(syn_conf, 2),
            "rationale": syn_rationale,
        }

    # Derive top-level signal from synthesis
    result: Dict[str, Any] = {
        "ticker": ticker,
        "signal": synthesis["signal"],
        "confidence": synthesis["confidence"],
        "agents": agents_list,
        "synthesis": synthesis,
        "cached": False,
        "cache_age_seconds": None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": "Educational analysis only — NOT financial advice. Do your own research.",
    }

    _cache_set(ticker, result)
    return web.json_response(result)


# ── Route registration ─────────────────────────────────────────────────────────


def register_analyst_desk_routes(app: Any) -> None:
    """Register /api/investments/analyst-desk/* routes."""
    app.router.add_post("/api/investments/analyst-desk/{ticker}", handle_analyst_desk)
    logger.info("Analyst Desk routes registered (/api/investments/analyst-desk/{ticker})")