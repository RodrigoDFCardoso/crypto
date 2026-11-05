import ccxt
from tradingview_ta import TA_Handler, Interval
import time

# Lista de símbolos de criptomoedas
symbols = ['LUNCUSDT', 'USTCUSDT', 'SHIBUSDT', 'ETHUSDT']

# Inicializar a API da Binance usando o ccxt
binance = ccxt.binance()

# Loop infinito definido pelo time.sleep no final
while True:
    print('******* Nova Leitura *******')

    # Iterar sobre os símbolos
    for symbol in symbols:
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
        print()

    # Aguardar 1 minutos antes da próxima iteração
    time.sleep(60)  # 60 segundos é equivalente a 1 minutos

