#!/usr/bin/env python3
"""
Publica métricas do Crypto Quant Monitor no ThingsBoard via HTTP telemetry.
Lê somente o SQLite local; não envia ordens à Binance.
"""
import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CRYPTO_DB_PATH", str(PROJECT_DIR / "data" / "crypto_market.db")))
TB_URL = os.getenv("TB_URL", "http://127.0.0.1:8080").rstrip("/")
TB_TOKEN = os.getenv("TB_TOKEN", "").strip()
HTTP_TIMEOUT = int(os.getenv("TB_HTTP_TIMEOUT", "15"))
MAX_ASSETS = int(os.getenv("TB_MAX_ASSETS", "20"))
LOG_PATH = PROJECT_DIR / "logs" / "thingsboard_publisher.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("tb-publisher")


def table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def latest_run(con):
    if not table_exists(con, "scanner_runs"):
        raise RuntimeError("Tabela scanner_runs não encontrada.")
    row = con.execute("""
        SELECT id, started_at, finished_at, duration_seconds, assets_approved,
               assets_selected, analyses_completed, partial_analyses,
               assets_failed, candles_inserted, signals_saved, status
        FROM scanner_runs ORDER BY id DESC LIMIT 1
    """).fetchone()
    if not row:
        raise RuntimeError("Ainda não existe execução do scanner.")
    names = [
        "id", "started_at", "finished_at", "duration_seconds",
        "assets_approved", "assets_selected", "analyses_completed",
        "partial_analyses", "assets_failed", "candles_inserted",
        "signals_saved", "status"
    ]
    result = dict(zip(names, row))
    # Busca as colunas adicionais de forma compatível com bancos anteriores.
    available = {r[1] for r in con.execute("PRAGMA table_info(scanner_runs)")}
    for column in (
        "history_backfilled", "gaps_found", "gaps_repaired", "gaps_pending",
        "history_audits_saved", "outcomes_updated"
    ):
        if column in available:
            result[column] = con.execute(
                f"SELECT {column} FROM scanner_runs WHERE id=?", (result["id"],)
            ).fetchone()[0]
    return result


