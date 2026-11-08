
import time
import sqlite3
import requests
import pandas as pd

from pathlib import Path
from datetime import datetime, timezone


# ==========================================
# CONFIGURAÇÕES
# ==========================================

BASE_URL = "https://data-api.binance.vision"

MIN_VOLUME = 1_000_000
MAX_SPREAD = 0.50

KLINE_LIMIT = 250
API_KLINE_LIMIT = 1000
REQUEST_PAUSE = 0.15

# Primeira execução de teste.
# Depois de validar, altere para None para analisar todos.
MAX_ASSETS = 20

# Stablecoins que não devem entrar na estratégia.
EXCLUDED_BASE_ASSETS = {
    "USDC",
    "USD1",
    "FDUSD",
    "TUSD",
    "USDP",
    "BUSD",
    "DAI",
    "USDE",
    "UST",
    "USTC",
    "PYUSD",
    "RLUSD",
    "RWUSD",
    "EUR"
}

INTERVALS = ["15m", "1h", "4h", "1d"]

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DB = DATA_DIR / "crypto_market.db"

DATA_DIR.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "CryptoQuantMonitor/2.0"
})


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

                PRIMARY KEY (
                    symbol,
                    timeframe,
                    open_time
                )
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_lookup
            ON candles(symbol, timeframe, open_time)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
            ON signals(timestamp)
        """ if table_exists(con, "signals") else """
            CREATE TABLE IF NOT EXISTS signals (
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


def table_exists(con, table_name):

    result = con.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name=?
    """, (table_name,)).fetchone()

    return result is not None


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
                except ValueError:
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
                    f"Erro temporário HTTP "
                    f"{response.status_code}. "
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

    raise RuntimeError(
        f"Falha persistente na API: {endpoint}"
    )


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

        # Exclui stablecoins negociadas contra USDT.
        if item["baseAsset"] in EXCLUDED_BASE_ASSETS:
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

        spread = (
            (ask - bid) / ((ask + bid) / 2)
        ) * 100

        if spread > MAX_SPREAD:
            continue

        assets.append({
            "symbol": symbol,
            "volume": volume,
            "spread": spread,
            "price": float(ticker["lastPrice"]),
            "change24h": float(
                ticker["priceChangePercent"]
            )
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
    Coleta candles da Binance.

    Primeira coleta:
        Baixa KLINE_LIMIT candles.

    Coletas seguintes:
        Começa no último candle armazenado.
        O último candle é baixado novamente como sobreposição.
        Apenas candles fechados são retornados.
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

        params["startTime"] = int(
            timestamp.timestamp() * 1000
        )

    all_data = []

    while True:

        data = api_get(
            "/api/v3/klines",
            params
        )

        if not data:
            break

        all_data.extend(data)

        # Na primeira coleta, basta baixar KLINE_LIMIT.
        if last_open_time is None:
            break

        # Não há mais páginas para consultar.
        if len(data) < API_KLINE_LIMIT:
            break

        last_candle = data[-1]

        # Se o último candle ainda está em formação,
        # não há candles fechados posteriores disponíveis.
        now_ms = int(time.time() * 1000)

        if int(last_candle[6]) >= now_ms:
            break

        next_start = int(last_candle[0]) + 1

        if next_start <= params["startTime"]:
            break

        params["startTime"] = next_start

        time.sleep(REQUEST_PAUSE)

    columns = [
        "time", "open", "high", "low",
        "close", "volume", "close_time",
        "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote",
        "ignore"
    ]

    df = pd.DataFrame(
        all_data,
        columns=columns
    )

    if df.empty:
        return df

    numeric_columns = [
        "open", "high", "low", "close",
        "volume", "quote_volume",
        "trades", "taker_buy_base",
        "taker_buy_quote"
    ]

    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col])

    df["time"] = pd.to_datetime(
        df["time"],
        unit="ms",
        utc=True
    )

    df["close_time"] = pd.to_datetime(
        df["close_time"],
        unit="ms",
        utc=True
    )

    # Mantém somente candles efetivamente fechados.
    now = pd.Timestamp.now(tz="UTC")

    df = df[
        df["close_time"] < now
    ].copy()

    # Remove eventuais sobreposições entre páginas.
    df = df.drop_duplicates(
        subset=["time"],
        keep="last"
    )

    return df.sort_values(
        "time"
    ).reset_index(drop=True)



# ==========================================
# PERSISTÊNCIA DOS CANDLES
# ==========================================


def get_last_candle_time(symbol, timeframe):

    """
    Retorna o horário de abertura do último candle
    armazenado para determinado ativo e timeframe.
    """

    with sqlite3.connect(DB, timeout=30) as con:

        result = con.execute("""
            SELECT MAX(open_time)
            FROM candles
            WHERE symbol = ?
            AND timeframe = ?
        """, (symbol, timeframe)).fetchone()

    return result[0] if result else None


def load_candles(symbol, timeframe, limit=250):

    """
    Carrega os últimos candles do banco para análise.
    Retorna os mesmos nomes de colunas utilizados
    pela função calculate_indicators().
    """

    with sqlite3.connect(DB, timeout=30) as con:

        df = pd.read_sql_query("""
            SELECT
                open_time,
                close_time,
                open,
                high,
                low,
                close,
                volume,
                quote_volume,
                trades,
                taker_buy_base,
                taker_buy_quote

            FROM candles

            WHERE symbol = ?
            AND timeframe = ?

            ORDER BY open_time DESC

            LIMIT ?
        """, con, params=(symbol, timeframe, limit))

    if df.empty:
        return df

    # Restaura a ordem cronológica.
    df = df.iloc[::-1].reset_index(drop=True)

    df = df.rename(columns={
        "open_time": "time"
    })

    df["time"] = pd.to_datetime(
        df["time"],
        utc=True
    )

    df["close_time"] = pd.to_datetime(
        df["close_time"],
        utc=True
    )

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
                symbol,
                timeframe,
                open_time,
                close_time,
                open,
                high,
                low,
                close,
                volume,
                quote_volume,
                trades,
                taker_buy_base,
                taker_buy_quote
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
# ARMAZENAMENTO DOS SINAIS
# ==========================================

def save_results(results):

    if not results:
        return

    df = pd.DataFrame(results)

    with sqlite3.connect(DB, timeout=30) as con:

        df.to_sql(
            "signals",
            con,
            if_exists="append",
            index=False
        )


# ==========================================
# EXECUÇÃO
# ==========================================

def main():

    print("\nCRYPTO QUANT MONITOR 2.1")
    print("Iniciando análise Binance Spot...\n")
    print(f"Banco: {DB}")

    init_database()

    assets = get_market_universe()

    print(f"Ativos aprovados nos filtros: {len(assets)}")

    if MAX_ASSETS is not None:
        assets = assets[:MAX_ASSETS]

    print(f"Ativos selecionados para análise: {len(assets)}")

    results = []

    total_candles_inserted = 0
    successful_assets = 0
    failed_assets = 0

    for i, asset in enumerate(assets, 1):

        symbol = asset["symbol"]

        print(
            f"\n[{i}/{len(assets)}] Analisando {symbol}"
        )

        frames = {}

        for interval in INTERVALS:

            
            try:

                # Descobre o último candle existente no banco.
                last_open_time = get_last_candle_time(
                    symbol,
                    interval
                )

                # Busca somente candles posteriores,
                # incluindo o último candle como sobreposição.
                new_df = get_candles(
                    symbol,
                    interval,
                    last_open_time
                )

                # Insere apenas registros ainda inexistentes.
                inserted = save_candles(
                    symbol,
                    interval,
                    new_df
                )

                total_candles_inserted += inserted

                # Recupera o histórico local para calcular indicadores.
                history_df = load_candles(
                    symbol,
                    interval,
                    KLINE_LIMIT
                )

                frames[interval] = history_df

                print(
                    f"  {interval}: "
                    f"{len(new_df)} retornados pela API, "
                    f"{inserted} novos, "
                    f"{len(history_df)} no histórico local"
                )

                time.sleep(REQUEST_PAUSE)


            except Exception as error:

                print(
                    f"  Erro em {symbol} ({interval}): {error}"
                )

                frames[interval] = pd.DataFrame()

        # Verifica se os quatro timeframes foram coletados.
        if any(
            interval not in frames or frames[interval].empty
            for interval in INTERVALS
        ):

            print(
                f"  Análise não realizada: "
                f"histórico incompleto em {symbol}"
            )

            failed_assets += 1
            continue

        try:

            day = analyze(frames["15m"])
            day_confirmation = analyze(frames["1h"])

            swing = analyze(frames["4h"])
            swing_confirmation = analyze(frames["1d"])

            if any(x is None for x in [
                day,
                day_confirmation,
                swing,
                swing_confirmation
            ]):

                print(
                    f"  Dados insuficientes para indicadores: "
                    f"{symbol}"
                )

                failed_assets += 1
                continue

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

            successful_assets += 1

            print(
                f"  Day: {day['score']:2} | "
                f"Swing: {swing['score']:2} | "
                f"{signal}"
            )

        except Exception as error:

            failed_assets += 1

            print(
                f"  Erro na análise de {symbol}: {error}"
            )

    save_results(results)

    print("\n================================")
    print("RESUMO DA EXECUÇÃO")
    print("================================")

    print(f"Ativos selecionados: {len(assets)}")
    print(f"Análises concluídas: {successful_assets}")
    print(f"Ativos com falha/sem dados: {failed_assets}")

    print(
        f"Candles novos armazenados: "
        f"{total_candles_inserted}"
    )

    print(f"Sinais registrados: {len(results)}")
    print(f"Banco de dados: {DB}")

    print("\nExecução finalizada.")


if __name__ == "__main__":
    main()
