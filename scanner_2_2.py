import time
import sqlite3
import requests
import pandas as pd

from pathlib import Path
from datetime import datetime, timezone


# ==========================================
# CONFIGURACOES
# ==========================================

BASE_URL = "https://data-api.binance.vision"

MIN_VOLUME = 1_000_000
MAX_SPREAD = 0.50

KLINE_LIMIT = 250
API_KLINE_LIMIT = 1000
REQUEST_PAUSE = 0.15

# Manter 20 durante a validacao.
# Depois, avaliar aumento gradual. None analisa todos os ativos aprovados.
MAX_ASSETS = 20

# Ativos-base que nao devem entrar na estrategia.
EXCLUDED_BASE_ASSETS = {
    "USDC", "USD1", "FDUSD", "TUSD", "USDP", "BUSD",
    "DAI", "USDE", "UST", "USTC", "PYUSD",
    "RLUSD", "RWUSD", "EUR"
}

# Ativos que nao fazem parte do universo principal desta estrategia.
# XAUT: ouro tokenizado. SOXLB: ativo tokenizado relacionado a ETF.
EXCLUDED_SYMBOLS = {
    "XAUTUSDT",
    "SOXLBUSDT"
}

INTERVALS = ["15m", "1h", "4h", "1d"]

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DB = DATA_DIR / "crypto_market.db"

DATA_DIR.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "CryptoQuantMonitor/2.2"
})


