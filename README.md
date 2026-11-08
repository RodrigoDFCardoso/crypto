# Crypto Quant Monitor

Monitor quantitativo de mercado para ativos **Binance Spot com cotação
em USDT**. O projeto coleta candles, calcula indicadores técnicos,
classifica condições de mercado para acompanhamento e armazena os
resultados localmente em SQLite. Um publicador separado envia métricas
ao ThingsBoard por HTTP.

> **Importante:** este projeto é uma ferramenta de monitoramento e
> pesquisa. Não envia ordens, não executa operações e não deve ser
> interpretado como recomendação de investimento.

## Recursos

-   Consulta do universo Binance Spot/USDT.
-   Filtros de volume, spread, ativos-base e símbolos excluídos.
-   Coleta de candles fechados nos períodos `15m`, `1h`, `4h` e `1d`.
-   Persistência local em SQLite, com chave única por
    ativo/período/abertura do candle.
-   Recuperação de histórico anterior até a profundidade configurada.
-   Detecção e tentativa de reparo de lacunas internas de candles.
-   Indicadores: EMA 9/21/50, SMA 200, RSI 14, MACD, ATR 14 e volume
    relativo.
-   Classificações `COMPRA_EM_OBSERVACAO`, `VENDA_EM_OBSERVACAO`,
    `AGUARDAR` e estados de análise parcial/insuficiente.
-   Registro de execuções, ativos selecionados, auditoria de histórico e
    acompanhamento futuro dos sinais.
-   Publicação de métricas no ThingsBoard CE por meio de
    `thingsboard_publisher.py`.

## Arquitetura

``` text
Binance Spot Market Data API
           |
           v
       scanner.py
           |
           v
   SQLite (data/crypto_market.db)
      |                 |
      |                 +--> histórico, auditorias e resultados de sinais
      |
      v
thingsboard_publisher.py
           |
           v
   ThingsBoard CE 4.4.0
           |
           v
 Dashboard Crypto Quant Monitor
```

O scanner e o publicador são processos separados. O publicador lê o
banco SQLite e envia um snapshot da execução mais recente; ele não
consulta novamente a Binance.

## Requisitos

-   Python 3.10 ou superior (ambiente validado no Python 3.13).
-   Conexão com a internet para consultar a API pública de dados da
    Binance.
-   ThingsBoard CE 4.4.0 (opcional, para dashboard e telemetria).
-   Linux com `systemd` (opcional, para execução agendada).

Dependências Python:

-   `pandas`
-   `requests`

## Instalação

No Raspberry Pi ou Linux:

