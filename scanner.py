import time
import sqlite3
import requests
import pandas as pd
import json
import re

from pathlib import Path
from datetime import datetime, timezone


# ==========================================
# CONFIGURACOES
# ==========================================

BASE_URL = "https://data-api.binance.vision"

MIN_VOLUME = 1_000_000
MAX_SPREAD = 0.50

HISTORY_LIMIT = 500
KLINE_LIMIT = HISTORY_LIMIT  # compatibilidade com a versao anterior
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
    "User-Agent": "CryptoQuantMonitor/2.4"
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

        # Novas metricas de qualidade; ALTER TABLE preserva execucoes anteriores.
        existing_columns = {
            row[1] for row in con.execute("PRAGMA table_info(scanner_runs)")
        }
        for column in ("history_backfilled", "gaps_found",
                       "gaps_repaired", "gaps_pending"):
            if column not in existing_columns:
                con.execute(
                    f"ALTER TABLE scanner_runs ADD COLUMN {column} INTEGER DEFAULT 0"
                )

        # Metricas adicionais da versao 2.4.
        for column in ("history_audits_saved", "outcomes_updated"):
            if column not in existing_columns:
                con.execute(
                    f"ALTER TABLE scanner_runs ADD COLUMN {column} INTEGER DEFAULT 0"
                )

        # Snapshot dos ativos escolhidos em cada execucao.
        con.execute("""
            CREATE TABLE IF NOT EXISTS scanner_run_assets (
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                selection_rank INTEGER NOT NULL,
                volume_24h REAL,
                spread REAL,
                reference_price REAL,
                change_24h REAL,
                analysis_status TEXT DEFAULT 'PENDING',
                signal TEXT,
                selected_at TEXT NOT NULL,
                PRIMARY KEY (run_id, symbol)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_run_assets_symbol
            ON scanner_run_assets(symbol, run_id)
        """)

        # Auditoria da cobertura observada dentro do intervalo armazenado.
        con.execute("""
            CREATE TABLE IF NOT EXISTS history_audit (
                run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                candle_count INTEGER NOT NULL,
                first_open_time TEXT,
                last_open_time TEXT,
                expected_in_span INTEGER,
                missing_internal INTEGER DEFAULT 0,
                coverage_pct REAL,
                target_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                audited_at TEXT NOT NULL,
                PRIMARY KEY (run_id, symbol, timeframe)
            )
        """)

        # Acompanhamento futuro de sinais de compra/venda em observacao.
        # O retorno e medido no preco; nao representa uma ordem ou short.
        con.execute("""
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal TEXT NOT NULL,
                reference_price REAL NOT NULL,
                horizon_hours INTEGER NOT NULL,
                target_time TEXT NOT NULL,
                observed_at TEXT,
                observed_time TEXT,
                observed_price REAL,
                price_change_pct REAL,
                signal_aligned_move_pct REAL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                UNIQUE (signal_timestamp, symbol, horizon_hours)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_pending
            ON signal_outcomes(status, target_time)
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
                history_backfilled = ?,
                gaps_found = ?,
                gaps_repaired = ?,
                gaps_pending = ?,
                history_audits_saved = ?,
                outcomes_updated = ?,
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
            stats["history_backfilled"],
            stats["gaps_found"],
            stats["gaps_repaired"],
            stats["gaps_pending"],
            stats["history_audits_saved"],
            stats["outcomes_updated"],
            status,
            error,
            run_id
        ))


def save_selected_assets(run_id, assets):
    """Registra o universo selecionado, preservando a ordem por volume."""
    selected_at = utc_now_iso()
    records = []
    for rank, asset in enumerate(assets, 1):
        records.append((
            run_id,
            asset["symbol"],
            rank,
            asset["volume"],
            asset["spread"],
            asset["price"],
            asset["change24h"],
            "PENDING",
            None,
            selected_at
        ))

    if records:
        with sqlite3.connect(DB, timeout=30) as con:
            con.executemany("""
                INSERT OR REPLACE INTO scanner_run_assets (
                    run_id, symbol, selection_rank, volume_24h, spread,
                    reference_price, change_24h, analysis_status, signal,
                    selected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)


def update_run_asset(run_id, symbol, analysis_status, signal=None):
    with sqlite3.connect(DB, timeout=30) as con:
        con.execute("""
            UPDATE scanner_run_assets
            SET analysis_status = ?, signal = ?
            WHERE run_id = ? AND symbol = ?
        """, (analysis_status, signal, run_id, symbol))


def save_history_audit(run_id, symbol, timeframe):
    """Audita somente o intervalo entre o primeiro e o ultimo candle salvo.

    Nao presume a data de listagem do ativo na Binance.
    """
    count, first_open, last_open = get_history_bounds(symbol, timeframe)
    missing_ranges = get_missing_candle_ranges(symbol, timeframe)
    missing_internal = sum(item[2] for item in missing_ranges)

    expected = None
    coverage = None
    if count and first_open and last_open:
        interval_ms = INTERVAL_MS[timeframe]
        first_ms = int(pd.Timestamp(first_open).timestamp() * 1000)
        last_ms = int(pd.Timestamp(last_open).timestamp() * 1000)
        expected = max(1, ((last_ms - first_ms) // interval_ms) + 1)
        coverage = round((count / expected) * 100, 4) if expected else None

    if missing_internal > 0:
        status = "GAPS_REMAINING"
    elif count >= HISTORY_LIMIT:
        status = "TARGET_REACHED"
    else:
        status = "SHORT_HISTORY_NO_INTERNAL_GAPS"

    audited_at = utc_now_iso()
    with sqlite3.connect(DB, timeout=30) as con:
        con.execute("""
            INSERT OR REPLACE INTO history_audit (
                run_id, symbol, timeframe, candle_count,
                first_open_time, last_open_time, expected_in_span,
                missing_internal, coverage_pct, target_count,
                status, audited_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, symbol, timeframe, count, first_open, last_open,
            expected, missing_internal, coverage, HISTORY_LIMIT,
            status, audited_at
        ))

    return {
        "candle_count": count,
        "missing_internal": missing_internal,
        "coverage_pct": coverage,
        "status": status
    }


OUTCOME_HORIZONS_HOURS = (1, 4, 24, 168)


def create_signal_outcomes(results):
    """Cria pontos de acompanhamento para sinais direcionais."""
    records = []
    for result in results:
        signal = result["signal"]
        if signal not in ("COMPRA_EM_OBSERVACAO", "VENDA_EM_OBSERVACAO"):
            continue

        reference_price = result.get("price")
        if reference_price is None or reference_price <= 0:
            continue

        signal_time = datetime.fromisoformat(result["timestamp"])
        for horizon in OUTCOME_HORIZONS_HOURS:
            target_time = signal_time + pd.Timedelta(hours=horizon)
            records.append((
                result["timestamp"],
                result["symbol"],
                signal,
                float(reference_price),
                horizon,
                target_time.isoformat(),
                "PENDING"
            ))

    if records:
        with sqlite3.connect(DB, timeout=30) as con:
            con.executemany("""
                INSERT OR IGNORE INTO signal_outcomes (
                    signal_timestamp, symbol, signal, reference_price,
                    horizon_hours, target_time, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, records)

    return len(records)


def update_signal_outcomes():
    """Atualiza resultados vencidos usando o primeiro candle 15m após o alvo."""
    now = utc_now()
    updated = 0

    with sqlite3.connect(DB, timeout=30) as con:
        pending = con.execute("""
            SELECT id, symbol, signal, reference_price, target_time
            FROM signal_outcomes
            WHERE status = 'PENDING' AND target_time <= ?
            ORDER BY target_time
        """, (now.isoformat(),)).fetchall()

        for row_id, symbol, signal, reference_price, target_time in pending:
            candle = con.execute("""
                SELECT open_time, close
                FROM candles
                WHERE symbol = ? AND timeframe = '15m'
                  AND open_time >= ?
                ORDER BY open_time
                LIMIT 1
            """, (symbol, target_time)).fetchone()

            if candle is None or not reference_price:
                continue

            observed_time, observed_price = candle
            price_change = (
                (float(observed_price) / float(reference_price) - 1) * 100
            )
            # Para venda em observacao, queda de preco e movimento alinhado
            # com o alerta. Isso nao simula venda a descoberto.
            aligned_move = (
                price_change if signal == "COMPRA_EM_OBSERVACAO"
                else -price_change
            )

            con.execute("""
                UPDATE signal_outcomes
                SET observed_at = ?,
                    observed_time = ?,
                    observed_price = ?,
                    price_change_pct = ?,
                    signal_aligned_move_pct = ?,
                    status = 'OBSERVED'
                WHERE id = ?
            """, (
                now.isoformat(),
                observed_time,
                float(observed_price),
                round(price_change, 6),
                round(aligned_move, 6),
                row_id
            ))
            updated += 1

    return updated



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
                timeout=(5, 30)
            )

            # Limite de requisicoes da Binance
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

            # Erros temporarios do servidor
            if response.status_code >= 500:
                wait = min(2 ** attempt, 30)

                print(
                    f"Erro temporario HTTP {response.status_code}. "
                    f"Nova tentativa em {wait}s"
                )

                if attempt < 4:
                    time.sleep(wait)
                continue

            # Erros 4xx, exceto 418 e 429, nao devem ser repetidos
            if 400 <= response.status_code < 500:
                raise RuntimeError(
                    f"Erro HTTP definitivo {response.status_code} "
                    f"em {endpoint}: {response.text[:500]}"
                )

            response.raise_for_status()
            return response.json()

        except RuntimeError:
            raise

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


def get_market_universe():
    """
    Monta o universo de ativos Binance Spot/USDT.

    Etapas:
    1. Consulta tickers de 24 horas.
    2. Filtra candidatos por volume e exclusoes.
    3. Valida os candidatos no exchangeInfo em lotes.
    4. Consulta bookTicker em lotes.
    5. Aplica o filtro de spread.
    6. Ordena por volume decrescente.
    """

    BATCH_SIZE = 20

    print("Consultando tickers de 24 horas...")

    # --------------------------------------------------
    # 1. Tickers de 24 horas
    # --------------------------------------------------

    tickers = api_get("/api/v3/ticker/24hr")

    ticker_map = {
        item["symbol"]: item
        for item in tickers
    }

    print(f"Total de pares recebidos: {len(ticker_map)}")

    # --------------------------------------------------
    # 2. Pre-selecao dos candidatos
    # --------------------------------------------------

    candidate_symbols = []
    invalid_symbols = []

    for item in tickers:
        symbol = item.get("symbol", "")

        # Aceita somente caracteres permitidos pela Binance
        if not re.fullmatch(r"[A-Z0-9._-]{1,50}", symbol):
            invalid_symbols.append(symbol)
            continue

        if not symbol.endswith("USDT"):
            continue

        base_asset = symbol[:-4]

        if base_asset in EXCLUDED_BASE_ASSETS:
            continue

        if symbol in EXCLUDED_SYMBOLS:
            continue

        try:
            volume = float(item["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue

        if volume < MIN_VOLUME:
            continue

        candidate_symbols.append(symbol)

        if invalid_symbols:
            print(
                f"Simbolos ignorados por caracteres invalidos: "
                f"{len(invalid_symbols)}"
            )
            print(f"Exemplos: {invalid_symbols[:10]}")
    
    # Mantem a ordem de volume decrescente
    candidate_symbols.sort(
        key=lambda symbol: float(
            ticker_map[symbol]["quoteVolume"]
        ),
        reverse=True
    )

    print(
        f"Candidatos preliminares: "
        f"{len(candidate_symbols)}"
    )

    if not candidate_symbols:
        return []

    # --------------------------------------------------
    # 3. Validacao no exchangeInfo em lotes
    # --------------------------------------------------

    print("Validando ativos no exchangeInfo...")

    valid_symbols = []

    for start in range(0, len(candidate_symbols), BATCH_SIZE):

        batch = candidate_symbols[
            start:start + BATCH_SIZE
        ]

        print(
            f"  exchangeInfo: lote "
            f"{start // BATCH_SIZE + 1} "
            f"({len(batch)} ativos)"
        )

        info = api_get(
            "/api/v3/exchangeInfo",
            params={
                "symbols": json.dumps(
                    batch,
                    separators=(",", ":")
                )
            }
        )

        for item in info.get("symbols", []):

            symbol = item["symbol"]

            if item.get("status") != "TRADING":
                continue

            if item.get("quoteAsset") != "USDT":
                continue

            if item.get("baseAsset") in EXCLUDED_BASE_ASSETS:
                continue

            if symbol in EXCLUDED_SYMBOLS:
                continue

            if item.get("isSpotTradingAllowed") is False:
                continue

            valid_symbols.append(symbol)

        time.sleep(REQUEST_PAUSE)

    print(
        f"Ativos aprovados pelo exchangeInfo: "
        f"{len(valid_symbols)}"
    )

    if not valid_symbols:
        return []

    # --------------------------------------------------
    # 4. Consulta bookTicker em lotes
    # --------------------------------------------------

    print("Consultando bookTicker...")

    book_map = {}

    for start in range(0, len(valid_symbols), BATCH_SIZE):

        batch = valid_symbols[
            start:start + BATCH_SIZE
        ]

        print(
            f"  bookTicker: lote "
            f"{start // BATCH_SIZE + 1} "
            f"({len(batch)} ativos)"
        )

        books = api_get(
            "/api/v3/ticker/bookTicker",
            params={
                "symbols": json.dumps(
                    batch,
                    separators=(",", ":")
                )
            }
        )

        for item in books:
            book_map[item["symbol"]] = item

        time.sleep(REQUEST_PAUSE)

    # --------------------------------------------------
    # 5. Filtro de spread e montagem do universo
    # --------------------------------------------------

    assets = []

    for symbol in valid_symbols:

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

        if volume < MIN_VOLUME:
            continue

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
            "price": price,
            "change24h": change_24h
        })

    # --------------------------------------------------
    # 6. Ordenacao por volume
    # --------------------------------------------------

    assets.sort(
        key=lambda item: item["volume"],
        reverse=True
    )

    print(
        f"Ativos aprovados nos filtros: "
        f"{len(assets)}"
    )

    return assets


# ==========================================
# COLETA DE CANDLES
# ==========================================

INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000
}


def parse_klines(data):
    columns = [
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]

    df = pd.DataFrame(data, columns=columns)
    if df.empty:
        return df

    numeric_columns = [
        "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote"
    ]
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(
        df["close_time"], unit="ms", utc=True
    )

    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now].copy()
    df = df.dropna(subset=[
        "time", "close_time", "open", "high", "low", "close", "volume"
    ])
    df = df.drop_duplicates(subset=["time"], keep="last")
    return df.sort_values("time").reset_index(drop=True)


def get_candles(symbol, interval, last_open_time=None):
    """
    Primeira coleta: baixa HISTORY_LIMIT candles.
    Coletas seguintes: consulta a partir do ultimo candle salvo,
    com paginacao para recuperar periodos longos sem execucao.
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": API_KLINE_LIMIT if last_open_time is not None else HISTORY_LIMIT
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
        if last_open_time is None or len(data) < API_KLINE_LIMIT:
            break

        next_start = int(data[-1][0]) + INTERVAL_MS[interval]
        if next_start <= params["startTime"]:
            break

        params["startTime"] = next_start
        time.sleep(REQUEST_PAUSE)

    return parse_klines(all_data)


def get_candles_range(symbol, interval, start_ms, end_ms):
    """Busca candles fechados dentro de um intervalo UTC, com paginacao."""
    if start_ms > end_ms:
        return pd.DataFrame()

    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": int(start_ms),
        "endTime": int(end_ms),
        "limit": API_KLINE_LIMIT
    }
    all_data = []

    while params["startTime"] <= end_ms:
        data = api_get("/api/v3/klines", params)
        if not data:
            break

        all_data.extend(data)
        next_start = int(data[-1][0]) + INTERVAL_MS[interval]
        if next_start <= params["startTime"]:
            break

        params["startTime"] = next_start
        if len(data) < API_KLINE_LIMIT:
            break
        time.sleep(REQUEST_PAUSE)

    return parse_klines(all_data)


def get_candles_before(symbol, interval, first_open_time, limit):
    """Busca candles anteriores ao mais antigo candle atualmente salvo."""
    if limit <= 0:
        return pd.DataFrame()

    first_ms = int(pd.Timestamp(first_open_time).timestamp() * 1000)
    params = {
        "symbol": symbol,
        "interval": interval,
        "endTime": first_ms - 1,
        "limit": min(int(limit), API_KLINE_LIMIT)
    }
    return parse_klines(api_get("/api/v3/klines", params))


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
# QUALIDADE E REPARACAO DO HISTORICO
# ==========================================

def get_history_bounds(symbol, timeframe):
    with sqlite3.connect(DB, timeout=30) as con:
        row = con.execute("""
            SELECT COUNT(*), MIN(open_time), MAX(open_time)
            FROM candles
            WHERE symbol = ? AND timeframe = ?
        """, (symbol, timeframe)).fetchone()
    return row


def ensure_history_depth(symbol, timeframe):
    """
    Tenta completar o historico ate HISTORY_LIMIT candles.
    Ativos novos podem ter menos candles por ainda nao existirem ha tempo suficiente.
    """
    count, first_open, _ = get_history_bounds(symbol, timeframe)
    if count == 0 or not first_open or count >= HISTORY_LIMIT:
        return 0

    missing = HISTORY_LIMIT - count
    older = get_candles_before(symbol, timeframe, first_open, missing)
    inserted = save_candles(symbol, timeframe, older)

    if inserted:
        print(
            f"  Historico ampliado ({timeframe}): "
            f"{inserted} candles anteriores recuperados"
        )
    return inserted


def get_missing_candle_ranges(symbol, timeframe):
    """Retorna intervalos de candles ausentes entre registros existentes."""
    interval_ms = INTERVAL_MS[timeframe]

    with sqlite3.connect(DB, timeout=30) as con:
        rows = con.execute("""
            SELECT open_time
            FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY open_time
        """, (symbol, timeframe)).fetchall()

    if len(rows) < 2:
        return []

    timestamps = [
        int(pd.Timestamp(row[0]).timestamp() * 1000)
        for row in rows
    ]

    ranges = []
    for previous_ms, current_ms in zip(timestamps, timestamps[1:]):
        delta = current_ms - previous_ms
        if delta > interval_ms:
            missing_count = (delta // interval_ms) - 1
            if missing_count > 0:
                ranges.append((
                    previous_ms + interval_ms,
                    current_ms - interval_ms,
                    int(missing_count)
                ))
    return ranges


def repair_candle_gaps(symbol, timeframe):
    """
    Detecta e tenta reparar lacunas entre candles armazenados.
    gaps_found/gaps_pending contam candles ausentes, nao apenas intervalos.
    """
    ranges = get_missing_candle_ranges(symbol, timeframe)
    missing_before = sum(item[2] for item in ranges)
    inserted_total = 0

    if not ranges:
        return 0, 0, 0

    print(
        f"  Gaps em {timeframe}: {len(ranges)} intervalo(s), "
        f"{missing_before} candle(s) ausente(s)"
    )

    for start_ms, end_ms, _ in ranges:
        recovered = get_candles_range(
            symbol, timeframe, start_ms, end_ms
        )
        inserted = save_candles(symbol, timeframe, recovered)
        inserted_total += inserted

        if inserted:
            print(f"    {timeframe}: {inserted} candle(s) recuperado(s)")
        time.sleep(REQUEST_PAUSE)

    remaining_ranges = get_missing_candle_ranges(symbol, timeframe)
    pending = sum(item[2] for item in remaining_ranges)
    return missing_before, inserted_total, pending


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
        return "VENDA_EM_OBSERVACAO"

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
    print("\nCRYPTO QUANT MONITOR 2.4")
    print("Iniciando analise Binance Spot e verificacao historica...\n")
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
        "history_backfilled": 0,
        "gaps_found": 0,
        "gaps_repaired": 0,
        "gaps_pending": 0,
        "history_audits_saved": 0,
        "outcomes_updated": 0,
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

        save_selected_assets(run_id, assets)
        stats["outcomes_updated"] = update_signal_outcomes()

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

                    # Completa historico anterior ate HISTORY_LIMIT, quando disponivel.
                    older_inserted = ensure_history_depth(symbol, interval)
                    stats["history_backfilled"] += older_inserted
                    stats["candles_inserted"] += older_inserted

                    # Detecta e recupera lacunas internas no historico.
                    found, repaired, pending = repair_candle_gaps(
                        symbol, interval
                    )
                    stats["gaps_found"] += found
                    stats["gaps_repaired"] += repaired
                    stats["gaps_pending"] += pending
                    stats["candles_inserted"] += repaired

                    history_df = load_candles(
                        symbol, interval, HISTORY_LIMIT
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

                # Audita mesmo quando a consulta da API falha, usando o banco local.
                try:
                    audit = save_history_audit(run_id, symbol, interval)
                    stats["history_audits_saved"] += 1
                    print(
                        f"  Auditoria {interval}: {audit['candle_count']} candles, "
                        f"{audit['coverage_pct']}% no intervalo, {audit['status']}"
                    )
                except Exception as audit_error:
                    print(f"  Erro na auditoria {symbol} ({interval}): {audit_error}")

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
                update_run_asset(run_id, symbol, "NO_ANALYSIS", signal)
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

            analysis_status = "PARTIAL" if (day is None or swing is None) else "COMPLETED"
            update_run_asset(run_id, symbol, analysis_status, signal)

            print(f"  Resultado: {signal}")

        stats["signals_saved"] = save_results(results)
        tracking_created = create_signal_outcomes(results)
        print(f"Pontos de acompanhamento de sinais criados: {tracking_created}")

        print("\n================================")
        print("RESUMO DA EXECUCAO")
        print("================================")
        print(f"Ativos aprovados: {stats['assets_approved']}")
        print(f"Ativos selecionados: {stats['assets_selected']}")
        print(f"Analises completas: {stats['analyses_completed']}")
        print(f"Analises parciais: {stats['partial_analyses']}")
        print(f"Ativos sem analise: {stats['assets_failed']}")
        print(f"Candles novos armazenados: {stats['candles_inserted']}")
        print(
            f"Historico anterior recuperado: {stats['history_backfilled']} "
            "(ja incluido nos candles novos armazenados)"
        )
        print(f"Gaps encontrados (candles): {stats['gaps_found']}")
        print(f"Gaps reparados (candles): {stats['gaps_repaired']}")
        print(f"Gaps pendentes (candles): {stats['gaps_pending']}")
        print(f"Auditorias de historico salvas: {stats['history_audits_saved']}")
        print(f"Resultados de sinais atualizados: {stats['outcomes_updated']}")
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
