PRAGMA foreign_keys = ON;

-- Contas e corretoras
CREATE TABLE IF NOT EXISTS portfolio_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    broker TEXT NOT NULL,
    market TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Cadastro dos ativos
CREATE TABLE IF NOT EXISTS portfolio_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    asset_type TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    isin TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (market, symbol)
);

-- Arquivos e processos de importação
CREATE TABLE IF NOT EXISTS portfolio_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    snapshot_date TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'VALIDATED', 'IMPORTED', 'FAILED')),
    row_count INTEGER DEFAULT 0,
    imported_at TEXT,
    metadata_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES portfolio_accounts(id),
    UNIQUE (account_id, file_sha256)
);

-- Posições por fotografia de carteira
CREATE TABLE IF NOT EXISTS portfolio_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    import_id INTEGER,
    snapshot_date TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_cost REAL,
    market_price REAL,
    market_value REAL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'MANUAL',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES portfolio_accounts(id),
    FOREIGN KEY (asset_id) REFERENCES portfolio_assets(id),
    FOREIGN KEY (import_id) REFERENCES portfolio_imports(id),
    UNIQUE (account_id, asset_id, snapshot_date, source)
);

-- Compras, vendas, taxas e transferências
CREATE TABLE IF NOT EXISTS portfolio_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    import_id INTEGER,
    external_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    transaction_date TEXT NOT NULL,
    transaction_type TEXT NOT NULL
        CHECK (transaction_type IN (
            'BUY', 'SELL', 'TRANSFER_IN', 'TRANSFER_OUT', 'FEE', 'ADJUSTMENT'
        )),
    quantity REAL NOT NULL DEFAULT 0,
    unit_price REAL,
    gross_amount REAL,
    fees REAL DEFAULT 0,
    fee_currency TEXT,
    currency TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES portfolio_accounts(id),
    FOREIGN KEY (asset_id) REFERENCES portfolio_assets(id),
    FOREIGN KEY (import_id) REFERENCES portfolio_imports(id)
);

-- Dividendos, JCP e rendimentos
CREATE TABLE IF NOT EXISTS portfolio_income (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    import_id INTEGER,
    external_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    gross_amount REAL NOT NULL,
    net_amount REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RECEIVED', 'CANCELLED')),
    reference_date TEXT,
    expected_payment_date TEXT,
    received_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES portfolio_accounts(id),
    FOREIGN KEY (asset_id) REFERENCES portfolio_assets(id),
    FOREIGN KEY (import_id) REFERENCES portfolio_imports(id)
);

-- Histórico de consolidação patrimonial
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    total_market_value REAL NOT NULL DEFAULT 0,
    available_cash REAL NOT NULL DEFAULT 0,
    pending_income REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES portfolio_accounts(id),
    UNIQUE (account_id, snapshot_date, currency)
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_date
    ON portfolio_positions(account_id, snapshot_date);

CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_date
    ON portfolio_transactions(transaction_date);

CREATE INDEX IF NOT EXISTS idx_portfolio_income_status
    ON portfolio_income(account_id, status, expected_payment_date);

CREATE INDEX IF NOT EXISTS idx_portfolio_assets_symbol
    ON portfolio_assets(symbol);
