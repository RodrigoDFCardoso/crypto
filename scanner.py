
import time
import sqlite3
import requests
import pandas as pd
from datetime import datetime, timezone

# ==========================================
# CONFIGURAÇÕES
# ==========================================

BASE_URL = "https://data-api.binance.vision"

MIN_VOLUME = 1_000_000       # USDT / 24h
MAX_SPREAD = 0.50            # percentual
KLINE_LIMIT = 250
REQUEST_PAUSE = 0.10

TIMEFRAMES = {
    "day": ("15m", "1h"),
    "swing": ("4h", "1d")
}

DB = "crypto_market.db"

session = requests.Session()
session.headers.update({"User-Agent": "CryptoQuantMonitor/0.1"})


# ==========================================
# BINANCE API
# ==========================================

def api_get(endpoint, params=None):

    url = BASE_URL + endpoint

    for attempt in range(5):

        response = session.get(
            url,
            params=params,
            timeout=20
        )

        if response.status_code in (418, 429):
            wait = int(response.headers.get(
                "Retry-After", 2 ** attempt
            ))
            print(f"Limite API. Aguardando {wait}s")
            time.sleep(min(wait, 120))
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("Limite da Binance persistente")


def get_market_universe():

    info = api_get("/api/v3/exchangeInfo")
    tickers = api_get("/api/v3/ticker/24hr")
    books = api_get("/api/v3/ticker/bookTicker")

    ticker_map = {x["symbol"]: x for x in tickers}
    book_map = {x["symbol"]: x for x in books}

    assets = []

    for item in info["symbols"]:

        symbol = item["symbol"]

        if item["status"] != "TRADING":
            continue

        if item["quoteAsset"] != "USDT":
            continue

        if item.get("isSpotTradingAllowed") is False:
            continue

        ticker = ticker_map.get(symbol)
        book = book_map.get(symbol)

        if not ticker or not book:
            continue

        volume = float(ticker["quoteVolume"])

        if volume < MIN_VOLUME:
            continue

        bid = float(book["bidPrice"])
        ask = float(book["askPrice"])

        if bid <= 0 or ask <= 0:
            continue

        spread = (ask - bid) / ((ask + bid) / 2) * 100

        if spread > MAX_SPREAD:
            continue

        assets.append({
            "symbol": symbol,
            "volume": volume,
            "spread": spread,
            "price": float(ticker["lastPrice"]),
            "change24h": float(ticker["priceChangePercent"])
        })

    return sorted(
        assets,
        key=lambda x: x["volume"],
        reverse=True
    )


def get_candles(symbol, interval):

    data = api_get(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": KLINE_LIMIT
        }
    )

    columns = [
        "time", "open", "high", "low",
        "close", "volume", "close_time",
        "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote",
        "ignore"
    ]

    df = pd.DataFrame(data, columns=columns)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    df["time"] = pd.to_datetime(
        df["time"], unit="ms", utc=True
    )

    # Exclui o candle ainda em formação
    return df.iloc[:-1].copy()


# ==========================================
# INDICADORES
# ==========================================

def calculate_indicators(df):

    df = df.copy()

    close = df["close"]

    df["ema9"] = close.ewm(
        span=9, adjust=False
    ).mean()

    df["ema21"] = close.ewm(
        span=21, adjust=False
    ).mean()

    df["ema50"] = close.ewm(
        span=50, adjust=False
    ).mean()

    df["sma200"] = close.rolling(200).mean()

    delta = close.diff()

    gain = delta.clip(lower=0).ewm(
        alpha=1/14, adjust=False
    ).mean()

    loss = (-delta.clip(upper=0)).ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = gain / loss.replace(0, float("nan"))

    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(
        span=12, adjust=False
    ).mean()

    ema26 = close.ewm(
        span=26, adjust=False
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9, adjust=False
    ).mean()

    previous = close.shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous).abs(),
        (df["low"] - previous).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1/14, adjust=False
    ).mean()

    df["atr_pct"] = df["atr"] / close * 100

    df["volume_avg"] = df["volume"].rolling(20).mean()

    df["relative_volume"] = (
        df["volume"] / df["volume_avg"]
    )

    return df


