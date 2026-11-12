


from datetime import datetime, timezone, timedelta
from typing import Any
import os
import secrets
import base64
import asyncio
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_URL = os.getenv(
    "KALSHI_BASE_URL",
    "https://external-api.kalshi.com/trade-api/v2"
).rstrip("/")

COINBASE_URL = "https://api.exchange.coinbase.com"

SERIES = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "DOGE": "KXDOGE15M",
    "ZEC": "KXZEC15M"
}

COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "DOGE": "DOGE-USD",
    "ZEC": "ZEC-USD"
}

app = FastAPI(
    title="Kalshi Control",
    version="0.3.0"
)

# Short cache to reduce API requests.
_cache = {}
_cache_lock = asyncio.Lock()


# --------------------------------------------------
# PASSWORD PROTECTION
# --------------------------------------------------

@app.middleware("http")
async def require_dashboard_password(request, call_next):

    if (
        request.url.path in ("/health", "/api/health")
        or request.url.path.startswith("/static/")
    ):
        return await call_next(request)

    expected = os.getenv("APP_PASSWORD")

    if not expected:
        return Response(
            "Server setup incomplete: APP_PASSWORD is not configured.",
            status_code=503
        )

    auth = request.headers.get("authorization", "")

    try:
        scheme, credentials = auth.split(" ", 1)
        decoded = base64.b64decode(credentials).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        scheme, username, password = "", "", ""

    if (
        scheme.lower() != "basic"
        or username != "kalshi"
        or not secrets.compare_digest(password, expected)
    ):
        return Response(
            "Authentication required",
            status_code=401,
            headers={
                "WWW-Authenticate": 'Basic realm="Kalshi Control"'
            }
        )

    return await call_next(request)


# --------------------------------------------------
# DASHBOARD FILES
# --------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)


@app.get("/")
async def home():
    return FileResponse("static/index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        "static/manifest.webmanifest",
        media_type="application/manifest+json"
    )


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return None


def safe_number(value, default=0):
    result = as_float(value)
    return result if result is not None else default


def get_cache(key):
    item = _cache.get(key)

    if not item:
        return None

    value, expires = item

    if time.monotonic() > expires:
        _cache.pop(key, None)
        return None

    return value


def set_cache(key, value, seconds=20):
    _cache[key] = (
        value,
        time.monotonic() + seconds
    )


# --------------------------------------------------
# HEALTH CHECKS
# --------------------------------------------------

@app.get("/health")
async def render_health():
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "mode": "paper-only",
        "utc": datetime.now(timezone.utc).isoformat()
    }


# --------------------------------------------------
# GENERAL OPEN MARKETS
# --------------------------------------------------

@app.get("/api/markets")
async def markets(
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = None
):
    params = {
        "status": "open",
        "limit": limit
    }

    if cursor:
        params["cursor"] = cursor

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{BASE_URL}/markets",
                params=params
            )
            response.raise_for_status()
            payload = response.json()

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Kalshi market request failed: {exc}"
        )

    output = []

    for market in payload.get("markets", []):

        output.append({
            "ticker": market.get("ticker"),
            "series_ticker": market.get("series_ticker"),
            "title": (
                market.get("title")
                or market.get("subtitle")
                or market.get("ticker")
            ),
            "subtitle": market.get("subtitle"),
            "category": market.get("category") or "Other",
            "status": market.get("status"),
            "yes_bid": as_float(market.get("yes_bid_dollars")),
            "yes_ask": as_float(market.get("yes_ask_dollars")),
            "no_bid": as_float(market.get("no_bid_dollars")),
            "no_ask": as_float(market.get("no_ask_dollars")),
            "last_price": as_float(market.get("last_price_dollars")),
            "volume": (
                as_float(market.get("volume_fp"))
                or as_float(market.get("volume"))
            ),
            "open_interest": (
                as_float(market.get("open_interest_fp"))
                or as_float(market.get("open_interest"))
            ),
            "close_time": market.get("close_time")
        })

    return {
        "markets": output,
        "cursor": payload.get("cursor"),
        "count": len(output)
    }


# --------------------------------------------------
# COINBASE SPOT PRICE MOMENTUM
# --------------------------------------------------

