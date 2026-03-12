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
                          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://quote.eastmoney.com/",
            "Origin": "https://quote.eastmoney.com",
            "Connection": "keep-alive",
        })
        self._code_id_map = None

    def _get_code_id_map(self) -> dict:
        """获取股票代码与市场ID映射（静态规则，无需网络请求）"""
        if self._code_id_map is not None:
            return self._code_id_map

        code_id_dict = {}
        # 上证：600xxx 601xxx 603xxx 605xxx → market_id=1
        for prefix in ["600", "601", "603", "605"]:
            for i in range(1000):
                code_id_dict[f"{prefix}{i:03d}"] = 1
        # 深证/创业板：000xxx 001xxx 002xxx 003xxx 300xxx 301xxx → market_id=0
        for prefix in ["000", "001", "002", "003", "300", "301"]:
            for i in range(1000):
                code_id_dict[f"{prefix}{i:03d}"] = 0

        self._code_id_map = code_id_dict
        return code_id_dict

    @staticmethod
    def is_a_stock(code: str) -> bool:
        """判断是否为A股"""
        return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003', '300', '301'))

    def _get_all_stock_codes(self) -> list:
        """通过腾讯行情获取全量A股代码列表（备用：静态规则生成）"""
        codes = []
        # 上证：600xxx 601xxx 603xxx 605xxx
        for prefix in ['600', '601', '603', '605']:
            for i in range(1000):
                codes.append(f"{prefix}{i:03d}")
        # 深证：000xxx 001xxx 002xxx 003xxx
        for prefix in ['000', '001', '002', '003']:
            for i in range(1000):
                codes.append(f"{prefix}{i:03d}")
        # 创业板：300xxx 301xxx
        for prefix in ['300', '301']:
            for i in range(1000):
                codes.append(f"{prefix}{i:03d}")
        return codes

    def fetch_all_stocks(self) -> pd.DataFrame:
        """获取全部A股实时行情（使用 ulist.np 接口，规避 clist 封禁）"""
        # 生成全量候选代码
        all_codes = self._get_all_stock_codes()

        # 构建 secid 列表：上证1.xxxx，深证/创业板0.xxxx
        def code_to_secid(code):
            if code.startswith(('600', '601', '603', '605')):
                return f"1.{code}"
            return f"0.{code}"

        secids = [code_to_secid(c) for c in all_codes]

        # 分批查询，每批 200 个
        batch_size = 200
        all_data = []
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"

        logger.info(f"  使用 ulist.np 接口，共 {len(secids)} 个候选代码，分批查询...")
        for i in range(0, len(secids), batch_size):
            batch = secids[i:i + batch_size]
            params = {
                "fltt": "2", "invt": "2",
                "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f100,f112,f113",
                "secids": ",".join(batch),
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "_": "1623833739532",
            }
            try:
                r = self.session.get(url, params=params, timeout=20)
                data_json = r.json()
                diff = data_json.get("data", {}).get("diff", [])
                if diff:
                    all_data.extend(diff)
            except Exception as e:
                logger.warning(f"  批次 {i//batch_size+1} 查询异常: {e}")
            time.sleep(random.uniform(0.2, 0.5))

        if not all_data:
            logger.error("获取A股行情失败")
            return pd.DataFrame()

        logger.info(f"  ulist.np 共返回 {len(all_data)} 条记录")
        df = pd.DataFrame(all_data)

        # 字段映射
        col_map = {
            "f2": "最新价", "f3": "涨跌幅", "f4": "涨跌额", "f5": "成交量",
            "f6": "成交额", "f7": "振幅", "f8": "换手率", "f9": "市盈率动",
            "f10": "量比", "f12": "代码", "f14": "名称", "f15": "最高",
            "f16": "最低", "f17": "今开", "f18": "昨收", "f20": "总市值",
            "f21": "流通市值", "f23": "市净率", "f100": "所处行业",
            "f112": "每股收益", "f113": "每股净资产",
        }
        df = df.rename(columns=col_map)
        # 只保留有效列
        valid_cols = [c for c in col_map.values() if c in df.columns]
        df = df[valid_cols]

        numeric_cols = ["最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅",
                        "换手率", "市盈率动", "量比", "最高", "最低", "今开", "昨收",
                        "总市值", "流通市值", "市净率", "每股收益", "每股净资产"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 过滤：有效A股 + 有价格 + 非ST
        df = df[df["代码"].apply(self.is_a_stock)]
        df = df[df["最新价"].notna() & (df["最新价"] > 0)]
        if "名称" in df.columns:
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

        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
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

    def _fetch_sectors_by_ulist(self, bk_range_start: int, bk_range_end: int) -> list:
        """用 ulist.np 枚举 BKxxxx 板块代码，获取资金流数据"""
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        all_data = []
        batch_size = 100
        codes = ["90.BK%04d" % i for i in range(bk_range_start, bk_range_end)]
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            try:
                r = self.session.get(url, params={
                    "fltt": 2, "invt": 2,
                    "fields": "f12,f14,f2,f3,f62,f184,f66,f69",
                    "secids": ",".join(batch),
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "_": "1623833739532",
                }, timeout=15)
                diff = r.json().get("data", {}).get("diff", [])
                if diff:
                    all_data.extend(diff)
            except Exception as e:
                logger.warning(f"板块批次查询异常: {e}")
            time.sleep(random.uniform(0.1, 0.3))
        return all_data

    def fetch_hot_sectors(self) -> pd.DataFrame:
        """获取行业板块资金流向（用于热点分析）- 使用 ulist.np 规避 clist 封禁"""
        try:
            # 行业板块代码范围 BK0400-BK0800
            data = self._fetch_sectors_by_ulist(400, 800)
            if not data:
                logger.error("获取行业板块资金流异常: 无数据")
                return pd.DataFrame()

            col_map = {"f12": "板块代码", "f14": "板块名称", "f2": "最新价",
                       "f3": "涨跌幅", "f62": "主力净流入", "f184": "主力净流入占比",
                       "f66": "超大单净流入", "f69": "超大单净流入占比"}
            df = pd.DataFrame(data).rename(columns=col_map)
            for col in ["最新价", "涨跌幅", "主力净流入", "主力净流入占比"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["主力净流入"]).sort_values("主力净流入", ascending=False).head(20)
            return df[["板块名称", "涨跌幅", "主力净流入", "主力净流入占比"]].reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取行业板块资金流异常: {e}")
            return pd.DataFrame()

    def fetch_concept_sectors(self) -> pd.DataFrame:
        """获取概念板块资金流向（用于热点分析）- 使用 ulist.np 规避 clist 封禁"""
        try:
            # 概念板块代码范围 BK0900-BK1500
            data = self._fetch_sectors_by_ulist(900, 1500)
            if not data:
                logger.error("获取概念板块资金流异常: 无数据")
                return pd.DataFrame()

            col_map = {"f12": "板块代码", "f14": "板块名称", "f2": "最新价",
                       "f3": "涨跌幅", "f62": "主力净流入", "f184": "主力净流入占比",
                       "f66": "超大单净流入", "f69": "超大单净流入占比"}
            df = pd.DataFrame(data).rename(columns=col_map)
            for col in ["最新价", "涨跌幅", "主力净流入", "主力净流入占比"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["主力净流入"]).sort_values("主力净流入", ascending=False).head(20)
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