def collect_telemetry():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco não encontrado: {DB_PATH}")

    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15) as con:
        con.row_factory = sqlite3.Row
        run = latest_run(con)
        run_id = run["id"]

        telemetry = {
            "monitor_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "scanner_run_id": int(run_id),
            "scanner_status": str(run["status"] or "UNKNOWN"),
            "scanner_duration_seconds": float(run["duration_seconds"] or 0),
            "assets_approved": int(run["assets_approved"] or 0),
            "assets_selected": int(run["assets_selected"] or 0),
            "analyses_completed": int(run["analyses_completed"] or 0),
            "partial_analyses": int(run["partial_analyses"] or 0),
            "assets_failed": int(run["assets_failed"] or 0),
            "candles_inserted_last_run": int(run["candles_inserted"] or 0),
            "signals_saved_last_run": int(run["signals_saved"] or 0),
        }

        for col, key in [
            ("history_backfilled", "history_backfilled_last_run"),
            ("gaps_found", "history_gaps_found_last_run"),
            ("gaps_repaired", "history_gaps_repaired_last_run"),
            ("gaps_pending", "history_gaps_pending_last_run"),
            ("history_audits_saved", "history_audits_saved_last_run"),
            ("outcomes_updated", "outcomes_updated_last_run"),
        ]:
            if col in run:
                telemetry[key] = int(run[col] or 0)

        if table_exists(con, "candles"):
            telemetry["candles_total"] = int(
                con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            )

        if table_exists(con, "scanner_run_assets"):
            rows = con.execute("""
                SELECT symbol, selection_rank, reference_price, volume_24h,
                       spread, change_24h, analysis_status, signal
                FROM scanner_run_assets
                WHERE run_id = ?
                ORDER BY selection_rank
                LIMIT ?
            """, (run_id, MAX_ASSETS)).fetchall()

            buy = sell = wait = partial = 0
            for r in rows:
                symbol = r["symbol"]
                status = (r["analysis_status"] or "").upper()
                signal = (r["signal"] or "").upper()

                if signal == "COMPRA_EM_OBSERVACAO":
                    buy += 1
                elif signal == "VENDA_EM_OBSERVACAO":
                    sell += 1
                elif signal == "AGUARDAR":
                    wait += 1
                if status not in ("COMPLETED", "COMPLETED_WITH_SIGNAL", "ANALYZED"):
                    if not signal:
                        partial += 1

                # Telemetria individual por ativo, útil para widgets no ThingsBoard.
                telemetry[f"signal_{symbol}"] = signal or status or "SEM_SINAL"
                for field, key in [
                    ("reference_price", "price"),
                    ("volume_24h", "volume_24h"),
                    ("spread", "spread_pct"),
                    ("change_24h", "change_24h_pct"),
                ]:
                    value = r[field]
                    if value is not None:
                        telemetry[f"{key}_{symbol}"] = float(value)

            telemetry["buy_observation_count"] = buy
            telemetry["sell_observation_count"] = sell
            telemetry["wait_count"] = wait
            telemetry["assets_without_signal_count"] = partial

        if table_exists(con, "signal_outcomes"):
            counts = con.execute("""
                SELECT status, COUNT(*) AS total
                FROM signal_outcomes GROUP BY status
            """).fetchall()
            for r in counts:
                telemetry[f"outcomes_{str(r['status']).lower()}"] = int(r["total"])

            horizons = con.execute("""
                SELECT horizon_hours,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='OBSERVED' THEN 1 ELSE 0 END) AS observed,
                       AVG(CASE WHEN status='OBSERVED' THEN signal_aligned_move_pct END) AS avg_aligned_pct
                FROM signal_outcomes
                GROUP BY horizon_hours
            """).fetchall()
            for r in horizons:
                h = int(r["horizon_hours"])
                telemetry[f"outcomes_total_{h}h"] = int(r["total"])
                telemetry[f"outcomes_observed_{h}h"] = int(r["observed"] or 0)
                if r["avg_aligned_pct"] is not None:
                    telemetry[f"outcomes_avg_aligned_pct_{h}h"] = float(r["avg_aligned_pct"])

        if table_exists(con, "history_audit"):
            rows = con.execute("""
                SELECT timeframe, COUNT(*) AS total,
                       SUM(CASE WHEN status='TARGET_REACHED' THEN 1 ELSE 0 END) AS target_reached,
                       SUM(CASE WHEN status='GAPS_REMAINING' THEN 1 ELSE 0 END) AS gaps_remaining,
                       AVG(coverage_pct) AS avg_coverage
                FROM history_audit
                WHERE run_id = ?
                GROUP BY timeframe
            """, (run_id,)).fetchall()
            for r in rows:
                tf = r["timeframe"]
                telemetry[f"history_assets_{tf}"] = int(r["total"])
                telemetry[f"history_target_reached_{tf}"] = int(r["target_reached"] or 0)
                telemetry[f"history_gaps_remaining_{tf}"] = int(r["gaps_remaining"] or 0)
                if r["avg_coverage"] is not None:
                    telemetry[f"history_avg_coverage_pct_{tf}"] = float(r["avg_coverage"])

    # Remove valores não suportados/NaN e garantir tipos JSON simples.
    clean = {}
    for key, value in telemetry.items():
        if value is None:
            continue
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                continue
        clean[key] = value
    return clean


def main():
    if not TB_TOKEN:
        raise RuntimeError("TB_TOKEN não configurado no arquivo de ambiente.")
    payload = collect_telemetry()
    url = f"{TB_URL}/api/v1/{TB_TOKEN}/telemetry"
    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"ThingsBoard HTTP {response.status_code}: {response.text[:500]}"
        )
    log.info(
        "POST enviado: HTTP %s | run_id=%s | %s métricas | %s",
        response.status_code, payload.get("scanner_run_id"),
        len(payload), url.split("/api/")[0]
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Falha ao publicar telemetria no ThingsBoard.")
        sys.exit(1)