async def get_coinbase_candles(client, coin):

    product = COINBASE_PRODUCTS[coin]

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=15)

    cache_key = f"coinbase_{coin}"
    cached = get_cache(cache_key)

    if cached:
        return cached

    params = {
        "granularity": 60,
        "start": start.isoformat(),
        "end": now.isoformat()
    }

    response = await client.get(
        f"{COINBASE_URL}/products/{product}/candles",
        params=params,
        headers={
            "User-Agent": "KalshiControl/0.3"
        }
    )

    response.raise_for_status()

    raw = response.json()

    candles = []

    for item in raw:

        if len(item) < 6:
            continue

        candles.append({
            "time": int(item[0]),
            "low": float(item[1]),
            "high": float(item[2]),
            "open": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5])
        })

    candles.sort(key=lambda x: x["time"])

    if len(candles) < 3:
        return {
            "available": False,
            "reason": "Not enough recent price candles"
        }

    latest = candles[-1]

    # Compare latest price against earlier candles.
    short_reference = candles[-3]
    longer_reference = candles[-6] if len(candles) >= 6 else candles[0]

    short_change = (
        (latest["close"] / short_reference["close"]) - 1
    ) * 100

    longer_change = (
        (latest["close"] / longer_reference["close"]) - 1
    ) * 100

    result = {
        "available": True,
        "source": "Coinbase spot-price proxy",
        "product": product,
        "current_price": latest["close"],
        "short_change_percent": round(short_change, 5),
        "longer_change_percent": round(longer_change, 5),
        "latest_candle_time": latest["time"],
        "candle_count": len(candles)
    }

    set_cache(cache_key, result, 20)

    return result


# --------------------------------------------------
# DIRECTIONAL ANALYSIS
# --------------------------------------------------

def analyze_direction(momentum):

    if not momentum.get("available"):
        return {
            "direction": "UNKNOWN",
            "strength": "UNAVAILABLE",
            "reason": momentum.get(
                "reason",
                "Underlying price data unavailable"
            )
        }

    short = momentum["short_change_percent"]
    longer = momentum["longer_change_percent"]

    # These are momentum thresholds, not probabilities.
    if short >= 0.05 and longer > 0:
        return {
            "direction": "UP",
            "strength": "POSITIVE MOMENTUM",
            "reason": (
                "Recent price movement and the broader "
                "short-term movement are both upward."
            )
        }

    if short <= -0.05 and longer < 0:
        return {
            "direction": "DOWN",
            "strength": "NEGATIVE MOMENTUM",
            "reason": (
                "Recent price movement and the broader "
                "short-term movement are both downward."
            )
        }

    return {
        "direction": "MIXED",
        "strength": "UNCERTAIN",
        "reason": (
            "Recent and broader price movement do not "
            "provide a clear matching direction."
        )
    }


# --------------------------------------------------
# OPPORTUNITY SCORING
# --------------------------------------------------