# ==========================================
# ESTRATÉGIA INICIAL
# ==========================================

def analyze(df):

    if len(df) < 200:
        return None

    df = calculate_indicators(df).dropna()

    if df.empty:
        return None

    last = df.iloc[-1]

    score = 0
    reasons = []

    if last["close"] > last["ema21"] > last["ema50"]:
        score += 2
        reasons.append("Tendência positiva")

    elif last["close"] < last["ema21"] < last["ema50"]:
        score -= 2
        reasons.append("Tendência negativa")

    if last["macd"] > last["macd_signal"]:
        score += 1
        reasons.append("MACD positivo")
    else:
        score -= 1
        reasons.append("MACD negativo")

    if 50 <= last["rsi"] <= 68:
        score += 1
        reasons.append("Momentum favorável")

    elif last["rsi"] > 75:
        score -= 1
        reasons.append("Sobrecompra")

    if last["relative_volume"] >= 1.5:
        score += 1
        reasons.append("Volume elevado")

    return {
        "score": score,
        "rsi": round(float(last["rsi"]), 2),
        "atr_pct": round(float(last["atr_pct"]), 3),
        "relative_volume": round(
            float(last["relative_volume"]), 2
        ),
        "price": float(last["close"]),
        "reasons": ", ".join(reasons)
    }


def classify(day, swing):

    if day is None or swing is None:
        return "DADOS_INSUFICIENTES"

    if day["score"] >= 2 and swing["score"] >= 2:
        return "COMPRA_EM_OBSERVACAO"

    if day["score"] <= -2 and swing["score"] <= -2:
        return "EVITAR_OU_REAVALIAR"

    return "AGUARDAR"


# ==========================================
# ARMAZENAMENTO
# ==========================================

def save_results(results):

    with sqlite3.connect(DB) as con:

        pd.DataFrame(results).to_sql(
            "signals",
            con,
            if_exists="append",
            index=False
        )


# ==========================================
# EXECUÇÃO
# ==========================================

def main():

    print("\nCRYPTO QUANT MONITOR")
    print("Iniciando análise Binance Spot...\n")

    assets = get_market_universe()

    print(f"Ativos aprovados nos filtros: {len(assets)}")

    results = []

    for i, asset in enumerate(assets, 1):

        symbol = asset["symbol"]

        try:

            frames = {}

            for interval in ["15m", "1h", "4h", "1d"]:

                frames[interval] = get_candles(
                    symbol, interval
                )

                time.sleep(REQUEST_PAUSE)

            day = analyze(frames["15m"])
            day_confirmation = analyze(frames["1h"])

            swing = analyze(frames["4h"])
            swing_confirmation = analyze(frames["1d"])

            if any(x is None for x in [
                day, day_confirmation,
                swing, swing_confirmation
            ]):
                continue

            # Média dos scores do timeframe principal
            # e do respectivo timeframe de confirmação.
            day["score"] = round(
                (day["score"] + day_confirmation["score"]) / 2
            )

            swing["score"] = round(
                (swing["score"] + swing_confirmation["score"]) / 2
            )

            signal = classify(day, swing)

            results.append({
                "timestamp": datetime.now(
                    timezone.utc
                ).isoformat(),
                "symbol": symbol,
                "price": asset["price"],
                "volume_24h": asset["volume"],
                "spread": asset["spread"],
                "change_24h": asset["change24h"],
                "day_score": day["score"],
                "swing_score": swing["score"],
                "day_rsi": day["rsi"],
                "swing_rsi": swing["rsi"],
                "day_atr_pct": day["atr_pct"],
                "swing_atr_pct": swing["atr_pct"],
                "signal": signal
            })

            print(
                f"{symbol:15} | "
                f"Day: {day['score']:2} | "
                f"Swing: {swing['score']:2} | "
                f"{signal}"
            )

        except Exception as error:

            print(f"Erro em {symbol}: {error}")

    if results:
        save_results(results)

    print(f"\nAnálise concluída: {len(results)} ativos.")
    print(f"Banco de dados: {DB}")


if __name__ == "__main__":
    main()
