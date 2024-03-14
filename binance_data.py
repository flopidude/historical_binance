import asyncio
import httpx
import tqdm
import zipfile
from io import BytesIO
import numpy as np
from datetime import datetime, timedelta, date
import polars as pl
from dateutil.relativedelta import relativedelta
import json
import os


class BinanceDataDownloader:
    downloadable_ticker_information = {}
    pbar = None
    use_pbar = True
    __minimum_achieved_date = None

    async def ensure_tickers(self, pairlist: [str], timeframes: [str], fallback_starting_date=None):
        tickers_dir = os.path.realpath("./tickers")
        if not os.path.exists(tickers_dir):
            os.makedirs(tickers_dir)
        if fallback_starting_date is None:
            fallback_starting_date = date.today() - relativedelta(years=2)
        for pair in pairlist:
            ticker = pair.replace("/USDT:USDT", "USDT")
            if ticker not in self.downloadable_ticker_information["symbolList"]:
                raise Exception(f"Ticker {ticker} is not downloadable")
            for timeframe in timeframes:
                ticker = pair.replace("/USDT:USDT", "USDT")
                ticker_path = os.path.join(tickers_dir, f"{ticker}-{timeframe}.csv")
                # Load existing data
                try:
                    existing_data = pl.read_csv(ticker_path, try_parse_dates=True).with_columns(pl.col("date").cast(pl.Datetime(time_unit="ms", time_zone="UTC")))
                    print(existing_data)
                except Exception as e:
                    existing_data = None
                    print(f"Error loading existing data for {ticker}: {e}")

                # Get the last available date
                if existing_data is not None and not existing_data.is_empty():
                    last_date = existing_data['date'].max().date()
                else:
                    last_date = fallback_starting_date

                # Download new data and merge with existing data
                new_data = await self.download_one_ticker(ticker, last_date, date.today(), timeframe)
                if new_data is not None and not new_data.is_empty():
                    if existing_data is not None and not existing_data.is_empty():
                        updated_data = pl.concat([existing_data, new_data], how="vertical").unique(subset=["date"])
                    else:
                        updated_data = new_data
                    updated_data.write_csv(ticker_path)
                    print(f"Updated data for {ticker}")
                else:
                    print(f"No new data available for {ticker}")


    async def download_and_process(self, session, url: str, ticker: str, date_of_cycle: datetime):
        DEFAULT_COLUMNS = ['open_time', 'open', 'high', 'low', 'close', 'volume',
                                                          'close_time', 'quote_volume', 'count', 'taker_buy_volume',
                                                          'taker_buy_quote_volume', 'ignore']
        try:
            response = await session.get(url)
            if response.status_code == 200:
                data = response.read()
                with zipfile.ZipFile(BytesIO(data)) as zip_file:
                    with zip_file.open(zip_file.namelist()[0]) as csv_file:
                        first_line = csv_file.readline().decode("utf-8").strip()
                        csv_file.seek(0)  # Reset the file pointer to the beginning

                        if "open_time" in first_line:
                            df = pl.read_csv(csv_file.read())
                        else:
                            df = pl.read_csv(csv_file.read(), has_header=False, new_columns=DEFAULT_COLUMNS)
                        df = df.filter(pl.col("ignore") == 0).rename({"open_time": "date"}).drop(["close_time", "ignore", "quote_volume", "taker_buy_quote_volume"]).with_columns((pl.col("date").cast(pl.Datetime(time_unit="ms")).dt.replace_time_zone("UTC").alias("date")))
                        if self.pbar is not None:
                            self.pbar.update(1)
                        if self.__minimum_achieved_date is None or date_of_cycle < self.__minimum_achieved_date:
                            self.__minimum_achieved_date = date_of_cycle
                        return df
            else:
                raise ConnectionError(response.status_code)
        except Exception as e:

            if self.__minimum_achieved_date is not None and (not date.today() == date_of_cycle) and self.__minimum_achieved_date < date_of_cycle:
                print(date.today(), date_of_cycle)
                raise Exception(f"A hole in the cycle has been found, minimum achieved date is {self.__minimum_achieved_date}, url: {url.split('/klines')[1]}, {e}")
            if self.pbar is not None:
                self.pbar.write(f"Error: {url.split('/klines')[1]}, {e}")
                self.pbar.update(1)
                if not hasattr(self.pbar, "error_count"):
                    self.pbar.error_count = 1
                else:
                    self.pbar.error_count += 1
                self.pbar.set_postfix_str(f"ErrCount: {self.pbar.error_count}")
                # self.pbar.
            else:
                print(f"Error: Failed to download data for {ticker} from {url}")
            return pl.DataFrame()

    async def download_one_ticker(self, ticker, start_date, end_date, timeframe):
        self.__minimum_achieved_date = None
        if ticker not in self.downloadable_ticker_information["symbolList"]:
            raise Exception(f"Ticker {ticker} is not downloadable")
        async with httpx.AsyncClient() as session:
            tasks = []
            current_date = start_date
            end_date_plus_one = end_date + timedelta(days=1)
            while current_date < end_date_plus_one:
                year, month, day = current_date.year, current_date.month, current_date.day

                if current_date.month == end_date.month and current_date.year == end_date.year:
                    # Download daily data for the ending month
                    url = f"https://data.binance.vision/data/futures/um/daily/klines/{ticker}/{timeframe}/{ticker}-{timeframe}-{year}-{month:02d}-{day:02d}.zip"
                    task = asyncio.create_task(self.download_and_process(session, url, ticker, current_date))
                    tasks.append(task)
                    current_date += timedelta(days=1)

                else:
                    # Download monthly data for full months
                    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{ticker}/{timeframe}/{ticker}-{timeframe}-{year}-{month:02d}.zip"
                    task = asyncio.create_task(self.download_and_process(session, url, ticker, current_date))
                    tasks.append(task)
                    # add one month to the current date setting the day to be the first day of the month
                    current_date = current_date.replace(day=1) + relativedelta(months=1)
            if self.use_pbar:
                self.pbar = tqdm.tqdm(total=len(tasks), desc=f'Downloading {ticker}')

            dfs = await asyncio.gather(*tasks)
            combined_df = pl.concat([i for i in dfs if i.height > 0], how="vertical")
            if combined_df.shape[0] == 0:
                raise Exception(f"No data found for {ticker} between {start_date} and {end_date}")
            if self.use_pbar:
                self.pbar.close()
            return combined_df

    async def __fetch_downloadable_tickers(self):
        async with httpx.AsyncClient() as session:
            headers = {
                'authority': 'www.binance.com',
                'accept': '*/*',
                'accept-language': 'en-US,en;q=0.9',
                'clienttype': 'web',
                'content-type': 'application/json',
                'dnt': '1',
                'lang': 'en',
                'origin': 'https://www.binance.com',
                'referer': 'https://www.binance.com/en/landing/data',
                'sec-ch-ua': '"Not(A:Brand";v="24", "Chromium";v="122"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"macOS"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            }
            data = {'bizType': 'FUTURES_UM', 'productId': 1}
            response = await session.post(
                'https://www.binance.com/bapi/bigdata/v1/public/bigdata/finance/exchange/listDownloadOptions',
                headers=headers, json=data)
            result = response.json()
            if result["code"] != '000000' or not result["success"]:
                raise Exception(f"Failed to fetch downloadable tickers, {result}")
            return result["data"]

    def __init__(self, use_pbar=True):
        self.use_pbar = use_pbar
        self.downloadable_ticker_information = (asyncio.run(self.__fetch_downloadable_tickers()))
        pass

# a = BinanceDataDownloader()
# b = asyncio.run(a.download_one_ticker("1000BONKUSDT", date(2021, 2, 1), date(2024, 3, 14), "5m"))
# print(b)
