# ThingsBoard CE 4.4.0 --- integração e dashboard

## 1. Pré-requisitos

1.  ThingsBoard CE 4.4.0 em execução.
2.  Dispositivo criado com o nome `Crypto Quant Monitor`.
3.  Token de acesso do dispositivo configurado em `.thingsboard.env`.
4.  `thingsboard_publisher.py` executado com HTTP `200` ou `204`.

Valide primeiro em **Dispositivos → Crypto Quant Monitor → Última
telemetria**. O publicador envia os dados para o endpoint de telemetria
do dispositivo.

## 2. Chaves de telemetria

### Execução e estado

  -----------------------------------------------------------------------------
  Chave                         Tipo esperado           Descrição
  ----------------------------- ----------------------- -----------------------
  `monitor_timestamp_utc`       string                  Horário UTC registrado
                                                        pelo publicador

  `scanner_run_id`              número                  ID da execução no
                                                        SQLite

  `scanner_status`              string                  Estado da execução

  `scanner_duration_seconds`    número                  Duração em segundos

  `assets_approved`             número                  Ativos aprovados nos
                                                        filtros de mercado

  `assets_selected`             número                  Ativos selecionados
                                                        para análise

  `analyses_completed`          número                  Análises completas

  `partial_analyses`            número                  Análises parciais

  `assets_failed`               número                  Ativos sem análise

  `candles_inserted_last_run`   número                  Candles inseridos na
                                                        execução

  `signals_saved_last_run`      número                  Registros gravados em
                                                        `signals`

  `candles_total`               número                  Total de candles no
                                                        banco
  -----------------------------------------------------------------------------

### Contagem de classificações

  -----------------------------------------------------------------------
  Chave                               Descrição
  ----------------------------------- -----------------------------------
  `buy_observation_count`             Quantidade de sinais
                                      `COMPRA_EM_OBSERVACAO`

  `sell_observation_count`            Quantidade de sinais
                                      `VENDA_EM_OBSERVACAO`

  `wait_count`                        Quantidade de sinais `AGUARDAR`

  `assets_without_signal_count`       Ativos sem sinal/classificação
                                      disponível
  -----------------------------------------------------------------------

### Histórico e qualidade

  -----------------------------------------------------------------------
  Chave                               Descrição
  ----------------------------------- -----------------------------------
  `history_backfilled_last_run`       Candles históricos anteriores
                                      recuperados

  `history_gaps_found_last_run`       Candles ausentes detectados

  `history_gaps_repaired_last_run`    Candles ausentes recuperados

  `history_gaps_pending_last_run`     Candles ainda ausentes após
                                      tentativa de reparo

  `history_audits_saved_last_run`     Auditorias gravadas

  `history_assets_{tf}`               Ativos auditados no timeframe

  `history_target_reached_{tf}`       Ativos que atingiram
                                      `HISTORY_LIMIT`

  `history_gaps_remaining_{tf}`       Ativos com lacunas restantes

  `history_avg_coverage_pct_{tf}`     Cobertura média no intervalo
                                      observado
  -----------------------------------------------------------------------

`{tf}` é substituído por `15m`, `1h`, `4h` ou `1d`.

### Acompanhamento de resultados

  -----------------------------------------------------------------------
  Chave                               Descrição
  ----------------------------------- -----------------------------------
  `outcomes_pending`                  Total de observações pendentes

  `outcomes_observed`                 Total de observações concluídas

  `outcomes_total_{h}h`               Total de observações no horizonte

  `outcomes_observed_{h}h`            Observações concluídas no horizonte

  `outcomes_avg_aligned_pct_{h}h`     Média do movimento alinhado ao
                                      sinal
  -----------------------------------------------------------------------

Horizontes atuais: `1h`, `4h`, `24h` e `168h`. Essas métricas só
aparecem quando existem registros correspondentes.

### Métricas individuais por ativo

