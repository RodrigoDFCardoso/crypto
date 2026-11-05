import ccxt
from tradingview_ta import TA_Handler, Interval
import pandas as pd
import time

# Lista de símbolos de criptomoedas
symbols = ['LUNCUSDT', 'USTCUSDT', 'SHIBUSDT', 'ETHUSDT']

# Inicializar a API da Binance usando o ccxt
binance = ccxt.binance()

# Carregar o histórico de transações a partir do CSV
historico_csv = 'last_ordens-2024-01-12.csv'
historico_df = pd.read_csv(historico_csv)

# Loop infinito definido pelo time.sleep no final
while True:
    print('******* Nova Leitura *******')

    # Iterar sobre as transações no histórico
    for index, transacao in historico_df.iterrows():
        symbol = transacao['Pair']

        # Obter informações atuais do ticker
        ticker = binance.fetch_ticker(symbol)

        # Obter análise técnica usando a biblioteca tradingview_ta
        output = TA_Handler(symbol=symbol, screener='Crypto', exchange='Binance', interval=Interval.INTERVAL_5_MINUTES)

        # Imprimir informações sobre a criptomoeda
        print('Moeda: ' + symbol, end=' ')
        print("Preço Atual: " + str(ticker['last']))
        print("Resumo: ", end=' ')
        print(output.get_analysis().summary['RECOMMENDATION'])

        print("Indicadores RSI: ", end=' ')
        print(output.get_analysis().oscillators['RECOMMENDATION'])

        print("Média Móvel: ", end=' ')
        print(output.get_analysis().moving_averages['RECOMMENDATION'])

        # Verificar se é o momento certo para vender
        # if output.get_analysis().summary['RECOMMENDATION'] == 'SELL':
        if transacao['Pair']:
            print("Avaliar porcentagem de lucro...")

            # Calcular porcentagem de lucro
            order_price = transacao['Order Price']
            current_price = ticker['last']
            profit_percentage = ((current_price - order_price) / order_price) * 100

            print(f'Valor de compra: {order_price}')

            print(f"Porcentagem de Lucro: {profit_percentage:.2f}%")

        # Lógica adicional para execução de ordem de venda

        print()

    # Aguardar 1 minuto antes da próxima iteração
    time.sleep(60)  # 60 segundos é equivalente a 1 minutos
