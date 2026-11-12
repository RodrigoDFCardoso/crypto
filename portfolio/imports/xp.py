from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook


DEFAULT_FILE = (
    Path(__file__).resolve().parents[2]
    / "imports" / "xp" / "PosicaoDetalhada.xlsx"
)


def number(value):
    if value is None or str(value).strip() == "":
        return None

    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))

    text = str(value).strip()
    text = text.replace("R$", "").replace("%", "").replace(" ", "")
    text = text.replace(".", "").replace(",", ".")

    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Número inválido: {value!r}") from exc


def parse_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()

    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date().isoformat()
        except ValueError:
            pass

    raise ValueError(f"Data inválida: {value!r}")


def brl(value):
    if value is None:
        return "—"

    text = f"{value:,.2f}"
    return "R$ " + text.replace(",", "X").replace(".", ",").replace("X", ".")


def asset_type(symbol):
    if symbol.endswith("34"):
        return "BDR"
    if symbol.endswith("11"):
        return "FII"
    return "STOCK"


def parse_workbook(path):
    if not path.is_file():
        raise FileNotFoundError(path)

    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    wb = load_workbook(path, read_only=True, data_only=True)

    try:
        if "Sua carteira" not in wb.sheetnames:
            raise ValueError("Aba 'Sua carteira' não encontrada.")

        ws = wb["Sua carteira"]
        rows = {
            i: list(row)
            for i, row in enumerate(ws.iter_rows(values_only=True), start=1)
        }

        # Conta e data estão juntas na célula F1 (F1:G1 mescladas).
        header = str(rows[1][5]).strip()

        account_match = re.search(r"Conta:\s*(\d+)", header)
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", header)

        if not account_match or not date_match:
            raise ValueError(
                f"Não foi possível identificar conta/data no cabeçalho: {header!r}"
            )

        account_code = account_match.group(1)
        snapshot_date = parse_date(date_match.group(1))
        xp_total = number(rows[4][0])

        positions = []

        # Ações e BDRs: linhas 9 a 11 e 14.
        for row_no in [9, 10, 11, 14]:
            row = rows[row_no]
            symbol = str(row[0]).strip()

            if not re.fullmatch(r"[A-Z0-9]{4,6}", symbol):
                continue

            qty = number(row[6])
            avg = number(row[4])

            positions.append({
                "symbol": symbol,
                "name": symbol,
                "type": asset_type(symbol),
                "quantity": qty,
                "average_cost": avg,
                "invested_amount": avg * qty,
                "market_price": number(row[5]),
                "market_value": number(row[1]),
                "net_liquidation_value": None,
            })

        # Fundo de investimento: linha 20.
        row = rows[20]

        positions.append({
            "symbol": "TREND_INVESTBACK_III_ONE",
            "name": str(row[0]).strip(),
            "type": "FUND",
            "quantity": None,
            "average_cost": None,
            "invested_amount": number(row[4]),
            "market_price": None,
            "market_value": number(row[2]),
            "net_liquidation_value": number(row[5]),
        })

        # FIIs: linhas 26 a 37.
        for row_no in range(26, 38):
            row = rows[row_no]
            symbol = str(row[0]).strip()

            if not re.fullmatch(r"[A-Z0-9]{4,6}", symbol):
                continue

            qty = number(row[5])
            avg = number(row[3])

            positions.append({
                "symbol": symbol,
                "name": symbol,
                "type": "FII",
                "quantity": qty,
                "average_cost": avg,
                "invested_amount": avg * qty,
                "market_price": number(row[4]),
                "market_value": number(row[1]),
                "net_liquidation_value": None,
            })

        # Proventos: FIIs (47-53) e ações (58-60).
        incomes = []

        for row_no in list(range(47, 54)) + list(range(58, 61)):
            row = rows[row_no]
            symbol = str(row[0]).strip()

            if not re.fullmatch(r"[A-Z0-9]{4,6}", symbol):
                continue

            incomes.append({
                "symbol": symbol,
                "quantity_reference": number(row[1]),
                "event_type": str(row[5]).strip().upper(),
                "gross_amount": number(row[3]),
                "net_amount": number(row[4]),
                "expected_payment_date": parse_date(row[6]),
            })

        section_totals = {
            "AÇÕES E BDRS": number(rows[6][6]),
            "FUNDOS DE INVESTIMENTO": number(rows[17][6]),
            "FIIS": number(rows[23][6]),
        }

        income_fii_total = number(rows[44][6])
        income_stock_total = number(rows[55][6])
        income_gross_total = income_fii_total + income_stock_total

        position_total = sum(
            (p["market_value"] for p in positions),
            Decimal("0")
        )

        section_total = sum(section_totals.values(), Decimal("0"))

        income_gross_sum = sum(
            (i["gross_amount"] for i in incomes),
            Decimal("0")
        )

        income_net_total = sum(
            (i["net_amount"] for i in incomes),
            Decimal("0")
        )

        checks = {
            "17 posições identificadas": len(positions) == 17,
            "10 proventos identificados": len(incomes) == 10,
            "posições conferem com os grupos": position_total == section_total,
            "proventos conferem com os grupos": income_gross_sum == income_gross_total,
            "patrimônio XP confere": xp_total == position_total + income_gross_total,
        }

        return {
            "filename": path.name,
            "sha256": file_hash,
            "account_code": account_code,
            "snapshot_date": snapshot_date,
            "xp_total": xp_total,
            "positions": positions,
            "incomes": incomes,
            "section_totals": section_totals,
            "position_total": position_total,
            "income_gross_total": income_gross_total,
            "income_net_total": income_net_total,
            "checks": checks,
        }

    finally:
        wb.close()


