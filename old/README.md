# Crypto Quant Monitor

## Automação e integração com ThingsBoard

Documentação da instalação e configuração do **Crypto Quant Monitor
2.4** na Raspberry Pi 4, incluindo execução automática com `systemd`,
armazenamento SQLite e publicação de telemetria no ThingsBoard.

  Item                   Configuração
  ---------------------- ----------------------------------
  Plataforma             Raspberry Pi 4
  Sistema operacional    Debian 13
  Projeto                Crypto Quant Monitor 2.4
  Fonte de dados         Binance Spot API pública
  Banco de dados         SQLite
  Monitoramento          ThingsBoard Community Edition
  Automação              `systemd` (`service` + `timer`)
  Diretório do projeto   `/home/pi4/crypto-quant-monitor`

------------------------------------------------------------------------

## 1. Arquitetura

O sistema executa periodicamente o scanner, persiste os dados no banco
SQLite e, somente após o término bem-sucedido da análise, publica as
métricas no ThingsBoard.

``` text
             BINANCE SPOT API
                    |
                    v
                scanner.py
                    |
          +---------+---------+
          |                   |
          v                   v
     Análise técnica     Histórico de candles
          |                   |
          +---------+---------+
                    |
                    v
              BANCO SQLITE
           crypto_market.db
                    |
                    v
       thingsboard_publisher.py
                    |
                    v
              HTTP POST / JSON
                    |
                    v
                THINGSBOARD
                    |
                    v
                 DASHBOARD
```

O `systemd` controla o horário e a ordem das tarefas. O processo não
depende de uma sessão SSH aberta.

## 2. Estrutura do projeto

Diretório:

``` bash
/home/pi4/crypto-quant-monitor
```

Estrutura principal:

``` text
crypto-quant-monitor/
├── scanner.py
├── thingsboard_publisher.py
├── data/
│   └── crypto_market.db
├── logs/
│   ├── scanner.log
│   └── thingsboard_publisher.log
├── .venv/
└── .thingsboard.env
```

  -----------------------------------------------------------------------
  Arquivo/diretório                   Responsabilidade
  ----------------------------------- -----------------------------------
  `scanner.py`                        Consulta a Binance, atualiza
                                      candles e executa as análises

  `thingsboard_publisher.py`          Consulta o SQLite e publica a
                                      telemetria

  `data/crypto_market.db`             Histórico de mercado, execuções,
                                      sinais e resultados

  `logs/`                             Registros de execução e publicação

  `.venv/`                            Ambiente virtual Python

  `.thingsboard.env`                  URL e token de acesso do
                                      ThingsBoard
  -----------------------------------------------------------------------

> Os nomes e caminhos acima correspondem à estrutura documentada durante
> a implementação. Confira os arquivos existentes na instalação antes de
> substituir configurações.

## 3. Preparação do ambiente Python

Acessar o projeto e ativar o ambiente virtual:

``` bash
cd ~/crypto-quant-monitor
source .venv/bin/activate
```

Dependências utilizadas pelo projeto:

``` bash
pip install requests pandas numpy python-dotenv
```

O scanner utiliza a API pública da Binance Spot. Para as consultas
implementadas, não é necessário configurar uma chave privada de API.

## 4. Configuração do ThingsBoard

### 4.1. Criar o dispositivo

Na interface do ThingsBoard:

1.  Acessar **Entities → Devices**.
2.  Criar um dispositivo chamado `Crypto Quant Monitor`.
3.  Abrir os detalhes do dispositivo.
4.  Consultar as credenciais do dispositivo.
5.  Utilizar autenticação por **Access Token**.
6.  Copiar o token gerado.

O token identifica o dispositivo que receberá a telemetria. Não o
publique em repositórios ou mensagens.

### 4.2. Criar o arquivo de configuração

No Raspberry Pi:

``` bash
nano ~/crypto-quant-monitor/.thingsboard.env
```

Conteúdo:

``` ini
TB_URL=http://192.168.18.34:8080
TB_TOKEN=SEU_ACCESS_TOKEN
```

Substitua `SEU_ACCESS_TOKEN` pelo token real do dispositivo.

Restringir as permissões do arquivo:

``` bash
chmod 600 ~/crypto-quant-monitor/.thingsboard.env
```

### 4.3. Endpoint de telemetria

A integração utiliza o endpoint HTTP de telemetria por token:

``` text
POST http://192.168.18.34:8080/api/v1/SEU_TOKEN/telemetry
```

Cabeçalho:

``` http
Content-Type: application/json
```

Exemplo de corpo JSON:

``` json
{
  "scanner_run_id": 6,
  "scanner_status": "COMPLETED",
  "assets_selected": 20,
  "analyses_completed": 19,
  "buy_observation_count": 3,
  "sell_observation_count": 7,
  "wait_count": 9
}
```