# ==========================================
# UTILITARIOS
# ==========================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def table_exists(con, table_name):
    result = con.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name=?
    """, (table_name,)).fetchone()

    return result is not None


# ==========================================
# BANCO DE DADOS
# ==========================================

def init_database():
    with sqlite3.connect(DB, timeout=30) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")

        con.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                quote_volume REAL,
                trades INTEGER,
                taker_buy_base REAL,
                taker_buy_quote REAL,
                PRIMARY KEY (symbol, timeframe, open_time)
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_lookup
            ON candles(symbol, timeframe, open_time)
        """)

        # Preserva a estrutura antiga da tabela signals.
        if not table_exists(con, "signals"):
            con.execute("""
                CREATE TABLE signals (
                    timestamp TEXT,
                    symbol TEXT,
                    price REAL,
                    volume_24h REAL,
                    spread REAL,
                    change_24h REAL,
                    day_score INTEGER,
                    swing_score INTEGER,
                    day_rsi REAL,
                    swing_rsi REAL,
                    day_atr_pct REAL,
                    swing_atr_pct REAL,
                    signal TEXT
                )
            """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
            ON signals(timestamp)
        """)

        # Auditoria das execucoes. Nao altera candles nem signals.
        con.execute("""
            CREATE TABLE IF NOT EXISTS scanner_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL,
                assets_approved INTEGER DEFAULT 0,
                assets_selected INTEGER DEFAULT 0,
                analyses_completed INTEGER DEFAULT 0,
                partial_analyses INTEGER DEFAULT 0,
                assets_failed INTEGER DEFAULT 0,
                candles_inserted INTEGER DEFAULT 0,
                signals_saved INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT
            )
        """)


def start_run():
    started_at = utc_now_iso()

    with sqlite3.connect(DB, timeout=30) as con:
        cursor = con.execute("""
            INSERT INTO scanner_runs (started_at, status)
            VALUES (?, 'RUNNING')
        """, (started_at,))

        return cursor.lastrowid, started_at


def finish_run(run_id, started_at, stats, status="COMPLETED", error=None):
    finished = utc_now()
    started = datetime.fromisoformat(started_at)
    duration = (finished - started).total_seconds()

    with sqlite3.connect(DB, timeout=30) as con:
        con.execute("""
            UPDATE scanner_runs
            SET finished_at = ?,
                duration_seconds = ?,
                assets_approved = ?,
                assets_selected = ?,
                analyses_completed = ?,
                partial_analyses = ?,
                assets_failed = ?,
                candles_inserted = ?,
                signals_saved = ?,
                status = ?,
                error = ?
            WHERE id = ?
        """, (
            finished.isoformat(),
            duration,
            stats["assets_approved"],
            stats["assets_selected"],
            stats["analyses_completed"],
            stats["partial_analyses"],
            stats["assets_failed"],
            stats["candles_inserted"],
            stats["signals_saved"],
            status,
            error,
            run_id
        ))


# ==========================================
# BINANCE API
# ==========================================

def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    for attempt in range(5):
        try:
            response = session.get(
                url,
                params=params,
                timeout=(5, 20)
            )

            if response.status_code in (418, 429):
                retry_after = response.headers.get(
                    "Retry-After", 2 ** attempt
                )

                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = 2 ** attempt

                wait = min(max(wait, 1), 120)

                print(
                    f"Limite da API ({response.status_code}). "
                    f"Aguardando {wait:.0f}s"
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = min(2 ** attempt, 30)
                print(
                    f"Erro temporario HTTP {response.status_code}. "
                    f"Nova tentativa em {wait}s"
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException as error:
            wait = min(2 ** attempt, 30)
            print(
                f"Falha na API: {error}. "
                f"Tentativa {attempt + 1}/5"
            )

            if attempt < 4:
                time.sleep(wait)

    raise RuntimeError(f"Falha persistente na API: {endpoint}")


# ==========================================
# UNIVERSO DE MERCADO
# ==========================================

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

        if item["baseAsset"] in EXCLUDED_BASE_ASSETS:
            continue

        if symbol in EXCLUDED_SYMBOLS:
            continue

        if item.get("isSpotTradingAllowed") is False:
            continue

        ticker = ticker_map.get(symbol)
        book = book_map.get(symbol)

        if not ticker or not book:
            continue

        try:
            volume = float(ticker["quoteVolume"])
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            price = float(ticker["lastPrice"])
            change_24h = float(ticker["priceChangePercent"])
        except (KeyError, TypeError, ValueError):
            continue

        if volume < MIN_VOLUME or bid <= 0 or ask <= 0:
            continue

        spread = ((ask - bid) / ((ask + bid) / 2)) * 100

        if spread > MAX_SPREAD:
            continue

        assets.append({
            "symbol": symbol,
            "volume": volume,
            "spread": spread,
            "price": price,
            "change24h": change_24h
        })

    return sorted(
        assets,
        key=lambda x: x["volume"],
        reverse=True
    )


# ==========================================
# COLETA DE CANDLES
# ==========================================

def get_candles(symbol, interval, last_open_time=None):
    """
    Primeira coleta: baixa KLINE_LIMIT candles.
    Coletas seguintes: consulta a partir do ultimo candle salvo,
    mantendo uma sobreposicao para evitar perder atualizacoes.
    Retorna somente candles fechados.
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": (
            API_KLINE_LIMIT
            if last_open_time is not None
            else KLINE_LIMIT
        )
    }

    if last_open_time is not None:
        timestamp = pd.Timestamp(last_open_time)
        params["startTime"] = int(timestamp.timestamp() * 1000)

    all_data = []

    while True:
        data = api_get("/api/v3/klines", params)

        if not data:
            break

        all_data.extend(data)

        if last_open_time is None:
            break

        if len(data) < API_KLINE_LIMIT:
            break

        last_candle = data[-1]
        now_ms = int(time.time() * 1000)

        if int(last_candle[6]) >= now_ms:
            break

        next_start = int(last_candle[0]) + 1

        if next_start <= params["startTime"]:
            break

        params["startTime"] = next_start
        time.sleep(REQUEST_PAUSE)

    columns = [
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]

    df = pd.DataFrame(all_data, columns=columns)

    if df.empty:
        return df

    numeric_columns = [
        "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote"
    ]

    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(
        df["time"], unit="ms", utc=True
    )
    df["close_time"] = pd.to_datetime(
        df["close_time"], unit="ms", utc=True
    )

    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now].copy()

    df = df.dropna(subset=[
        "time", "close_time", "open", "high", "low",
        "close", "volume"
    ])

    df = df.drop_duplicates(subset=["time"], keep="last")

    return df.sort_values("time").reset_index(drop=True)


# ==========================================
# PERSISTENCIA DOS CANDLES
# ==========================================

def get_last_candle_time(symbol, timeframe):
    with sqlite3.connect(DB, timeout=30) as con:
        result = con.execute("""
            SELECT MAX(open_time)
            FROM candles
            WHERE symbol = ? AND timeframe = ?
        """, (symbol, timeframe)).fetchone()

    return result[0] if result else None


