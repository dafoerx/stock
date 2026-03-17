#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 数据缓存层
将 Tushare 获取的数据持久化到本地 SQLite，支持增量更新，减少 API 调用
"""
import sqlite3
import logging
import pandas as pd
from datetime import datetime

from BBBIG.config import DB_FILE

logger = logging.getLogger("BBBIG")


class StockDBCache:
    """股票数据 SQLite 缓存"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_FILE
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        """初始化数据库表结构"""
        conn = self._get_conn()
        try:
            conn.executescript("""
                -- 交易日历
                CREATE TABLE IF NOT EXISTS trade_cal (
                    cal_date TEXT PRIMARY KEY,
                    is_open INTEGER
                );

                -- 股票基础信息
                CREATE TABLE IF NOT EXISTS stock_basic (
                    ts_code TEXT PRIMARY KEY,
                    symbol TEXT,
                    name TEXT,
                    area TEXT,
                    industry TEXT,
                    market TEXT,
                    list_date TEXT,
                    updated_at TEXT
                );

                -- 日行情（daily）
                CREATE TABLE IF NOT EXISTS daily (
                    ts_code TEXT,
                    trade_date TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    pre_close REAL,
                    change REAL,
                    pct_chg REAL,
                    vol REAL,
                    amount REAL,
                    PRIMARY KEY (ts_code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date);

                -- 每日指标（daily_basic）
                CREATE TABLE IF NOT EXISTS daily_basic (
                    ts_code TEXT,
                    trade_date TEXT,
                    turnover_rate REAL,
                    pe_ttm REAL,
                    pb REAL,
                    ps_ttm REAL,
                    total_mv REAL,
                    circ_mv REAL,
                    volume_ratio REAL,
                    PRIMARY KEY (ts_code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_basic_date ON daily_basic(trade_date);

                -- 概念板块列表
                CREATE TABLE IF NOT EXISTS concept (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    src TEXT,
                    updated_at TEXT
                );

                -- 概念板块成分股
                CREATE TABLE IF NOT EXISTS concept_detail (
                    concept_code TEXT,
                    ts_code TEXT,
                    PRIMARY KEY (concept_code, ts_code)
                );

                -- 数据同步记录（记录哪些日期的哪类数据已获取）
                CREATE TABLE IF NOT EXISTS sync_log (
                    data_type TEXT,
                    trade_date TEXT,
                    synced_at TEXT,
                    PRIMARY KEY (data_type, trade_date)
                );
            """)
            conn.commit()
        finally:
            conn.close()

    # ========== 交易日历 ==========

    def get_trade_dates(self, start_date: str, end_date: str) -> list:
        """获取交易日列表"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT cal_date FROM trade_cal WHERE cal_date BETWEEN ? AND ? AND is_open=1 ORDER BY cal_date",
                (start_date, end_date)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def save_trade_cal(self, df: pd.DataFrame):
        """保存交易日历"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            for _, row in df.iterrows():
                conn.execute(
                    "INSERT OR REPLACE INTO trade_cal (cal_date, is_open) VALUES (?, ?)",
                    (row['cal_date'], int(row['is_open']))
                )
            conn.commit()
            logger.info(f"缓存交易日历 {len(df)} 条")
        finally:
            conn.close()

    def has_trade_cal(self, year_month: str = None) -> bool:
        """检查交易日历是否已缓存"""
        conn = self._get_conn()
        try:
            if year_month:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trade_cal WHERE cal_date LIKE ?", (f"{year_month}%",)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM trade_cal").fetchone()
            return row[0] > 0
        finally:
            conn.close()

    # ========== 股票基础信息 ==========

    def get_stock_basic(self) -> pd.DataFrame:
        """获取所有股票基础信息"""
        conn = self._get_conn()
        try:
            df = pd.read_sql("SELECT * FROM stock_basic", conn)
            return df
        finally:
            conn.close()

    def save_stock_basic(self, df: pd.DataFrame):
        """保存股票基础信息"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for _, row in df.iterrows():
                conn.execute(
                    """INSERT OR REPLACE INTO stock_basic
                    (ts_code, symbol, name, area, industry, market, list_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row.get('ts_code', ''), row.get('symbol', ''), row.get('name', ''),
                     row.get('area', ''), row.get('industry', ''), row.get('market', ''),
                     row.get('list_date', ''), now)
                )
            conn.commit()
            logger.info(f"缓存股票基础信息 {len(df)} 条")
        finally:
            conn.close()

    def is_stock_basic_fresh(self, max_age_hours: int = 24) -> bool:
        """检查股票基础信息是否需要更新"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM stock_basic"
            ).fetchone()
            if not row or not row[0]:
                return False
            last_update = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            return (datetime.now() - last_update).total_seconds() < max_age_hours * 3600
        except Exception:
            return False
        finally:
            conn.close()

    # ========== 日行情 ==========

    def get_daily_by_date(self, trade_date: str) -> pd.DataFrame:
        """获取某日全市场日行情"""
        conn = self._get_conn()
        try:
            df = pd.read_sql(
                "SELECT * FROM daily WHERE trade_date=?", conn, params=(trade_date,)
            )
            return df
        finally:
            conn.close()

    def get_daily_by_code(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取某只股票一段时间的日行情"""
        conn = self._get_conn()
        try:
            df = pd.read_sql(
                "SELECT * FROM daily WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                conn, params=(ts_code, start_date, end_date)
            )
            return df
        finally:
            conn.close()

    def save_daily(self, df: pd.DataFrame):
        """保存日行情数据"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close',
                    'pre_close', 'change', 'pct_chg', 'vol', 'amount']
            for _, row in df.iterrows():
                values = tuple(row.get(c, None) for c in cols)
                conn.execute(
                    f"INSERT OR REPLACE INTO daily ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
                    values
                )
            conn.commit()
        finally:
            conn.close()

    def has_daily(self, trade_date: str) -> bool:
        """检查某日日行情是否已缓存"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM daily WHERE trade_date=?", (trade_date,)
            ).fetchone()
            return row[0] > 100  # 至少有100条才算有效
        finally:
            conn.close()

    # ========== 每日指标 ==========

    def get_daily_basic_by_date(self, trade_date: str) -> pd.DataFrame:
        """获取某日全市场每日指标"""
        conn = self._get_conn()
        try:
            df = pd.read_sql(
                "SELECT * FROM daily_basic WHERE trade_date=?", conn, params=(trade_date,)
            )
            return df
        finally:
            conn.close()

    def save_daily_basic(self, df: pd.DataFrame):
        """保存每日指标"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            cols = ['ts_code', 'trade_date', 'turnover_rate', 'pe_ttm', 'pb',
                    'ps_ttm', 'total_mv', 'circ_mv', 'volume_ratio']
            for _, row in df.iterrows():
                values = tuple(row.get(c, None) for c in cols)
                conn.execute(
                    f"INSERT OR REPLACE INTO daily_basic ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
                    values
                )
            conn.commit()
        finally:
            conn.close()

    def has_daily_basic(self, trade_date: str) -> bool:
        """检查某日每日指标是否已缓存"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM daily_basic WHERE trade_date=?", (trade_date,)
            ).fetchone()
            return row[0] > 100
        finally:
            conn.close()

    # ========== 概念板块 ==========

    def get_concepts(self) -> pd.DataFrame:
        """获取概念板块列表"""
        conn = self._get_conn()
        try:
            df = pd.read_sql("SELECT code, name, src FROM concept", conn)
            return df
        finally:
            conn.close()

    def save_concepts(self, df: pd.DataFrame):
        """保存概念板块列表"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for _, row in df.iterrows():
                conn.execute(
                    "INSERT OR REPLACE INTO concept (code, name, src, updated_at) VALUES (?, ?, ?, ?)",
                    (row.get('code', ''), row.get('name', ''), row.get('src', ''), now)
                )
            conn.commit()
            logger.info(f"缓存概念板块 {len(df)} 个")
        finally:
            conn.close()

    def is_concept_fresh(self, max_age_hours: int = 72) -> bool:
        """概念列表 72 小时内有效"""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT MAX(updated_at) FROM concept").fetchone()
            if not row or not row[0]:
                return False
            last_update = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            return (datetime.now() - last_update).total_seconds() < max_age_hours * 3600
        except Exception:
            return False
        finally:
            conn.close()

    def get_concept_detail(self, concept_code: str) -> list:
        """获取概念板块成分股"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT ts_code FROM concept_detail WHERE concept_code=?", (concept_code,)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def save_concept_detail(self, concept_code: str, ts_codes: list):
        """保存概念板块成分股"""
        if not ts_codes:
            return
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM concept_detail WHERE concept_code=?", (concept_code,))
            for code in ts_codes:
                conn.execute(
                    "INSERT OR REPLACE INTO concept_detail (concept_code, ts_code) VALUES (?, ?)",
                    (concept_code, code)
                )
            conn.commit()
        finally:
            conn.close()

    def has_concept_detail(self, concept_code: str) -> bool:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM concept_detail WHERE concept_code=?", (concept_code,)
            ).fetchone()
            return row[0] > 0
        finally:
            conn.close()

    # ========== 同步记录 ==========

    def mark_synced(self, data_type: str, trade_date: str):
        """标记某类数据某日已同步"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sync_log (data_type, trade_date, synced_at) VALUES (?, ?, ?)",
                (data_type, trade_date, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            conn.commit()
        finally:
            conn.close()

    def is_synced(self, data_type: str, trade_date: str) -> bool:
        """检查某类数据某日是否已同步"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE data_type=? AND trade_date=?",
                (data_type, trade_date)
            ).fetchone()
            return row[0] > 0
        finally:
            conn.close()

    # ========== 工具方法 ==========

    def get_missing_trade_dates(self, data_type: str, start_date: str, end_date: str) -> list:
        """获取某类数据在某段时间内缺失的交易日"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT tc.cal_date FROM trade_cal tc
                   WHERE tc.cal_date BETWEEN ? AND ? AND tc.is_open=1
                   AND tc.cal_date NOT IN (
                       SELECT trade_date FROM sync_log WHERE data_type=?
                   )
                   ORDER BY tc.cal_date""",
                (start_date, end_date, data_type)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def get_db_stats(self) -> dict:
        """获取数据库统计信息"""
        conn = self._get_conn()
        try:
            stats = {}
            for table in ['trade_cal', 'stock_basic', 'daily', 'daily_basic', 'concept', 'concept_detail', 'sync_log']:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = row[0]
            return stats
        finally:
            conn.close()


# 全局实例
db_cache = StockDBCache()