Várias chaves podem ser enviadas em uma única requisição. O publicador
instalado envia as métricas reunidas em um único POST.

## 5. Publicador de telemetria

O arquivo `thingsboard_publisher.py` é responsável por:

1.  Carregar a URL e o token do arquivo de ambiente.
2.  Consultar o banco SQLite.
3.  Obter os dados da execução mais recente.
4.  Montar as métricas gerais e os dados individuais dos ativos.
5.  Serializar os dados em JSON.
6.  Enviar um HTTP POST ao ThingsBoard.
7.  Registrar o resultado da requisição no log.

O publicador consulta o banco sem modificar os registros históricos. A
resposta HTTP 200 ou 204 indica que o ThingsBoard aceitou a requisição.

### 5.1. Grupos de métricas

As métricas publicadas incluem:

**Execução do scanner**

-   `scanner_run_id`
-   `scanner_status`
-   `scanner_duration_seconds`
-   `assets_selected`
-   `assets_approved`
-   `assets_failed`
-   `analyses_completed`
-   `partial_analyses`

**Distribuição dos sinais**

-   `buy_observation_count`
-   `sell_observation_count`
-   `wait_count`
-   `signals_saved_last_run`

**Histórico de mercado**

-   `candles_total`
-   `candles_inserted_last_run`
-   `history_backfilled_last_run`
-   `history_audits_saved_last_run`
-   `history_gaps_found_last_run`
-   `history_gaps_repaired_last_run`
-   `history_gaps_pending_last_run`

**Qualidade do histórico**

-   `history_avg_coverage_pct_15m`
-   `history_avg_coverage_pct_1h`
-   `history_avg_coverage_pct_4h`
-   `history_avg_coverage_pct_1d`

**Resultados dos sinais**

-   `outcomes_pending`
-   `outcomes_updated_last_run`
-   `outcomes_observed_1h`
-   `outcomes_observed_4h`
-   `outcomes_observed_24h`
-   `outcomes_observed_168h`

**Dados individuais dos ativos**

Para cada símbolo, são publicados campos no padrão:

``` text
price_BTCUSDT
signal_BTCUSDT
change_24h_pct_BTCUSDT
spread_pct_BTCUSDT
volume_24h_BTCUSDT
```

O mesmo padrão é utilizado para os demais ativos selecionados. A
execução validada publicou **147 métricas**.

## 6. Teste manual da integração

Antes da automação, a comunicação pode ser testada diretamente com
`curl`.

### 6.1. Teste simples

``` bash
curl -v -X POST \
  "http://192.168.18.34:8080/api/v1/SEU_TOKEN/telemetry" \
  -H "Content-Type: application/json" \
  -d '{
    "temperature": 25,
    "scanner_run_id": 6,
    "scanner_status": "COMPLETED"
  }'
```

### 6.2. Teste com arquivo JSON

Criar um arquivo de teste:

``` bash
nano test_telemetry.json
```

Exemplo:

``` json
{
  "scanner_run_id": 6,
  "scanner_status": "COMPLETED",
  "analyses_completed": 19,
  "assets_approved": 96,
  "assets_selected": 20,
  "assets_failed": 0,
  "buy_observation_count": 3,
  "sell_observation_count": 7,
  "wait_count": 9,
  "scanner_duration_seconds": 56.95,
  "candles_total": 46623
}
```

Enviar:

``` bash
curl -v -X POST \
  "http://192.168.18.34:8080/api/v1/SEU_TOKEN/telemetry" \
  -H "Content-Type: application/json" \
  --data-binary @test_telemetry.json
```

**Resultado esperado:** HTTP 200 ou HTTP 204.

> Os dados de teste são valores estáticos. Eles servem para validar a
> recepção e a construção dos widgets, não representam uma nova execução
> real do scanner.

## 7. Automação com systemd

A automação utiliza duas unidades:

-   `crypto-monitor.service`: executa o scanner e o publicador em
    sequência.
-   `crypto-monitor.timer`: agenda a execução horária.

### 7.1. Serviço `crypto-monitor.service`

Arquivo:

``` text
/etc/systemd/system/crypto-monitor.service
```

A estrutura funcional utilizada é:

``` ini
[Unit]
Description=Crypto Quant Monitor - scanner e publicação ThingsBoard
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=pi4
WorkingDirectory=/home/pi4/crypto-quant-monitor
EnvironmentFile=/home/pi4/crypto-quant-monitor/.thingsboard.env

ExecStart=/home/pi4/crypto-quant-monitor/.venv/bin/python /home/pi4/crypto-quant-monitor/scanner.py

ExecStartPost=/home/pi4/crypto-quant-monitor/.venv/bin/python /home/pi4/crypto-quant-monitor/thingsboard_publisher.py

[Install]
WantedBy=multi-user.target
```