def analyze_market(market, momentum, now):

    yes_ask = market["yes_ask"]
    no_ask = market["no_ask"]

    spread = market["yes_spread"]
    volume = market["volume"]

    direction = analyze_direction(momentum)

    seconds_left = market["seconds_until_close"]

    candidates = []

    for side, price in [
        ("YES", yes_ask),
        ("NO", no_ask)
    ]:

        if price is None or price <= 0 or price >= 1:
            continue

        # Gross payout before fees if a $1 contract wins.
        payout_multiple = round(1 / price, 2)

        # Gross profit per $1 invested if it wins.
        gross_profit = round(
            (1 / price) - 1,
            2
        )

        candidates.append({
            "side": side,
            "entry_price": price,
            "entry_cents": round(price * 100, 1),
            "gross_payout_multiple": payout_multiple,
            "gross_profit_per_dollar": gross_profit
        })

    if direction["direction"] == "UP":
        suggested_side = "YES"

    elif direction["direction"] == "DOWN":
        suggested_side = "NO"

    else:
        suggested_side = None

    selected = next(
        (
            item for item in candidates
            if item["side"] == suggested_side
        ),
        None
    )

    reasons = []

    if suggested_side:
        reasons.append(
            f"Underlying momentum currently points {direction['direction']}."
        )
    else:
        reasons.append(
            "No clear directional agreement in the recent price data."
        )

    if spread is not None and spread <= 0.03:
        reasons.append("YES quote spread is relatively narrow.")
    elif spread is not None and spread > 0.08:
        reasons.append("YES quote spread is wide; execution may be costly.")

    if volume >= 10000:
        reasons.append("Market has substantial reported trading volume.")
    else:
        reasons.append("Reported trading volume is relatively limited.")

    if seconds_left <= 60:
        reasons.append("Very little time remains; settlement risk is high.")
    elif seconds_left <= 300:
        reasons.append("Five minutes or less remain.")
    elif seconds_left <= 600:
        reasons.append("Ten minutes or less remain.")

    if selected:
        if selected["entry_price"] <= 0.30:
            reasons.append(
                "Low-priced contract: potentially large gross payout, "
                "but a high risk of losing the stake."
            )

        if selected["entry_price"] >= 0.80:
            reasons.append(
                "Expensive contract: limited gross upside relative "
                "to the amount paid."
            )

    # Conservative score. It ranks data conditions;
    # it is NOT a probability of winning.
    score = 0

    if suggested_side:
        score += 30

    if (
        momentum.get("available")
        and abs(momentum.get("short_change_percent", 0)) >= 0.05
    ):
        score += 20

    if (
        momentum.get("available")
        and abs(momentum.get("longer_change_percent", 0)) >= 0.05
    ):
        score += 15

    if spread is not None and spread <= 0.03:
        score += 15
    elif spread is not None and spread <= 0.06:
        score += 8

    if volume >= 10000:
        score += 10
    elif volume >= 1000:
        score += 5

    if seconds_left > 60:
        score += 10

    # A directional hint is not an order recommendation.
    if not selected or direction["direction"] == "MIXED":
        recommendation = "NO TRADE"
    elif seconds_left <= 60:
        recommendation = "AVOID - EXPIRING"
    elif spread is None or spread > 0.08:
        recommendation = "WATCH - WIDE SPREAD"
    elif score >= 70:
        recommendation = "TOP WATCH"
    else:
        recommendation = "WATCH"

    return {
        "recommendation": recommendation,
        "ranking_score": score,
        "score_explanation": (
            "Ranking score measures data conditions, "
            "not probability of winning."
        ),
        "suggested_side": suggested_side,
        "direction": direction,
        "entry": selected,
        "other_side": next(
            (
                item for item in candidates
                if selected and item["side"] != selected["side"]
            ),
            None
        ),
        "reasons": reasons,
        "risk_warning": (
            "A low contract price does not mean it is undervalued. "
            "A winning contract pays $1 before fees; a losing "
            "contract can lose the full entry cost."
        )
    }


# --------------------------------------------------
# DEDICATED 15-MINUTE CRYPTO SCANNER
# --------------------------------------------------

