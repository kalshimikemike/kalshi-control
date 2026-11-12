

from datetime import datetime, timezone, timedelta
from typing import Any
import os
import secrets
import base64

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles

BASE_URL = os.getenv(
    "KALSHI_BASE_URL",
    "https://external-api.kalshi.com/trade-api/v2"
).rstrip("/")

app = FastAPI(
    title="Kalshi Phone Dashboard",
    version="0.2.0"
)


# PASSWORD PROTECTION
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


# DASHBOARD FILES
app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# MAIN DASHBOARD
@app.get("/")
async def home():
    return FileResponse("static/index.html")


# MANIFEST
@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        "static/manifest.webmanifest",
        media_type="application/manifest+json"
    )


# HEALTH CHECKS
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


# GENERAL OPEN MARKETS
@app.get("/api/markets")
async def markets(
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = None
):
    params: dict[str, Any] = {
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
            detail=f"Kalshi market-data request failed: {exc}"
        )

    output = []

    for m in payload.get("markets", []):
        output.append({
            "ticker": m.get("ticker"),
            "series_ticker": m.get("series_ticker"),
            "title": (
                m.get("title")
                or m.get("subtitle")
                or m.get("ticker")
            ),
            "subtitle": m.get("subtitle"),
            "category": m.get("category") or "Other",
            "status": m.get("status"),
            "yes_bid": as_float(m.get("yes_bid_dollars")),
            "yes_ask": as_float(m.get("yes_ask_dollars")),
            "no_bid": as_float(m.get("no_bid_dollars")),
            "no_ask": as_float(m.get("no_ask_dollars")),
            "last_price": as_float(m.get("last_price_dollars")),
            "volume": (
                as_float(m.get("volume_fp"))
                or as_float(m.get("volume"))
            ),
            "open_interest": (
                as_float(m.get("open_interest_fp"))
                or as_float(m.get("open_interest"))
            ),
            "close_time": m.get("close_time")
        })

    return {
        "markets": output,
        "cursor": payload.get("cursor"),
        "count": len(output)
    }


# DEDICATED 15-MINUTE CRYPTO SCANNER
@app.get("/api/crypto/15m")
async def crypto_15m():

    series = {
        "BTC": "KXBTC15M",
        "ETH": "KXETH15M",
        "DOGE": "KXDOGE15M",
        "ZEC": "KXZEC15M"
    }

    results = []
    errors = []

    try:
        async with httpx.AsyncClient(timeout=20) as client:

            for coin, series_ticker in series.items():

                try:
                    response = await client.get(
                        f"{BASE_URL}/markets",
                        params={
                            "status": "open",
                            "series_ticker": series_ticker,
                            "limit": 20
                        }
                    )

                    response.raise_for_status()
                    payload = response.json()

                except httpx.HTTPError as exc:
                    errors.append({
                        "coin": coin,
                        "error": str(exc)
                    })
                    continue

                for m in payload.get("markets", []):

                    ticker = m.get("ticker") or ""

                    # Only accept markets belonging to this series.
                    if not ticker.startswith(series_ticker + "-"):
                        continue

                    actual_series = m.get("series_ticker")

                    if actual_series and actual_series != series_ticker:
                        continue

                    yes_bid = as_float(m.get("yes_bid_dollars"))
                    yes_ask = as_float(m.get("yes_ask_dollars"))
                    no_bid = as_float(m.get("no_bid_dollars"))
                    no_ask = as_float(m.get("no_ask_dollars"))

                    spread = None

                    if yes_bid is not None and yes_ask is not None:
                        spread = round(yes_ask - yes_bid, 4)

                    volume = (
                        as_float(m.get("volume_fp"))
                        or as_float(m.get("volume"))
                        or 0
                    )

                    results.append({
                        "coin": coin,
                        "ticker": ticker,
                        "series_ticker": series_ticker,
                        "title": (
                            m.get("title")
                            or m.get("subtitle")
                            or ticker
                        ),
                        "yes_bid": yes_bid,
                        "yes_ask": yes_ask,
                        "no_bid": no_bid,
                        "no_ask": no_ask,
                        "yes_spread": spread,
                        "last_price": as_float(
                            m.get("last_price_dollars")
                        ),
                        "volume": volume,
                        "open_interest": (
                            as_float(m.get("open_interest_fp"))
                            or as_float(m.get("open_interest"))
                            or 0
                        ),
                        "close_time": m.get("close_time"),
                        "status": m.get("status")
                    })

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Crypto scanner request failed: {exc}"
        )

    results.sort(
        key=lambda item: item["volume"],
        reverse=True
    )

    return {
        "markets": results,
        "count": len(results),
        "coins": list(series.keys()),
        "errors": errors,
        "mode": "market-discovery-only"
    }


# MARKET ORDER BOOK
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


# MARKET CANDLESTICKS
@app.get("/api/markets/{ticker}/candlesticks")
async def candlesticks(
    ticker: str,
    series_ticker: str,
    interval: int = Query(60, alias="period_interval")
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