**Atenção:** consulte a unidade efetivamente instalada antes de editar
ou substituir o arquivo:

``` bash
sudo systemctl cat crypto-monitor.service
```

O comportamento esperado é:

1.  O systemd inicia `scanner.py`.
2.  O scanner consulta a Binance e atualiza o SQLite.
3.  Se o scanner terminar com sucesso, o `ExecStartPost` executa o
    publicador.
4.  O publicador envia a telemetria ao ThingsBoard.
5.  O serviço termina e aguarda a próxima ativação.

Como o serviço utiliza `Type=oneshot`, é normal que apareça como
`inactive (dead)` depois de uma execução bem-sucedida.

### 7.2. Timer `crypto-monitor.timer`

Arquivo:

``` text
/etc/systemd/system/crypto-monitor.timer
```

Configuração:

``` ini
[Unit]
Description=Executa Crypto Quant Monitor a cada hora

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=30
Unit=crypto-monitor.service

[Install]
WantedBy=timers.target
```

  -----------------------------------------------------------------------
  Parâmetro                           Função
  ----------------------------------- -----------------------------------
  `OnCalendar=hourly`                 Agenda uma execução por hora

  `Persistent=true`                   Permite recuperar uma execução
                                      perdida enquanto o sistema estava
                                      desligado

  `RandomizedDelaySec=30`             Adiciona um pequeno atraso
                                      aleatório

  `Unit=crypto-monitor.service`       Define o serviço que será iniciado
  -----------------------------------------------------------------------

### 7.3. Habilitar a automação

Após criar ou modificar os arquivos:

``` bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-monitor.timer
```

Verificar o timer:

``` bash
sudo systemctl status crypto-monitor.timer
```

Verificar se está habilitado:

``` bash
systemctl is-enabled crypto-monitor.timer
```

Consultar a próxima execução:

``` bash
systemctl list-timers --all | grep crypto-monitor
```

Estado esperado:

``` text
Active: active (waiting)
```

A indicação `active (waiting)` significa que o timer está ativo e
aguardando o próximo horário.

## 8. Executar e acompanhar manualmente

Mesmo com o timer habilitado, é possível iniciar o serviço manualmente:

``` bash
sudo systemctl start crypto-monitor.service
```

Acompanhar a execução em tempo real:

``` bash
sudo journalctl -u crypto-monitor.service -f
```

O acompanhamento pode ser encerrado com `Ctrl + C`. Isso encerra apenas
a visualização do log, não o serviço.

O fluxo esperado no log é semelhante a:

``` text
Iniciando analise Binance Spot...
...
Execucao finalizada.
POST enviado: HTTP 200 | run_id=6 | 147 métricas
Finished crypto-monitor.service
```

## 9. Monitoramento dos logs

Consultar as últimas 100 linhas do serviço:

``` bash
sudo journalctl -u crypto-monitor.service -n 100 --no-pager
```

Consultar as execuções do dia:

``` bash
sudo journalctl -u crypto-monitor.service --since today --no-pager
```

Consultar o log do publicador:

``` bash
tail -n 20 ~/crypto-quant-monitor/logs/thingsboard_publisher.log
```

Acompanhar o log do publicador continuamente:

``` bash
tail -f ~/crypto-quant-monitor/logs/thingsboard_publisher.log
```

## 10. Verificar o banco SQLite

As execuções são registradas na tabela `scanner_runs`.

Para consultar as últimas cinco execuções:

``` bash
cd ~/crypto-quant-monitor

python -c "
import sqlite3
c = sqlite3.connect('data/crypto_market.db')
print(*c.execute('''
SELECT id, started_at, finished_at,
duration_seconds, status,
assets_selected, analyses_completed,
signals_saved
FROM scanner_runs
ORDER BY id DESC LIMIT 5
'''), sep='\\n')
c.close()
"
```

Essa consulta permite comparar os resultados do scanner com as métricas
publicadas no ThingsBoard.

## 11. Verificar a telemetria no ThingsBoard

Acessar a interface web:

``` text
http://192.168.18.34:8080
```

Navegar até:

``` text
Entities
  └── Devices
      └── Crypto Quant Monitor
          └── Latest telemetry
```

As chaves recebidas devem aparecer na lista de telemetria. Exemplos:

``` text
scanner_run_id
scanner_status
analyses_completed
assets_selected
buy_observation_count
sell_observation_count
wait_count
candles_total
price_BTCUSDT
signal_BTCUSDT
history_avg_coverage_pct_15m
```

## 12. Estrutura sugerida para o dashboard

Com a telemetria disponível, o dashboard pode ser organizado em quatro
áreas.

### 12.1. Visão geral