``` bash
git clone <URL_DO_SEU_REPOSITORIO>
cd crypto-quant-monitor

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

Crie as pastas de dados e logs (os scripts também criam a pasta
`data`/`logs` quando necessário):

``` bash
mkdir -p data logs
```

## Configuração do ThingsBoard

Copie o modelo de ambiente:

``` bash
cp .thingsboard.env.example .thingsboard.env
nano .thingsboard.env
```

Preencha `TB_TOKEN` com o token de acesso do dispositivo ThingsBoard
criado para o Crypto Quant Monitor. **Não coloque o token real no Git.**

Exemplo de variáveis:

``` ini
CRYPTO_DB_PATH=/home/pi4/crypto-quant-monitor/data/crypto_market.db
TB_URL=http://127.0.0.1:8080
TB_TOKEN=COLOQUE_O_TOKEN_DO_DISPOSITIVO_AQUI
TB_HTTP_TIMEOUT=15
TB_MAX_ASSETS=20
```

O arquivo `.thingsboard.env` é ignorado pelo Git. O modelo
`.thingsboard.env.example` pode ser versionado porque não contém
credenciais.

## Execução manual

Ative o ambiente virtual:

``` bash
cd ~/crypto-quant-monitor
source .venv/bin/activate
```

Execute o scanner:

``` bash
python scanner.py
```

Depois publique a execução mais recente no ThingsBoard:

``` bash
python thingsboard_publisher.py
```

O publicador deve registrar uma resposta HTTP `200` ou `204`. Exemplo de
log:

``` text
POST enviado: HTTP 200 | run_id=17 | 150 métricas | http://127.0.0.1:8080
```

O número de métricas varia conforme os ativos selecionados e a
disponibilidade de tabelas/dados opcionais.

### Reenviar dados

Na versão atual, `thingsboard_publisher.py` publica a execução mais
recente do banco. Ele **não implementa** o argumento `--run-id`. Para
reenviar uma execução antiga, será necessário adaptar o publicador para
receber o ID e consultar os registros daquela execução; não use
`python thingsboard_publisher.py --run-id 16` até essa funcionalidade
existir.

## Parâmetros principais do scanner

Os parâmetros abaixo ficam no início de `scanner.py`:

  ------------------------------------------------------------------------
  Parâmetro                              Valor atual Função
  --------------------- ---------------------------- ---------------------
  `MIN_VOLUME`                           `1_000_000` Volume mínimo em USDT
                                                     nas últimas 24 h

  `MAX_SPREAD`                                `0.50` Spread máximo
                                                     percentual

  `HISTORY_LIMIT`                              `500` Profundidade-alvo de
                                                     candles por
                                                     ativo/período

  `API_KLINE_LIMIT`                           `1000` Limite usado na
                                                     paginação da API

  `REQUEST_PAUSE`                           `0.15` s Pausa entre consultas

  `MAX_ASSETS`                                  `20` Máximo de ativos
                                                     analisados por
                                                     execução

  `INTERVALS`                      `15m, 1h, 4h, 1d` Períodos coletados
  ------------------------------------------------------------------------

Durante a validação, mantenha `MAX_ASSETS=20`. Aumente gradualmente
somente depois de observar duração, limites da API, qualidade dos dados
e acúmulo de resultados dos sinais.

## Interpretação dos sinais

-   `COMPRA_EM_OBSERVACAO`: condições técnicas alinhadas para
    acompanhamento de possível movimento de alta.
-   `VENDA_EM_OBSERVACAO`: alerta de condição técnica baixista. Em
    contexto Spot, significa atenção/possível redução de exposição ou
    evitar novas entradas; **não representa operação vendida (short)**.
-   `AGUARDAR`: critérios não convergiram para uma classificação
    direcional.
-   `ANALISE_PARCIAL_DAY` / `ANALISE_PARCIAL_SWING`: apenas parte dos
    períodos necessários produziu dados suficientes.
-   `DADOS_INSUFICIENTES`: dados insuficientes para análise.

Os sinais são classificações geradas por regras heurísticas. Não são
previsões garantidas. Antes de qualquer avaliação de desempenho,
considere taxas, spread, slippage, liquidez, drawdown, viés de seleção,
viés de sobrevivência e validação fora da amostra.

## Banco de dados

Arquivo padrão:

``` text
data/crypto_market.db
```

Tabelas principais:

  -----------------------------------------------------------------------
  Tabela                              Conteúdo
  ----------------------------------- -----------------------------------
  `candles`                           Candles OHLCV por ativo e timeframe

  `signals`                           Resultado histórico das análises

  `scanner_runs`                      Auditoria e estatísticas de cada
                                      execução

  `scanner_run_assets`                Ativos selecionados e resultado por
                                      execução

  `history_audit`                     Cobertura e lacunas observadas por
                                      ativo/timeframe

  `signal_outcomes`                   Acompanhamento dos sinais nos
                                      horizontes de 1, 4, 24 e 168 horas
  -----------------------------------------------------------------------

O banco é um artefato local e não deve ser enviado ao Git por padrão.
Faça backups separados.

## Integração com ThingsBoard

O publicador envia telemetria para:

``` text
POST {TB_URL}/api/v1/{TB_TOKEN}/telemetry
```

O payload é JSON e contém métricas agregadas e métricas individuais por
ativo. Consulte [`docs/THINGSBOARD.md`](docs/THINGSBOARD.md) para a
lista de chaves e orientação de configuração do dashboard.

No ThingsBoard, confira primeiro **Dispositivos → Crypto Quant Monitor →
Última telemetria**. Se as chaves aparecem ali, a publicação chegou ao
dispositivo; a visualização no dashboard depende da configuração dos
aliases, fontes de dados e widgets.

## Agendamento com systemd

Os modelos estão em `systemd/`.

Ajuste os caminhos e o usuário nos arquivos para corresponder ao local
real da instalação. Depois:

``` bash
sudo cp systemd/crypto-monitor.service /etc/systemd/system/
sudo cp systemd/crypto-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-monitor.timer
```

Verifique:

``` bash
systemctl status crypto-monitor.timer
systemctl list-timers crypto-monitor.timer
journalctl -u crypto-monitor.service -n 100 --no-pager
```

O serviço é `oneshot`; portanto, aparecer como `inactive (dead)` após
uma execução bem-sucedida é normal. O timer deve permanecer
`active (waiting)`.

## Logs

``` text
logs/scanner.log
logs/thingsboard_publisher.log
```

O `systemd` também registra a saída no journal:

``` bash
journalctl -u crypto-monitor.service -f
```

## Segurança e versionamento

Antes de fazer commit:

-   Não versionar `.thingsboard.env`, tokens, senhas ou credenciais.
-   Não versionar `.venv/`, banco SQLite, logs ou arquivos temporários.
-   Versionar `.thingsboard.env.example` apenas com valores fictícios.
-   Não publicar dados pessoais ou identificadores privados nos logs.
-   Revogar/rotacionar o token do dispositivo se ele for exposto.

O `.gitignore` deste pacote cobre os artefatos locais mais comuns.

## Limitações conhecidas da versão documentada

-   O publicador seleciona a execução mais recente
    (`ORDER BY id DESC LIMIT 1`).
-   O publicador atual não aceita `--run-id`.
-   `monitor_timestamp_utc` é uma métrica textual dentro do payload; o
    timestamp de ingestão do ThingsBoard é controlado pelo servidor
    quando o payload não contém timestamps históricos no formato
    específico da API.
-   `history_audit` mede a cobertura dentro do intervalo entre o
    primeiro e o último candle armazenado; não comprova que todo o
    histórico desde a listagem do ativo foi obtido.
-   `TARGET_REACHED` indica que o número-alvo de candles foi alcançado,
    não que um alvo de preço foi atingido.
-   Resultados em `signal_outcomes` permanecem `PENDING` até o horizonte
    vencer e existir candle posterior utilizável.

## Estrutura sugerida do repositório

``` text
crypto-quant-monitor/
├── scanner.py
├── thingsboard_publisher.py
├── requirements.txt
├── .gitignore
├── .thingsboard.env.example
├── README.md
├── data/                  # local, ignorado pelo Git
├── logs/                  # local, ignorado pelo Git
├── docs/
│   └── THINGSBOARD.md
└── systemd/
    ├── crypto-monitor.service
    └── crypto-monitor.timer
```