@app.get("/api/crypto/15m")
async def crypto_15m():

    now = datetime.now(timezone.utc)

    results = []
    errors = []
    rejected = []

    async with httpx.AsyncClient(timeout=20) as client:

        for coin, series_ticker in SERIES.items():

            # Get current underlying price momentum.
            try:
                momentum = await get_coinbase_candles(
                    client,
                    coin
                )

            except (httpx.HTTPError, ValueError, KeyError) as exc:
                momentum = {
                    "available": False,
                    "reason": f"Spot data unavailable: {str(exc)}"
                }

                errors.append({
                    "coin": coin,
                    "source": "Coinbase",
                    "error": str(exc)
                })

            # Discover current Kalshi markets.
            try:
                response = await client.get(
                    f"{BASE_URL}/markets",
                    params={
                        "status": "open",
                        "series_ticker": series_ticker,
                        "limit": 100
                    }
                )

                response.raise_for_status()
                payload = response.json()

            except httpx.HTTPError as exc:
                errors.append({
                    "coin": coin,
                    "source": "Kalshi",
                    "error": str(exc)
                })
                continue

            for market in payload.get("markets", []):

                ticker = market.get("ticker") or ""
                status = (market.get("status") or "").lower()

                if not ticker.startswith(series_ticker + "-"):
                    continue

                actual_series = market.get("series_ticker")

                if actual_series and actual_series != series_ticker:
                    continue

                if status not in ("open", "active"):
                    rejected.append({
                        "coin": coin,
                        "ticker": ticker,
                        "reason": "Market is not active"
                    })
                    continue

                close_time = market.get("close_time")
                expiry = parse_time(close_time)

                if not expiry or expiry <= now:
                    rejected.append({
                        "coin": coin,
                        "ticker": ticker,
                        "reason": "Missing or expired closing time"
                    })
                    continue

                yes_bid = as_float(
                    market.get("yes_bid_dollars")
                )
                yes_ask = as_float(
                    market.get("yes_ask_dollars")
                )
                no_bid = as_float(
                    market.get("no_bid_dollars")
                )
                no_ask = as_float(
                    market.get("no_ask_dollars")
                )

                if any(
                    value is None or value < 0 or value > 1
                    for value in [
                        yes_bid, yes_ask, no_bid, no_ask
                    ]
                ):
                    rejected.append({
                        "coin": coin,
                        "ticker": ticker,
                        "reason": "Missing or invalid quotes"
                    })
                    continue

                if yes_bid > yes_ask or no_bid > no_ask:
                    rejected.append({
                        "coin": coin,
                        "ticker": ticker,
                        "reason": "Invalid bid/ask relationship"
                    })
                    continue

                volume = (
                    as_float(market.get("volume_fp"))
                    or as_float(market.get("volume"))
                    or 0
                )

                seconds_left = int(
                    (expiry - now).total_seconds()
                )

                item = {
                    "coin": coin,
                    "ticker": ticker,
                    "series_ticker": series_ticker,
                    "title": (
                        market.get("title")
                        or market.get("subtitle")
                        or ticker
                    ),
                    "status": status,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "yes_spread": round(
                        yes_ask - yes_bid,
                        4
                    ),
                    "no_spread": round(
                        no_ask - no_bid,
                        4
                    ),
                    "last_price": as_float(
                        market.get("last_price_dollars")
                    ),
                    "volume": volume,
                    "open_interest": (
                        as_float(market.get("open_interest_fp"))
                        or as_float(market.get("open_interest"))
                        or 0
                    ),
                    "close_time": close_time,
                    "seconds_until_close": seconds_left,
                    "minutes_until_close": round(
                        seconds_left / 60,
                        1
                    ),
                    "underlying": momentum
                }

                analysis = analyze_market(
                    item,
                    momentum,
                    now
                )

                item["analysis"] = analysis

                results.append(item)

    # Rank by data quality and directional alignment.
    results.sort(
        key=lambda item: item["analysis"]["ranking_score"],
        reverse=True
    )

    best_choices = [
        item for item in results
        if item["analysis"]["recommendation"] in (
            "TOP WATCH",
            "WATCH",
            "WATCH - WIDE SPREAD"
        )
    ][:5]

    return {
        "scanner_status": (
            "ACTIVE_MARKETS_FOUND"
            if results
            else "NO_ACTIVE_MARKETS"
        ),
        "mode": "decision-support-only",
        "live_trading_enabled": False,
        "checked_at": now.isoformat(),
        "markets": results,
        "count": len(results),
        "best_choices": best_choices,
        "errors": errors,
        "rejected_count": len(rejected),
        "rejected": rejected[:20],
        "important_notes": [
            "Ranking scores are not win probabilities.",
            "Coinbase prices are a proxy and may differ from Kalshi settlement data.",
            "No trade is a valid recommendation.",
            "Fees, slippage and order-book depth can change actual returns.",
            "This scanner does not place orders."
        ]
    }


# --------------------------------------------------
# MARKET ORDER BOOK
# --------------------------------------------------

@app.get("/api/markets/{ticker}/orderbook")
async def orderbook(ticker: str):

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{BASE_URL}/markets/{ticker}/orderbook"
            )

            response.raise_for_status()
            return response.json()

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Kalshi orderbook request failed: {exc}"
        )


# --------------------------------------------------
# MARKET CANDLESTICKS
# --------------------------------------------------

@app.get("/api/markets/{ticker}/candlesticks")
async def candlesticks(
    ticker: str,
    series_ticker: str,
    interval: int = Query(
        60,
        alias="period_interval"
    )
):

    if interval not in (1, 60, 1440):
        raise HTTPException(
            status_code=400,
            detail="period_interval must be 1, 60, or 1440"
        )

    end = datetime.now(timezone.utc)

    hours = (
        6 if interval == 1
        else 24 if interval == 60
        else 24 * 30
    )

    start = end - timedelta(hours=hours)

    params = {
        "start_ts": int(start.timestamp()),
        "end_ts": int(end.timestamp()),
        "period_interval": interval
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{BASE_URL}/series/{series_ticker}/markets/{ticker}/candlesticks",
                params=params
            )

            response.raise_for_status()
            return response.json()

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Kalshi candlestick request failed: {exc}"
        )