Indicadores:

-   Ativos aprovados.
-   Ativos selecionados.
-   Análises concluídas.
-   Tempo de execução.
-   Sinais registrados.

### 12.2. Distribuição dos sinais

Gráfico de pizza ou barras com:

-   Compra em observação.
-   Venda em observação.
-   Aguardar.

### 12.3. Monitoramento dos ativos

Tabela com:

-   Símbolo.
-   Preço.
-   Variação percentual em 24 horas.
-   Volume negociado.
-   Spread.
-   Sinal identificado.

### 12.4. Qualidade e desempenho

Indicadores de qualidade:

-   Cobertura histórica por timeframe.
-   Candles armazenados.
-   Gaps encontrados e pendentes.
-   Histórico recuperado.

Indicadores de desempenho:

-   Resultados observados após 1 hora.
-   Resultados observados após 4 horas.
-   Resultados observados após 24 horas.
-   Resultados observados após 168 horas.

Os resultados futuros dos sinais dependem do tempo decorrido desde a
geração de cada sinal. Valores pendentes não devem ser interpretados
como falhas.

## 13. Problemas encontrados e diagnóstico

### 13.1. Serviço aparece como `inactive (dead)`

Esse estado é esperado para um serviço `Type=oneshot` após a conclusão.
O timer permanece ativo e inicia o serviço no próximo horário.

Verificar o timer:

``` bash
sudo systemctl status crypto-monitor.timer
```

### 13.2. Falha de conexão com a Binance

Durante uma execução foram observados erros como:

``` text
Read timed out
```

e:

``` text
Temporary failure in name resolution
```

Os testes posteriores confirmaram que a conexão voltou a funcionar:

``` text
HTTP: 200
```

A consulta ao endpoint `exchangeInfo` apresentou tempos de resposta
entre aproximadamente 6,9 e 14,2 segundos. Como o scanner utilizava
timeout de leitura de 5 segundos, a latência variável era suficiente
para provocar falhas.

**Melhoria recomendada:** aumentar o timeout de leitura da API e avaliar
retentativas com espera progressiva.

Testes de conectividade:

``` bash
getent ahosts data-api.binance.vision
getent ahosts google.com
```

Teste do endpoint:

``` bash
curl -sS -o /dev/null \
  --connect-timeout 5 \
  --max-time 20 \
  -w 'HTTP: %{http_code} | Tempo: %{time_total}s\n' \
  https://data-api.binance.vision/api/v3/exchangeInfo
```

### 13.3. Falha do scanner e publicação

O publicador é executado após o scanner. Se o scanner falhar, o envio
posterior não deve ser tratado como uma nova análise concluída.

Isso evita confundir telemetria antiga com resultados atuais.

**Melhoria futura:** implementar recuperação automática de falhas
temporárias no systemd, com intervalo de espera e limite de tentativas.
Essa recuperação não deve provocar publicações duplicadas nem
sobrescrever resultados atuais com dados antigos.

## 14. Resultado validado

Durante os testes da integração, uma execução completa apresentou:

``` text
CRYPTO QUANT MONITOR 2.4

Ativos selecionados: 20
Análises completas: 19
Análises parciais: 1
Sinais registrados: 20
Auditorias históricas: 80
Gaps pendentes: 0
```

Resultado da publicação:

``` text
POST enviado: HTTP 200
run_id=6
147 métricas
http://192.168.18.34:8080
```

O systemd confirmou a conclusão:

``` text
crypto-monitor.service: Deactivated successfully.
Finished crypto-monitor.service
```

O timer também foi verificado como habilitado e ativo:

``` text
Active: active (waiting)
Loaded: enabled
```

## 15. Próximas melhorias

1.  Aumentar a tolerância a falhas de conexão com a Binance.
2.  Implementar recuperação automática de execuções malsucedidas.
3.  Desenvolver o dashboard no ThingsBoard.
4.  Acompanhar o desempenho histórico dos sinais.
5.  Desenvolver indicadores estatísticos para avaliar a estratégia.
6.  Implementar alertas para falhas de execução e problemas de qualidade
    dos dados.

## 16. Considerações finais

A implementação transforma a Raspberry Pi 4 em uma plataforma autônoma
de coleta, análise quantitativa e monitoramento de mercado.

As responsabilidades estão separadas:

-   **Scanner:** coleta e análise dos dados.
-   **SQLite:** armazenamento local e histórico.
-   **Publisher:** integração e envio de telemetria.
-   **systemd:** gerenciamento e agendamento das execuções.
-   **ThingsBoard:** recepção, visualização e monitoramento.

O sistema utiliza dados públicos da Binance Spot e não executa ordens de
compra ou venda. Os sinais são indicadores experimentais para análise e
avaliação histórica, não recomendações financeiras.
