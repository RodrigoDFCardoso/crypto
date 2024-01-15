import pandas as pd
import matplotlib.pyplot as plt

# Load CSV data into a DataFrame
df = pd.read_csv('binance_ordens-2024-01-12.csv')

# Display the first few rows of the DataFrame
print(df.head())

# Check data types and missing values
print(df.info())

# Convert 'Date(UTC)' column to datetime
df['Date(UTC)'] = pd.to_datetime(df['Date(UTC)'])

# Set 'Date(UTC)' as the index
df.set_index('Date(UTC)', inplace=True)

# Display the DataFrame with the updated index
print(df.head())

# Plot the trading prices
df['Order Price'].plot(label='Order Price')
df['AvgTrading Price'].plot(label='AvgTrading Price')
plt.title('Trading Prices Over Time')
plt.xlabel('Date')
plt.ylabel('Price')
plt.legend()
plt.show()

# Calculate and plot the total filled amount over time
df['Filled'].cumsum().plot(title='Cumulative Filled Amount Over Time')
plt.xlabel('Date')
plt.ylabel('Total Filled Amount')
plt.show()
