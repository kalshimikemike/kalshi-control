from datetime import datetime, timezone, timedelta
from typing import Any
import os
import secrets
from fastapi.responses import Response

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
app = FastAPI(title="Kalshi Phone Dashboard", version="0.1.1")

# HTTP Basic authentication. Set APP_PASSWORD in the hosting provider's
# secret/environment-variable settings. Never commit the password.
@app.middleware("http")
async def require_dashboard_password(request, call_next):
    if request.url.path == "/api/health":
        return await call_next(request)
    expected = os.getenv("APP_PASSWORD")
    if not expected:
        return Response("Server setup incomplete: APP_PASSWORD is not configured.", status_code=503)
    auth = request.headers.get("authorization", "")
    try:
        scheme, credentials = auth.split(" ", 1)
        import base64
        decoded = base64.b64decode(credentials).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        scheme, username, password = "", "", ""
    if scheme.lower() != "basic" or username != "kalshi" or not secrets.compare_digest(password, expected):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Kalshi Control"'},
        )
    return await call_next(request)
app.mount("/static", StaticFiles(directory="static"), name="static")


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@app.get("/")
async def home():
    return FileResponse("app/static/index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("app/static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/api/health")
async def health():
    return {"ok": True, "mode": "paper-only", "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/markets")
async def markets(limit: int = Query(100, ge=1, le=200), cursor: str | None = None):
    params: dict[str, Any] = {"status": "open", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BASE_URL}/markets", params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Kalshi market-data request failed: {exc}")

    output = []
    for m in payload.get("markets", []):
        output.append({
            "ticker": m.get("ticker"),
            "series_ticker": m.get("series_ticker"),
            "title": m.get("title") or m.get("subtitle") or m.get("ticker"),
            "subtitle": m.get("subtitle"),
            "category": m.get("category") or "Other",
            "status": m.get("status"),
            "yes_bid": as_float(m.get("yes_bid_dollars")),
            "yes_ask": as_float(m.get("yes_ask_dollars")),
            "no_bid": as_float(m.get("no_bid_dollars")),
            "no_ask": as_float(m.get("no_ask_dollars")),
            "last_price": as_float(m.get("last_price_dollars")),
            "volume": as_float(m.get("volume_fp")) or as_float(m.get("volume")),
            "open_interest": as_float(m.get("open_interest_fp")) or as_float(m.get("open_interest")),
            "close_time": m.get("close_time"),
        })
    return {"markets": output, "cursor": payload.get("cursor"), "count": len(output)}


@app.get("/api/markets/{ticker}/orderbook")
async def orderbook(ticker: str):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BASE_URL}/markets/{ticker}/orderbook")
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Kalshi orderbook request failed: {exc}")


@app.get("/api/markets/{ticker}/candlesticks")
async def candlesticks(ticker: str, series_ticker: str, interval: int = Query(60, alias="period_interval")):
    if interval not in (1, 60, 1440):
        raise HTTPException(status_code=400, detail="period_interval must be 1, 60, or 1440")
    end = datetime.now(timezone.utc)
    hours = 6 if interval == 1 else (24 if interval == 60 else 24 * 30)
    start = end - timedelta(hours=hours)
    params = {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": interval}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BASE_URL}/series/{series_ticker}/markets/{ticker}/candlesticks", params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Kalshi candlestick request failed: {exc}")