Para cada ativo selecionado, o publicador gera:

  ------------------------------------------------------------------------------
  Padrão                      Exemplo                    Descrição
  --------------------------- -------------------------- -----------------------
  `signal_{SYMBOL}`           `signal_BTCUSDT`           Classificação do ativo

  `price_{SYMBOL}`            `price_BTCUSDT`            Preço de referência

  `volume_24h_{SYMBOL}`       `volume_24h_BTCUSDT`       Volume cotado em 24 h

  `spread_pct_{SYMBOL}`       `spread_pct_BTCUSDT`       Spread percentual

  `change_24h_pct_{SYMBOL}`   `change_24h_pct_BTCUSDT`   Variação percentual em
                                                         24 h
  ------------------------------------------------------------------------------

As chaves individuais são dinâmicas: dependem dos ativos selecionados
naquela execução.

## 3. Configuração do dashboard

O dashboard deve usar o dispositivo `Crypto Quant Monitor` como fonte de
dados. Uma abordagem híbrida é recomendada:

-   Widgets nativos de **Últimos valores** para KPIs e estado da
    execução.
-   Widgets de **Série temporal** para acompanhar métricas ao longo das
    execuções.
-   Widget HTML personalizado para a tabela dinâmica de ativos, caso se
    queira reproduzir o layout do Crypto Market Scanner.

Crie um alias de entidade do tipo **Dispositivo único** apontando para
`Crypto Quant Monitor`, por exemplo `Crypto Scanner`.

### KPIs sugeridos

Configure widgets de Últimos valores com estas chaves:

-   `assets_selected`
-   `buy_observation_count`
-   `sell_observation_count`
-   `wait_count`
-   `scanner_duration_seconds`
-   `candles_total`
-   `history_gaps_pending_last_run`

### Qualidade do histórico

Adicione KPIs ou gráficos para:

-   `history_avg_coverage_pct_15m`
-   `history_avg_coverage_pct_1h`
-   `history_avg_coverage_pct_4h`
-   `history_avg_coverage_pct_1d`
-   `history_gaps_remaining_15m`
-   `history_gaps_remaining_1h`
-   `history_gaps_remaining_4h`
-   `history_gaps_remaining_1d`

Lembre-se: cobertura é calculada somente entre o primeiro e o último
candle armazenado. Não equivale ao histórico completo desde a listagem
do ativo.

### HTML Card / widget personalizado

Ao configurar um widget HTML personalizado, adicione o alias/dispositivo
e as chaves necessárias na aba de fontes de dados/chaves de dados do
widget. O JavaScript deve consumir a estrutura fornecida pelo contexto
do widget na versão do ThingsBoard utilizada. Não presuma que
`self.ctx.data` contém uma lista simples de objetos por ativo: em geral,
os dados vêm organizados por datasource e data key, e precisam ser
mapeados pelas chaves configuradas.

Para evitar o erro de iteração observado durante a edição, valide o tipo
antes de percorrer a coleção:

``` javascript
const data = Array.isArray(self.ctx.data)
    ? self.ctx.data
    : [];
```

Essa verificação evita chamar métodos de array em um valor que não seja
array, mas não substitui a configuração correta dos data keys nem a
transformação dos pontos de telemetria em registros por símbolo.

## 4. Diagnóstico

### A telemetria aparece no dispositivo, mas o widget fica vazio

Verifique:

1.  O alias aponta para o dispositivo correto.
2.  O widget tem datasource configurado.
3.  As chaves estão adicionadas em **Chaves de dados** com o nome exato.
4.  O tipo do widget é compatível com a chave (número para KPI/gráfico;
    texto para status).
5.  O intervalo temporal do dashboard inclui o momento da publicação.
6.  O widget foi salvo e o dashboard também foi salvo.

### O publicador retorna HTTP 200, mas não há dados no dashboard

HTTP 200 confirma que o endpoint aceitou o payload. Não garante que o
dashboard tenha datasource, alias ou chaves configurados corretamente.

### Teste de publicação

``` bash
cd ~/crypto-quant-monitor
source .venv/bin/activate
python thingsboard_publisher.py
tail -n 30 logs/thingsboard_publisher.log
```

Não compartilhe o token do dispositivo ao solicitar suporte.