def preview(data):
    print("=" * 105)
    print("CRYPTO QUANT MONITOR - IMPORTADOR XP")
    print("MODO DRY-RUN - SOMENTE LEITURA")
    print("=" * 105)

    print(f"Arquivo:       {data['filename']}")
    print(f"Conta XP:      {data['account_code']}")
    print(f"Data snapshot: {data['snapshot_date']}")
    print(f"SHA-256:       {data['sha256']}")

    print("\nPOSIÇÕES")
    print(
        f"{'ATIVO':30} {'TIPO':7} {'QTD':>7} "
        f"{'PM':>12} {'APLICADO':>13} {'COTAÇÃO':>12} {'MERCADO':>13}"
    )

    for p in data["positions"]:
        qty = f"{p['quantity']:g}" if p["quantity"] is not None else "—"

        print(
            f"{p['symbol']:30} {p['type']:7} {qty:>7} "
            f"{brl(p['average_cost']):>12} "
            f"{brl(p['invested_amount']):>13} "
            f"{brl(p['market_price']):>12} "
            f"{brl(p['market_value']):>13}"
        )

        if p["net_liquidation_value"] is not None:
            print(
                "  Saldo líquido do fundo:",
                brl(p["net_liquidation_value"])
            )

    print("\nTOTAIS DAS POSIÇÕES")
    for name, value in data["section_totals"].items():
        print(f"{name:30} {brl(value)}")

    print("TOTAL DAS POSIÇÕES:".ljust(30), brl(data["position_total"]))

    print("\nPROVENTOS PROVISIONADOS")
    print(
        f"{'ATIVO':10} {'EVENTO':28} {'BRUTO':>12} "
        f"{'LÍQUIDO':>12} {'PAGAMENTO':>12}"
    )

    for item in data["incomes"]:
        print(
            f"{item['symbol']:10} {item['event_type']:28} "
            f"{brl(item['gross_amount']):>12} "
            f"{brl(item['net_amount']):>12} "
            f"{item['expected_payment_date']:>12}"
        )

    print("\nCONFERÊNCIA FINANCEIRA")
    print("Posições:".ljust(30), brl(data["position_total"]))
    print("Proventos brutos:".ljust(30), brl(data["income_gross_total"]))
    print("Proventos líquidos:".ljust(30), brl(data["income_net_total"]))
    print("Patrimônio informado XP:".ljust(30), brl(data["xp_total"]))

    print("\nVALIDAÇÕES")
    for description, passed in data["checks"].items():
        print(f"[{'OK' if passed else 'FALHOU'}] {description}")

    print("\nNenhum dado foi gravado no banco de dados.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()

    data = parse_workbook(args.file)
    preview(data)

    return 0 if all(data["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
