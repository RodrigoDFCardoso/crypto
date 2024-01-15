import pandas as pd
import matplotlib.pyplot as plt

# Load CSV data into a DataFrame
df = pd.read_csv('binance_ordens-2024-01-12.csv')

# Convert 'Date(UTC)' column to datetime
df['Date(UTC)'] = pd.to_datetime(df['Date(UTC)'])

# Set 'Date(UTC)' as the index
df.set_index('Date(UTC)', inplace=True)

# Group the DataFrame by 'Pair'
grouped_data = df.groupby('Pair')

# Iterate over each group and perform analysis
for pair, data in grouped_data:
    print(f"\nAnalyzing data for pair: {pair}")

    # Display the first few rows of the group
    print(data.head())

    # Plot the trading prices for each pair
    data['Order Price'].plot(label='Order Price')
    data['AvgTrading Price'].plot(label='AvgTrading Price')
    plt.title(f'Trading Prices Over Time for {pair}')
    plt.xlabel('Date')
    plt.ylabel('Price')
    plt.legend()
    plt.show()

    # Calculate and plot the total filled amount over time for each pair
    data['Filled'].cumsum().plot(title=f'Cumulative Filled Amount Over Time for {pair}')
    plt.xlabel('Date')
    plt.ylabel('Total Filled Amount')
    plt.show()
