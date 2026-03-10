#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取模块 - 独立实现，不依赖 instock 模块
从东方财富获取A股实时行情和历史K线数据
"""
import time
import random
import math
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("BBBIG")


class StockDataFetcher:
    """A股数据获取器"""

    def __init__(self):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self._code_id_map = None

    def _get_code_id_map(self) -> dict:
        """获取股票代码与市场ID映射"""
        if self._code_id_map is not None:
            return self._code_id_map

        code_id_dict = {}
        # 上证
        for fs, market_id in [("m:1 t:2,m:1 t:23", 1), ("m:0 t:6,m:0 t:80", 0), ("m:0 t:81 s:2048", 0)]:
            page_size = 50
            page_current = 1
            url = "http://80.push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": page_current, "pz": page_size, "po": "1", "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
                "fid": "f12", "fs": fs, "fields": "f12", "_": "1623833739532",
            }
            try:
                r = self.session.get(url, params=params, timeout=15)
                data_json = r.json()
                data = data_json.get("data", {}).get("diff", [])
                if not data:
                    continue
                data_count = data_json["data"]["total"]
                page_count = math.ceil(data_count / page_size)
                while page_count > 1:
                    time.sleep(random.uniform(0.3, 0.8))
                    page_current += 1
                    params["pn"] = page_current
                    r = self.session.get(url, params=params, timeout=15)
                    _data = r.json().get("data", {}).get("diff", [])
                    if _data:
                        data.extend(_data)
                    page_count -= 1
                for item in data:
                    code_id_dict[item["f12"]] = market_id
            except Exception as e:
                logger.warning(f"获取股票代码映射异常: {e}")

        self._code_id_map = code_id_dict
        return code_id_dict

    @staticmethod
    def is_a_stock(code: str) -> bool:
        """判断是否为A股"""
        return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003', '300', '301'))

    def fetch_all_stocks(self) -> pd.DataFrame:
        """获取全部A股实时行情"""
        url = "http://82.push2.eastmoney.com/api/qt/clist/get"
        page_size = 50
        page_current = 1
        params = {
            "pn": page_current, "pz": page_size, "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f12",
            "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
            "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f100,f112,f113",
            "_": "1623833739532",
        }
        all_data = []
        try:
            r = self.session.get(url, params=params, timeout=15)
            data_json = r.json()
            data = data_json["data"]["diff"]
            if not data:
                return pd.DataFrame()
            all_data.extend(data)
            data_count = data_json["data"]["total"]
            page_count = math.ceil(data_count / page_size)
            while page_count > 1:
                time.sleep(random.uniform(0.5, 1.0))
                page_current += 1
                params["pn"] = page_current
                r = self.session.get(url, params=params, timeout=15)
                _data = r.json()["data"]["diff"]
                all_data.extend(_data)
                page_count -= 1
        except Exception as e:
            logger.error(f"获取全部A股行情异常: {e}")
            return pd.DataFrame()

        df = pd.DataFrame(all_data)
        df.columns = [
            "最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅", "换手率",
            "市盈率动", "量比", "代码", "名称", "最高", "最低", "今开", "昨收",
            "总市值", "流通市值", "市净率", "60日涨跌幅", "年初至今涨跌幅",
            "所处行业", "每股收益", "每股净资产"
        ]
        # 类型转换
        numeric_cols = ["最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅",
                        "换手率", "市盈率动", "量比", "最高", "最低", "今开", "昨收",
                        "总市值", "流通市值", "市净率", "60日涨跌幅", "年初至今涨跌幅",
                        "每股收益", "每股净资产"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 过滤A股 + 有价格的
        df = df[df["代码"].apply(self.is_a_stock)]
        df = df[df["最新价"].notna() & (df["最新价"] > 0)]
        # 过滤ST
        df = df[~df["名称"].str.contains("ST", na=False)]
        df = df.reset_index(drop=True)
        return df

    def fetch_stock_kline(self, code: str, days: int = 30, adjust: str = "qfq",
                          end_date_str: str = None) -> pd.DataFrame:
        """
        获取个股日K线数据
        :param code: 股票代码
        :param days: 获取天数
        :param adjust: qfq-前复权, hfq-后复权, 空-不复权
        :param end_date_str: 结束日期，格式 YYYYMMDD，默认为当天
        """
        code_id_dict = self._get_code_id_map()
        if code not in code_id_dict:
            logger.warning(f"未找到股票代码: {code}")
            return pd.DataFrame()

        adjust_dict = {"qfq": "1", "hfq": "2", "": "0"}
        if end_date_str:
            ref_date = datetime.strptime(end_date_str, "%Y%m%d")
        else:
            ref_date = datetime.now()
        start_date = (ref_date - timedelta(days=days + 15)).strftime("%Y%m%d")
        end_date = ref_date.strftime("%Y%m%d")

        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": adjust_dict.get(adjust, "1"),
            "secid": f"{code_id_dict[code]}.{code}",
            "beg": start_date,
            "end": end_date,
            "_": "1623766962675",
        }
        try:
            r = self.session.get(url, params=params, timeout=15)
            data_json = r.json()
            if not (data_json.get("data") and data_json["data"].get("klines")):
                return pd.DataFrame()

            df = pd.DataFrame([item.split(",") for item in data_json["data"]["klines"]])
            df.columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
                          "振幅", "涨跌幅", "涨跌额", "换手率"]
            for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            # 只取最近 days 个交易日
            df = df.tail(days).reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"获取{code}K线异常: {e}")
            return pd.DataFrame()

    def fetch_hot_sectors(self) -> pd.DataFrame:
        """获取行业板块资金流向（用于热点分析）"""
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90 t:2",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
            "_": "1623833739532",
        }
        try:
            r = self.session.get(url, params=params, timeout=15)
            data_json = r.json()
            data = data_json.get("data", {}).get("diff", [])
            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df.columns = [
                "板块代码", "板块名称", "最新价", "涨跌幅", "主力净流入",
                "主力净流入占比", "超大单净流入", "超大单净流入占比",
                "大单净流入", "大单净流入占比", "中单净流入", "中单净流入占比",
                "小单净流入", "小单净流入占比", "主力净流入最大股", "主力净流入最大股代码", "_"
            ]
            numeric_cols = ["最新价", "涨跌幅", "主力净流入", "主力净流入占比"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("主力净流入", ascending=False).head(20)
            return df[["板块名称", "涨跌幅", "主力净流入", "主力净流入占比"]].reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取行业板块资金流异常: {e}")
            return pd.DataFrame()

    def fetch_concept_sectors(self) -> pd.DataFrame:
        """获取概念板块资金流向（用于热点分析）"""
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90 t:3",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
            "_": "1623833739532",
        }
        try:
            r = self.session.get(url, params=params, timeout=15)
            data_json = r.json()
            data = data_json.get("data", {}).get("diff", [])
            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df.columns = [
                "板块代码", "板块名称", "最新价", "涨跌幅", "主力净流入",
                "主力净流入占比", "超大单净流入", "超大单净流入占比",
                "大单净流入", "大单净流入占比", "中单净流入", "中单净流入占比",
                "小单净流入", "小单净流入占比", "主力净流入最大股", "主力净流入最大股代码", "_"
            ]
            numeric_cols = ["最新价", "涨跌幅", "主力净流入", "主力净流入占比"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("主力净流入", ascending=False).head(20)
            return df[["板块名称", "涨跌幅", "主力净流入", "主力净流入占比"]].reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取概念板块资金流异常: {e}")
            return pd.DataFrame()


# 全局实例
fetcher = StockDataFetcher()


if __name__ == "__main__":
    print("=== 获取A股实时行情 ===")
    stocks = fetcher.fetch_all_stocks()
    print(f"共获取 {len(stocks)} 只A股")
    print(stocks.head())

    print("\n=== 获取平安银行K线 ===")
    kline = fetcher.fetch_stock_kline("000001", days=10)
    print(kline)

    print("\n=== 行业板块资金流向 ===")
    sectors = fetcher.fetch_hot_sectors()
    print(sectors)