def load_candles(symbol, timeframe, limit=250):
    with sqlite3.connect(DB, timeout=30) as con:
        df = pd.read_sql_query("""
            SELECT
                open_time, close_time, open, high, low, close,
                volume, quote_volume, trades,
                taker_buy_base, taker_buy_quote
            FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY open_time DESC
            LIMIT ?
        """, con, params=(symbol, timeframe, limit))

    if df.empty:
        return df

    df = df.iloc[::-1].reset_index(drop=True)
    df = df.rename(columns={"open_time": "time"})

    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)

    numeric_columns = [
        "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote"
    ]

    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def save_candles(symbol, timeframe, df):
    if df.empty:
        return 0

    records = []

    for _, row in df.iterrows():
        records.append((
            symbol,
            timeframe,
            row["time"].isoformat(),
            row["close_time"].isoformat(),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
            float(row["quote_volume"]),
            int(row["trades"]),
            float(row["taker_buy_base"]),
            float(row["taker_buy_quote"])
        ))

    with sqlite3.connect(DB, timeout=30) as con:
        cursor = con.executemany("""
            INSERT OR IGNORE INTO candles (
                symbol, timeframe, open_time, close_time,
                open, high, low, close, volume, quote_volume,
                trades, taker_buy_base, taker_buy_quote
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)

        inserted = cursor.rowcount

    return inserted


# ==========================================
# INDICADORES
# ==========================================

def calculate_indicators(df):
    df = df.copy()
    close = df["close"]

    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["sma200"] = close.rolling(200).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1 / 14, adjust=False
    ).mean()
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    # Tratamento para periodos sem perdas ou sem variacao.
    rsi = rsi.mask((loss == 0) & (gain > 0), 100)
    rsi = rsi.mask((loss == 0) & (gain == 0), 50)
    df["rsi"] = rsi

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
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

    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr_pct"] = df["atr"] / close * 100

    df["volume_avg"] = df["volume"].rolling(20).mean()
    df["relative_volume"] = df["volume"] / df["volume_avg"]

    return df


# ==========================================
# ESTRATEGIA INICIAL
# ==========================================

def analyze(df):
    if df is None or len(df) < 200:
        return None

    indicators = calculate_indicators(df)
    valid = indicators.dropna(subset=[
        "ema21", "ema50", "sma200", "rsi",
        "macd", "macd_signal", "atr_pct",
        "relative_volume"
    ])

    if valid.empty:
        return None

    last = valid.iloc[-1]

    score = 0
    reasons = []

    if last["close"] > last["ema21"] > last["ema50"]:
        score += 2
        reasons.append("Tendencia positiva")
    elif last["close"] < last["ema21"] < last["ema50"]:
        score -= 2
        reasons.append("Tendencia negativa")

    if last["macd"] > last["macd_signal"]:
        score += 1
        reasons.append("MACD positivo")
    else:
        score -= 1
        reasons.append("MACD negativo")

    if 50 <= last["rsi"] <= 68:
        score += 1
        reasons.append("Momentum favoravel")
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
        "relative_volume": round(float(last["relative_volume"]), 2),
        "price": float(last["close"]),
        "reasons": ", ".join(reasons)
    }


def classify(day, swing):
    if day is None and swing is None:
        return "DADOS_INSUFICIENTES"

    if day is None:
        return "ANALISE_PARCIAL_SWING"

    if swing is None:
        return "ANALISE_PARCIAL_DAY"

    if day["score"] >= 2 and swing["score"] >= 2:
        return "COMPRA_EM_OBSERVACAO"

    if day["score"] <= -2 and swing["score"] <= -2:
        return "EVITAR_OU_REAVALIAR"

    return "AGUARDAR"


def combine_scores(primary, confirmation):
    if primary is None or confirmation is None:
        return None

    combined = primary.copy()
    combined["score"] = round(
        (primary["score"] + confirmation["score"]) / 2
    )
    return combined


# ==========================================
# ARMAZENAMENTO DOS SINAIS
# ==========================================

def save_results(results):
    if not results:
        return 0

    df = pd.DataFrame(results)

    with sqlite3.connect(DB, timeout=30) as con:
        df.to_sql(
            "signals",
            con,
            if_exists="append",
            index=False
        )

    return len(df)


# ==========================================
# EXECUCAO
# ==========================================

def main():
    print("\nCRYPTO QUANT MONITOR 2.2")
    print("Iniciando analise Binance Spot...\n")
    print(f"Banco: {DB}")

    init_database()

    run_id, started_at = start_run()

    stats = {
        "assets_approved": 0,
        "assets_selected": 0,
        "analyses_completed": 0,
        "partial_analyses": 0,
        "assets_failed": 0,
        "candles_inserted": 0,
        "signals_saved": 0
    }

    run_status = "COMPLETED"
    run_error = None

    try:
        assets = get_market_universe()
        stats["assets_approved"] = len(assets)

        print(f"Ativos aprovados nos filtros: {len(assets)}")

        if MAX_ASSETS is not None:
            assets = assets[:MAX_ASSETS]

        stats["assets_selected"] = len(assets)
        print(f"Ativos selecionados para analise: {len(assets)}")

        results = []

        for i, asset in enumerate(assets, 1):
            symbol = asset["symbol"]

            print(f"\n[{i}/{len(assets)}] Analisando {symbol}")

            frames = {}

            for interval in INTERVALS:
                try:
                    last_open_time = get_last_candle_time(
                        symbol, interval
                    )

                    new_df = get_candles(
                        symbol, interval, last_open_time
                    )

                    inserted = save_candles(
                        symbol, interval, new_df
                    )
                    stats["candles_inserted"] += inserted

                    history_df = load_candles(
                        symbol, interval, KLINE_LIMIT
                    )
                    frames[interval] = history_df

                    print(
                        f"  {interval}: "
                        f"{len(new_df)} retornados pela API, "
                        f"{inserted} novos, "
                        f"{len(history_df)} no historico local"
                    )

                    time.sleep(REQUEST_PAUSE)

                except Exception as error:
                    print(f"  Erro em {symbol} ({interval}): {error}")
                    frames[interval] = pd.DataFrame()

            # Day Trade usa 15m + confirmacao 1h.
            day_primary = analyze(frames.get("15m"))
            day_confirmation = analyze(frames.get("1h"))
            day = combine_scores(day_primary, day_confirmation)

            # Swing Trade usa 4h + confirmacao 1d.
            swing_primary = analyze(frames.get("4h"))
            swing_confirmation = analyze(frames.get("1d"))
            swing = combine_scores(swing_primary, swing_confirmation)

            signal = classify(day, swing)

            if day is None:
                print("  Day Trade: dados insuficientes")
            else:
                print(
                    f"  Day Trade: score {day['score']} "
                    f"(RSI {day['rsi']}, ATR {day['atr_pct']}%)"
                )

            if swing is None:
                print("  Swing Trade: dados insuficientes")
            else:
                print(
                    f"  Swing Trade: score {swing['score']} "
                    f"(RSI {swing['rsi']}, ATR {swing['atr_pct']}%)"
                )

            if day is None and swing is None:
                stats["assets_failed"] += 1
                print(f"  Sem dados suficientes para {symbol}")
                continue

            if day is None or swing is None:
                stats["partial_analyses"] += 1
            else:
                stats["analyses_completed"] += 1

            results.append({
                "timestamp": utc_now_iso(),
                "symbol": symbol,
                "price": asset["price"],
                "volume_24h": asset["volume"],
                "spread": asset["spread"],
                "change_24h": asset["change24h"],
                "day_score": day["score"] if day else None,
                "swing_score": swing["score"] if swing else None,
                "day_rsi": day["rsi"] if day else None,
                "swing_rsi": swing["rsi"] if swing else None,
                "day_atr_pct": day["atr_pct"] if day else None,
                "swing_atr_pct": swing["atr_pct"] if swing else None,
                "signal": signal
            })

            print(f"  Resultado: {signal}")

        stats["signals_saved"] = save_results(results)

        print("\n================================")
        print("RESUMO DA EXECUCAO")
        print("================================")
        print(f"Ativos aprovados: {stats['assets_approved']}")
        print(f"Ativos selecionados: {stats['assets_selected']}")
        print(f"Analises completas: {stats['analyses_completed']}")
        print(f"Analises parciais: {stats['partial_analyses']}")
        print(f"Ativos sem analise: {stats['assets_failed']}")
        print(f"Candles novos armazenados: {stats['candles_inserted']}")
        print(f"Sinais registrados: {stats['signals_saved']}")
        print(f"Banco de dados: {DB}")
        print("\nExecucao finalizada.")

    except Exception as error:
        run_status = "FAILED"
        run_error = str(error)
        raise

    finally:
        finish_run(
            run_id,
            started_at,
            stats,
            status=run_status,
            error=run_error
        )


if __name__ == "__main__":
    main()
