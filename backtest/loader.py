"""
backtest/loader.py — 数据加载层
从 BBBIG SQLite 缓存读取日行情，转换为 backtesting.py 标准 DataFrame 格式
"""

import sqlite3
import pandas as pd
from pathlib import Path

# 默认数据库路径（相对于项目根目录）
DEFAULT_DB = Path(__file__).parent.parent / "BBBIG" / "data" / "stock_data.db"


def load_stock(
    ts_code: str,
    start_date: str = None,
    end_date: str = None,
    db_path: str = None,
) -> pd.DataFrame:
    """
    从 SQLite 缓存加载单只股票日行情。

    Parameters
    ----------
    ts_code   : str  Tushare 格式代码，如 '000001.SZ' 或 '600519.SH'
    start_date: str  起始日期 'YYYYMMDD'（含），默认全量
    end_date  : str  截止日期 'YYYYMMDD'（含），默认全量
    db_path   : str  数据库路径，默认使用 BBBIG/data/stock_data.db

    Returns
    -------
    pd.DataFrame  columns: Open High Low Close Volume (backtesting.py 标准格式)
                  index: pd.DatetimeIndex（升序）
    """
    db = db_path or DEFAULT_DB
    conn = sqlite3.connect(db)

    where_clauses = ["ts_code = ?"]
    params = [ts_code]
    if start_date:
        where_clauses.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("trade_date <= ?")
        params.append(end_date)

    sql = f"""
        SELECT trade_date, open, high, low, close, vol
        FROM daily
        WHERE {" AND ".join(where_clauses)}
        ORDER BY trade_date ASC
    """
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()

    if df.empty:
        raise ValueError(f"No data found for {ts_code} in DB ({db})")

    df.index = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.drop(columns=["trade_date"])
    df = df.rename(columns={
        "open":  "Open",
        "high":  "High",
        "low":   "Low",
        "close": "Close",
        "vol":   "Volume",
    })
    df = df.astype(float)
    return df


def load_stocks(
    ts_codes: list,
    start_date: str = None,
    end_date: str = None,
    db_path: str = None,
) -> dict:
    """
    批量加载多只股票，返回 {ts_code: DataFrame} 字典
    """
    return {
        code: load_stock(code, start_date, end_date, db_path)
        for code in ts_codes
    }


def list_available_stocks(db_path: str = None) -> pd.DataFrame:
    """
    列出数据库中有日行情数据的股票及其时间范围
    """
    db = db_path or DEFAULT_DB
    conn = sqlite3.connect(db)
    df = pd.read_sql_query("""
        SELECT d.ts_code,
               b.name,
               b.industry,
               MIN(d.trade_date) AS first_date,
               MAX(d.trade_date) AS last_date,
               COUNT(*) AS trading_days
        FROM daily d
        LEFT JOIN stock_basic b ON d.ts_code = b.ts_code
        GROUP BY d.ts_code
        ORDER BY trading_days DESC
    """, conn)
    conn.close()
    return df
