"""
Investments API handlers for Hermes gateway (:8642).

Endpoints (all under /api/investments/*):
  GET    /api/investments/holdings                — list all holdings (with live quotes)
  POST   /api/investments/holdings                — create a holding
  PUT    /api/investments/holdings/{id}           — update a holding
  DELETE /api/investments/holdings/{id}           — delete a holding

  GET    /api/investments/watchlist               — list watchlist (with live quotes)
  POST   /api/investments/watchlist               — add to watchlist
  DELETE /api/investments/watchlist/{id}          — remove from watchlist

  GET    /api/investments/accounts                — list accounts
  POST   /api/investments/accounts                — create account
  PUT    /api/investments/accounts/{id}           — update account
  DELETE /api/investments/accounts/{id}           — delete account

  POST   /api/investments/quotes                  — fetch live quotes for symbols
  GET    /api/investments/portfolio-aggregation   — portfolio P&L + allocation
  GET    /api/investments/networth-summary        — total net worth

Storage: ~/.amo-dev/investments/ (local JSON, never committed)
Quotes:  yfinance (stocks/ETFs, no key) + CoinGecko (crypto, no key)

GUARDRAIL: Educational/research only — NO trade execution, NO financial advice.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Storage helpers ────────────────────────────────────────────────────────────

_DATA_DIR: Optional[Path] = None


def _data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        base = Path(os.environ.get("AMO_DATA_DIR", Path.home() / ".amo-dev"))
        _DATA_DIR = base / "investments"
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Ensure subdirs exist
        (_DATA_DIR / "holdings").mkdir(exist_ok=True)
        (_DATA_DIR / "watchlist").mkdir(exist_ok=True)
        (_DATA_DIR / "accounts").mkdir(exist_ok=True)
    return _DATA_DIR


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return {}


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _load_collection(subdir: str) -> List[Dict]:
    """Load all JSON files from a subdirectory as a list."""
    d = _data_dir() / subdir
    items = []
    for f in sorted(d.glob("*.json")):
        obj = _load_json(f)
        if obj:
            items.append(obj)
    return items


def _save_item(subdir: str, item_id: str, data: Dict) -> None:
    path = _data_dir() / subdir / f"{item_id}.json"
    _save_json(path, data)


def _delete_item(subdir: str, item_id: str) -> bool:
    path = _data_dir() / subdir / f"{item_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _get_item(subdir: str, item_id: str) -> Optional[Dict]:
    path = _data_dir() / subdir / f"{item_id}.json"
    obj = _load_json(path)
    return obj if obj else None


# ── Quote fetching ─────────────────────────────────────────────────────────────

_CRYPTO_IDS: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "BNB": "binancecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "ATOM": "cosmos",
    "NEAR": "near",
    "APT": "aptos",
    "SUI": "sui",
    "PEPE": "pepe",
    "SHIB": "shiba-inu",
}

# Simple in-memory quote cache (TTL = 60 seconds)
_quote_cache: Dict[str, Dict] = {}
_CACHE_TTL = 60


def _is_crypto(symbol: str, asset_type: str) -> bool:
    return asset_type == "crypto" or symbol.upper() in _CRYPTO_IDS


async def _fetch_stock_quote(symbol: str) -> Optional[Dict]:
    """Fetch stock/ETF quote via yfinance."""
    cached = _quote_cache.get(f"stock:{symbol}")
    if cached and time.time() - cached["_ts"] < _CACHE_TTL:
        return cached

    try:
        import yfinance as yf  # type: ignore
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = float(info.last_price or 0)
        prev_close = float(info.previous_close or price)
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0
        name = symbol
        try:
            full_info = ticker.info
            name = full_info.get("longName") or full_info.get("shortName") or symbol
        except Exception:
            pass

        result = {
            "symbol": symbol.upper(),
            "name": name,
            "current_price": price,
            "price_change_24h": round(change, 4),
            "price_change_percent_24h": round(change_pct, 4),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "_ts": time.time(),
        }
        _quote_cache[f"stock:{symbol}"] = result
        return result
    except Exception as exc:
        logger.warning("yfinance quote failed for %s: %s", symbol, exc)
        return None


async def _fetch_crypto_quote(symbol: str) -> Optional[Dict]:
    """Fetch crypto quote via CoinGecko (no API key)."""
    cached = _quote_cache.get(f"crypto:{symbol}")
    if cached and time.time() - cached["_ts"] < _CACHE_TTL:
        return cached

    coingecko_id = _CRYPTO_IDS.get(symbol.upper(), symbol.lower())
    try:
        import httpx
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coingecko_id}&vs_currencies=usd"
            f"&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
        )
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None
        data = resp.json().get(coingecko_id, {})
        price = float(data.get("usd", 0))
        change_pct = float(data.get("usd_24h_change", 0))
        change = price * change_pct / 100

        # Get name from known mapping
        name_map = {v: k for k, v in _CRYPTO_IDS.items()}
        name = name_map.get(coingecko_id, symbol.upper())

        result = {
            "symbol": symbol.upper(),
            "name": name,
            "current_price": price,
            "price_change_24h": round(change, 6),
            "price_change_percent_24h": round(change_pct, 4),
            "market_cap": float(data.get("usd_market_cap", 0)),
            "volume_24h": float(data.get("usd_24h_vol", 0)),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "_ts": time.time(),
        }
        _quote_cache[f"crypto:{symbol}"] = result
        return result
    except Exception as exc:
        logger.warning("CoinGecko quote failed for %s: %s", symbol, exc)
        return None


async def _get_quote(symbol: str, asset_type: str) -> Optional[Dict]:
    """Route quote fetch to yfinance or CoinGecko based on asset type."""
    if _is_crypto(symbol, asset_type):
        return await _fetch_crypto_quote(symbol)
    return await _fetch_stock_quote(symbol)


def _enrich_holding(holding: Dict, quote: Optional[Dict]) -> Dict:
    """Attach live price + P&L to a stored holding dict."""
    h = dict(holding)
    qty = float(h.get("quantity", 0))
    cost_per = float(h.get("cost_basis_per_unit", 0))
    cost_total = qty * cost_per
    h["cost_basis_total"] = round(cost_total, 4)

    if quote and quote.get("current_price"):
        price = float(quote["current_price"])
        value = qty * price
        gain = value - cost_total
        gain_pct = (gain / cost_total * 100) if cost_total else 0.0
        h["current_price"] = round(price, 6)
        h["current_value"] = round(value, 4)
        h["gain_loss"] = round(gain, 4)
        h["gain_loss_percent"] = round(gain_pct, 4)
    else:
        h["current_price"] = 0.0
        h["current_value"] = round(cost_total, 4)
        h["gain_loss"] = 0.0
        h["gain_loss_percent"] = 0.0
    return h


# ── aiohttp helpers ────────────────────────────────────────────────────────────

def _json_ok(data: Any, status: int = 200):
    try:
        from aiohttp import web  # type: ignore
        return web.json_response(data, status=status)
    except ImportError:
        raise RuntimeError("aiohttp not available")


def _json_error(message: str, status: int = 400):
    try:
        from aiohttp import web  # type: ignore
        return web.json_response({"error": message}, status=status)
    except ImportError:
        raise RuntimeError("aiohttp not available")


# ── Holdings handlers ──────────────────────────────────────────────────────────

async def handle_holdings_list(request: Any) -> Any:
    """GET /api/investments/holdings — list all holdings with live quotes."""
    holdings = _load_collection("holdings")
    enriched = []
    for h in holdings:
        quote = await _get_quote(h["symbol"], h.get("assetType", "stock"))
        enriched.append(_enrich_holding(h, quote))
    return _json_ok(enriched)


async def handle_holdings_create(request: Any) -> Any:
    """POST /api/investments/holdings — create a new holding."""
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return _json_error("symbol is required")
    if not body.get("account_id"):
        return _json_error("account_id is required")

    qty = float(body.get("quantity", 0))
    cost_per = float(body.get("cost_basis_per_unit", 0))
    asset_type = body.get("assetType", "stock")

    # Fetch live quote to fill name if not provided
    quote = await _get_quote(symbol, asset_type)
    name = body.get("name") or (quote and quote.get("name")) or symbol

    item_id = str(uuid.uuid4())
    holding = {
        "id": item_id,
        "symbol": symbol,
        "name": name,
        "assetType": asset_type,
        "quantity": qty,
        "cost_basis_per_unit": cost_per,
        "account_id": body.get("account_id", ""),
        "date_added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_item("holdings", item_id, holding)
    return _json_ok(_enrich_holding(holding, quote), status=201)


async def handle_holdings_update(request: Any) -> Any:
    """PUT /api/investments/holdings/{id} — update quantity/cost_basis."""
    item_id = request.match_info.get("id", "")
    existing = _get_item("holdings", item_id)
    if existing is None:
        return _json_error("Holding not found", status=404)

    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    for field in ("quantity", "cost_basis_per_unit", "account_id", "name"):
        if field in body:
            existing[field] = body[field]

    _save_item("holdings", item_id, existing)
    quote = await _get_quote(existing["symbol"], existing.get("assetType", "stock"))
    return _json_ok(_enrich_holding(existing, quote))


async def handle_holdings_delete(request: Any) -> Any:
    """DELETE /api/investments/holdings/{id}."""
    item_id = request.match_info.get("id", "")
    if not _delete_item("holdings", item_id):
        return _json_error("Holding not found", status=404)
    return _json_ok({"deleted": item_id})


# ── Watchlist handlers ─────────────────────────────────────────────────────────

async def handle_watchlist_list(request: Any) -> Any:
    """GET /api/investments/watchlist — list watchlist with live quotes."""
    items = _load_collection("watchlist")
    result = []
    for item in items:
        quote = await _get_quote(item["symbol"], item.get("assetType", "stock"))
        entry = dict(item)
        if quote:
            entry["current_price"] = quote.get("current_price", 0)
            entry["price_change_24h"] = quote.get("price_change_24h", 0)
            entry["price_change_percent_24h"] = quote.get("price_change_percent_24h", 0)
        else:
            entry.setdefault("current_price", 0)
            entry.setdefault("price_change_24h", 0)
            entry.setdefault("price_change_percent_24h", 0)
        result.append(entry)
    return _json_ok(result)


async def handle_watchlist_create(request: Any) -> Any:
    """POST /api/investments/watchlist — add symbol to watchlist."""
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return _json_error("symbol is required")

    asset_type = body.get("assetType", "stock")
    quote = await _get_quote(symbol, asset_type)
    name = body.get("name") or (quote and quote.get("name")) or symbol

    item_id = str(uuid.uuid4())
    item = {
        "id": item_id,
        "symbol": symbol,
        "name": name,
        "assetType": asset_type,
        "current_price": (quote or {}).get("current_price", 0),
        "price_change_24h": (quote or {}).get("price_change_24h", 0),
        "price_change_percent_24h": (quote or {}).get("price_change_percent_24h", 0),
        "date_added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_item("watchlist", item_id, item)
    return _json_ok(item, status=201)


async def handle_watchlist_delete(request: Any) -> Any:
    """DELETE /api/investments/watchlist/{id}."""
    item_id = request.match_info.get("id", "")
    if not _delete_item("watchlist", item_id):
        return _json_error("Watchlist entry not found", status=404)
    return _json_ok({"deleted": item_id})


# ── Accounts handlers ──────────────────────────────────────────────────────────

async def handle_accounts_list(request: Any) -> Any:
    """GET /api/investments/accounts."""
    return _json_ok(_load_collection("accounts"))


async def handle_accounts_create(request: Any) -> Any:
    """POST /api/investments/accounts."""
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    name = (body.get("name") or "").strip()
    if not name:
        return _json_error("name is required")

    item_id = str(uuid.uuid4())
    account = {
        "id": item_id,
        "name": name,
        "account_type": body.get("account_type", "brokerage"),
        "currency": body.get("currency", "USD"),
        "balance": float(body.get("balance", 0)),
        "date_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_item("accounts", item_id, account)
    return _json_ok(account, status=201)


async def handle_accounts_update(request: Any) -> Any:
    """PUT /api/investments/accounts/{id}."""
    item_id = request.match_info.get("id", "")
    existing = _get_item("accounts", item_id)
    if existing is None:
        return _json_error("Account not found", status=404)

    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    for field in ("name", "balance", "currency", "account_type"):
        if field in body:
            existing[field] = body[field]

    _save_item("accounts", item_id, existing)
    return _json_ok(existing)


async def handle_accounts_delete(request: Any) -> Any:
    """DELETE /api/investments/accounts/{id}."""
    item_id = request.match_info.get("id", "")
    if not _delete_item("accounts", item_id):
        return _json_error("Account not found", status=404)
    return _json_ok({"deleted": item_id})


# ── Quotes handler ─────────────────────────────────────────────────────────────

async def handle_quotes(request: Any) -> Any:
    """POST /api/investments/quotes — batch quote fetch.

    Body: { "symbols": [ { "symbol": "AAPL", "assetType": "stock" }, ... ] }
    """
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON body")

    symbols = body.get("symbols", [])
    if not isinstance(symbols, list):
        return _json_error("symbols must be a list")

    quotes = []
    for entry in symbols:
        sym = (entry.get("symbol") or "").upper()
        atype = entry.get("assetType", "stock")
        q = await _get_quote(sym, atype)
        if q:
            # Strip internal cache timestamp
            clean = {k: v for k, v in q.items() if not k.startswith("_")}
            quotes.append(clean)

    return _json_ok({
        "quotes": quotes,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ── Portfolio aggregation ──────────────────────────────────────────────────────

async def handle_portfolio_aggregation(request: Any) -> Any:
    """GET /api/investments/portfolio-aggregation — P&L + allocation %."""
    holdings = _load_collection("holdings")

    total_value = 0.0
    total_cost = 0.0
    allocation_raw: Dict[str, float] = {}

    for h in holdings:
        quote = await _get_quote(h["symbol"], h.get("assetType", "stock"))
        enriched = _enrich_holding(h, quote)
        val = enriched["current_value"]
        cost = enriched["cost_basis_total"]
        total_value += val
        total_cost += cost
        atype = h.get("assetType", "stock")
        allocation_raw[atype] = allocation_raw.get(atype, 0) + val

    total_gain_loss = total_value - total_cost
    gain_pct = (total_gain_loss / total_cost * 100) if total_cost else 0.0

    # Allocation as percentage
    allocation: Dict[str, float] = {}
    if total_value > 0:
        for atype, val in allocation_raw.items():
            allocation[atype] = round(val / total_value * 100, 2)

    return _json_ok({
        "total_value": round(total_value, 2),
        "total_cost_basis": round(total_cost, 2),
        "total_gain_loss": round(total_gain_loss, 2),
        "total_gain_loss_percent": round(gain_pct, 2),
        "holdings_count": len(holdings),
        "allocation": allocation,
    })


# ── Net worth summary ──────────────────────────────────────────────────────────

async def handle_networth_summary(request: Any) -> Any:
    """GET /api/investments/networth-summary."""
    holdings = _load_collection("holdings")
    accounts = _load_collection("accounts")

    portfolio_value = 0.0
    for h in holdings:
        quote = await _get_quote(h["symbol"], h.get("assetType", "stock"))
        enriched = _enrich_holding(h, quote)
        portfolio_value += enriched["current_value"]

    # Cash reserves = sum of cash-type accounts
    cash_reserves = sum(
        float(a.get("balance", 0))
        for a in accounts
        if a.get("account_type") in ("cash",)
    )
    # Also add non-cash account balances (brokerage cash, etc.)
    other_balances = sum(
        float(a.get("balance", 0))
        for a in accounts
        if a.get("account_type") not in ("cash",)
    )

    total_net_worth = portfolio_value + cash_reserves + other_balances

    return _json_ok({
        "total_net_worth": round(total_net_worth, 2),
        "portfolio_value": round(portfolio_value, 2),
        "cash_reserves": round(cash_reserves + other_balances, 2),
        "currency": "USD",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ── Route registration helper ──────────────────────────────────────────────────

def register_investments_routes(app: Any) -> None:
    """Register all /api/investments/* routes on the aiohttp app."""
    r = app.router
    r.add_get("/api/investments/holdings", handle_holdings_list)
    r.add_post("/api/investments/holdings", handle_holdings_create)
    r.add_put("/api/investments/holdings/{id}", handle_holdings_update)
    r.add_delete("/api/investments/holdings/{id}", handle_holdings_delete)

    r.add_get("/api/investments/watchlist", handle_watchlist_list)
    r.add_post("/api/investments/watchlist", handle_watchlist_create)
    r.add_delete("/api/investments/watchlist/{id}", handle_watchlist_delete)

    r.add_get("/api/investments/accounts", handle_accounts_list)
    r.add_post("/api/investments/accounts", handle_accounts_create)
    r.add_put("/api/investments/accounts/{id}", handle_accounts_update)
    r.add_delete("/api/investments/accounts/{id}", handle_accounts_delete)

    r.add_post("/api/investments/quotes", handle_quotes)
    r.add_get("/api/investments/portfolio-aggregation", handle_portfolio_aggregation)
    r.add_get("/api/investments/networth-summary", handle_networth_summary)
    logger.info("Investments API routes registered (/api/investments/*)")
