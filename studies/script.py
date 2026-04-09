# Configure environment
# uv pip install mt5linux pandas matplotlib
# docker run --rm -p 6081:6081 -p 18812:18812 mt5linux:mt5-installed


from mt5linux import MetaTrader5
import pandas as pd
from datetime import datetime, timedelta
import os

SYMBOL = os.environ["SYMBOL"]
LOGIN = int(os.environ["LOGIN"])
PASSWORD = os.environ["PASSWORD"]

mt5 = MetaTrader5()
success = mt5.initialize(
    server="ClearInvestimentos-CLEAR",
    login=LOGIN,
    password=PASSWORD,
)

if not success:
    print("Erro de login")
    exit(1)


# tz = -3h
# start_time = datetime.now() - timedelta(hours=3, minutes=1)
# end_time = datetime.now()
tz = timedelta(hours=-3)
today = datetime.today()
today = datetime(today.year, today.month, today.day)
if "DATE" in os.environ:
    try:
        year, month, day = map(int, os.environ["DATE"].split("/"))
        today = datetime(year, month, day)
    except:
        pass
start_time = today + timedelta(hours=10) + tz
end_time = today + timedelta(hours=12) + tz
print("Fetching data...")
df = pd.DataFrame(
    mt5.copy_ticks_range(SYMBOL, start_time, end_time, mt5.COPY_TICKS_TRADE)
)
print("Length:", df.shape[0])
df.index = pd.to_datetime(df["time_msc"], unit="ms")
df["buy"] = df["flags"] & mt5.TICK_FLAG_BUY > 0
df.drop(["time", "time_msc", "flags", "volume_real"], inplace=True, axis=1)

df.to_csv(SYMBOL + ".csv", sep="\t")

print(df.describe())
