#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消息面情绪分析模块（FinGPT 思路）
利用 Tushare 新闻接口 + DeepSeek 大模型做情绪判断，
在选股前对行业/个股进行"利空过滤"。

核心逻辑：
1. 从 Tushare 获取近期新闻（个股公告 + 行业新闻）
2. 按行业 / 个股聚合新闻摘要
3. 调用 DeepSeek 做批量情绪打分（-1~+1）
4. 情绪低于阈值的行业/个股标记为"利空"，选股时跳过或降权
"""
import logging
import time
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

from BBBIG.deepseek_client import deepseek
from BBBIG.config import (
    SENTIMENT_ENABLED, SENTIMENT_NEGATIVE_THRESHOLD,
    SENTIMENT_PENALTY_WEIGHT, SENTIMENT_NEWS_DAYS,
    SENTIMENT_MAX_NEWS_PER_BATCH
)

logger = logging.getLogger("BBBIG")


class SentimentAnalyzer:
    """消息面情绪分析器（FinGPT 风格）"""

    def __init__(self):
        # 缓存：{日期: {行业/代码: sentiment_score}}
        self._cache = {}

    def analyze_industry_sentiment(
        self, news_by_industry: Dict[str, List[str]]
    ) -> Dict[str, dict]:
        """
        批量分析行业情绪

        参数:
            news_by_industry: {行业名: [新闻标题/摘要列表]}

        返回:
            {行业名: {"score": float(-1~1), "label": "利好/中性/利空", "reason": str}}
        """
        if not news_by_industry:
            return {}

        # 构造批量分析 prompt
        industry_texts = []
        industry_names = []
        for industry, news_list in news_by_industry.items():
            if not news_list:
                continue
            # 截取最多15条新闻
            trimmed = news_list[:15]
            news_text = "\n".join(f"  - {n}" for n in trimmed)
            industry_texts.append(f"【{industry}】\n{news_text}")
            industry_names.append(industry)

        if not industry_texts:
            return {}

        # 分批处理（每批最多10个行业）
        results = {}
        batch_size = 10
        for i in range(0, len(industry_texts), batch_size):
            batch_texts = industry_texts[i:i + batch_size]
            batch_names = industry_names[i:i + batch_size]
            batch_result = self._call_sentiment_llm(batch_texts, batch_names, level="行业")
            results.update(batch_result)
            if i + batch_size < len(industry_texts):
                time.sleep(1)  # 批次间隔

        return results

    def analyze_stock_sentiment(
        self, news_by_stock: Dict[str, List[str]]
    ) -> Dict[str, dict]:
        """
        批量分析个股情绪

        参数:
            news_by_stock: {股票代码_名称: [新闻标题/摘要列表]}

        返回:
            {股票代码_名称: {"score": float(-1~1), "label": "利好/中性/利空", "reason": str}}
        """
        if not news_by_stock:
            return {}

        stock_texts = []
        stock_keys = []
        for stock_key, news_list in news_by_stock.items():
            if not news_list:
                continue
            trimmed = news_list[:10]
            news_text = "\n".join(f"  - {n}" for n in trimmed)
            stock_texts.append(f"【{stock_key}】\n{news_text}")
            stock_keys.append(stock_key)

        if not stock_texts:
            return {}

        # 分批处理（每批最多20只股票）
        results = {}
        batch_size = 20
        for i in range(0, len(stock_texts), batch_size):
            batch_texts = stock_texts[i:i + batch_size]
            batch_keys = stock_keys[i:i + batch_size]
            batch_result = self._call_sentiment_llm(batch_texts, batch_keys, level="个股")
            results.update(batch_result)
            if i + batch_size < len(stock_texts):
                time.sleep(1)

        return results

    def _call_sentiment_llm(
        self, texts: List[str], keys: List[str], level: str = "行业"
    ) -> Dict[str, dict]:
        """
        调用 DeepSeek 进行情绪打分

        采用 FinGPT 的 prompt 设计思路：
        - 角色设定为金融情绪分析专家
        - 输出结构化 JSON
        - 评分范围 -1（极度利空） ~ +1（极度利好）
        """
        combined_text = "\n\n".join(texts)

        system_prompt = """你是一位专业的A股金融情绪分析师，擅长从新闻标题和摘要中判断市场情绪方向。

你的任务是对每个{level}的新闻进行情绪分析，给出：
1. **情绪分数** (score): -1.0（极度利空）到 +1.0（极度利好），精确到小数点后2位
   - [-1.0, -0.5]: 强烈利空（重大负面事件：业绩暴雷、重大违规、政策打压、行业衰退）
   - (-0.5, -0.2]: 偏利空（轻度负面：业绩不及预期、减持、竞争加剧）
   - (-0.2, 0.2): 中性（日常运营、无明显方向性的新闻）
   - [0.2, 0.5): 偏利好（正面信号：业绩增长、新品发布、政策支持）
   - [0.5, 1.0]: 强烈利好（重大利好：突破性技术、大额订单、政策红利）
2. **情绪标签** (label): "利好" / "中性" / "利空"
3. **判断理由** (reason): 20字以内的简短理由

关键原则：
- 关注实质性影响，忽略营销软文和无关噪音
- 同一{level}有利好也有利空时，综合权衡给出净情绪
- 宁可偏保守（偏中性），不要过度解读
- 政策面和行业趋势的权重高于个别事件""".format(level=level)

        user_prompt = f"""请对以下{len(keys)}个{level}的近期新闻进行情绪分析：

{combined_text}

请严格按以下JSON格式返回（不要有多余文字）：
```json
{{
  "sentiments": [
    {{
      "name": "{level}名称",
      "score": 0.00,
      "label": "中性",
      "reason": "判断理由"
    }}
  ]
}}
```"""

        try:
            result = deepseek.analyze_for_json(
                system_prompt, user_prompt,
                temperature=0.1, max_tokens=2048
            )

            if isinstance(result, dict) and "sentiments" in result:
                sentiments = result["sentiments"]
                output = {}
                for item in sentiments:
                    name = item.get("name", "")
                    # 匹配到原始 key
                    matched_key = self._match_key(name, keys)
                    if matched_key:
                        score = float(item.get("score", 0))
                        score = max(-1.0, min(1.0, score))  # 限制范围
                        label = item.get("label", "中性")
                        if label not in ("利好", "中性", "利空"):
                            label = "利好" if score > 0.2 else ("利空" if score < -0.2 else "中性")
                        output[matched_key] = {
                            "score": round(score, 2),
                            "label": label,
                            "reason": item.get("reason", "")[:30]
                        }
                return output
            else:
                logger.warning(f"情绪分析返回格式异常: {str(result)[:200]}")
                return {}

        except Exception as e:
            logger.warning(f"情绪分析调用异常: {e}")
            return {}

    @staticmethod
    def _match_key(name: str, keys: List[str]) -> Optional[str]:
        """模糊匹配 LLM 返回的名称到原始 key"""
        if not name:
            return None
        # 精确匹配
        if name in keys:
            return name
        # 包含匹配
        for key in keys:
            if name in key or key in name:
                return key
        # 去除空格后匹配
        name_clean = name.replace(" ", "")
        for key in keys:
            if name_clean in key.replace(" ", "") or key.replace(" ", "") in name_clean:
                return key
        return None

    def filter_by_sentiment(
        self,
        candidates_df,
        industry_sentiment: Dict[str, dict],
        stock_sentiment: Dict[str, dict],
        negative_threshold: float = None
    ) -> Tuple:
        """
        根据情绪分析结果过滤/降权候选股

        参数:
            candidates_df: 候选股 DataFrame（需包含 '代码', '名称', '所处行业' 列）
            industry_sentiment: 行业情绪 {行业名: {score, label, reason}}
            stock_sentiment: 个股情绪 {代码_名称: {score, label, reason}}
            negative_threshold: 利空阈值（低于此值视为利空），默认使用配置值

        返回:
            (filtered_df, sentiment_log)
            - filtered_df: 过滤后的候选 DataFrame（新增 'sentiment_score' 列）
            - sentiment_log: 情绪分析日志列表
        """
        import pandas as pd

        if negative_threshold is None:
            negative_threshold = SENTIMENT_NEGATIVE_THRESHOLD

        df = candidates_df.copy()
        sentiment_scores = []
        sentiment_log = []

        for _, row in df.iterrows():
            code = row.get('代码', '')
            name = row.get('名称', '')
            industry = row.get('所处行业', '')
            stock_key = f"{code}_{name}"

            # 行业情绪
            ind_sent = industry_sentiment.get(industry, {})
            ind_score = ind_sent.get('score', 0.0)

            # 个股情绪
            stk_sent = stock_sentiment.get(stock_key, {})
            stk_score = stk_sent.get('score', 0.0)

            # 综合情绪 = 行业情绪 * 0.4 + 个股情绪 * 0.6
            # 如果只有行业或只有个股，则单独使用
            if stk_sent and ind_sent:
                combined_score = ind_score * 0.4 + stk_score * 0.6
            elif stk_sent:
                combined_score = stk_score
            elif ind_sent:
                combined_score = ind_score
            else:
                combined_score = 0.0  # 无新闻时视为中性

            sentiment_scores.append(round(combined_score, 2))

            # 记录日志
            if combined_score <= negative_threshold:
                reason_parts = []
                if ind_sent:
                    reason_parts.append(f"行业{ind_sent.get('label','中性')}({ind_score:+.2f}:{ind_sent.get('reason','')})")
                if stk_sent:
                    reason_parts.append(f"个股{stk_sent.get('label','中性')}({stk_score:+.2f}:{stk_sent.get('reason','')})")
                sentiment_log.append({
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "score": combined_score,
                    "action": "过滤" if combined_score <= negative_threshold else "降权",
                    "reason": "; ".join(reason_parts)
                })

        df['sentiment_score'] = sentiment_scores

        # 过滤强烈利空（score <= 阈值）
        before_count = len(df)
        filtered = df[df['sentiment_score'] > negative_threshold].copy()
        removed_count = before_count - len(filtered)

        if removed_count > 0:
            logger.info(f"  📰 情绪过滤: {removed_count} 只候选股因消息面利空被移除")
            for log_item in sentiment_log:
                if log_item['action'] == '过滤':
                    logger.info(f"    ❌ {log_item['code']} {log_item['name']}: "
                                f"情绪{log_item['score']:+.2f} — {log_item['reason']}")

        return filtered.reset_index(drop=True), sentiment_log


def aggregate_news_by_industry(
    news_list: List[dict],
    stock_basic_map: Dict[str, str]
) -> Dict[str, List[str]]:
    """
    将新闻列表按行业聚合

    参数:
        news_list: [{"title": str, "content": str, "ts_code": str, ...}]
        stock_basic_map: {ts_code: industry}

    返回:
        {行业名: [新闻标题列表]}
    """
    industry_news = {}
    for news in news_list:
        title = news.get("title", "").strip()
        if not title:
            continue

        # 尝试从 ts_code 找到行业
        ts_code = news.get("ts_code", "")
        industry = stock_basic_map.get(ts_code, "")

        # 如果有行业关键词匹配
        if not industry:
            industry = _extract_industry_from_title(title)

        if industry:
            if industry not in industry_news:
                industry_news[industry] = []
            industry_news[industry].append(title)

    return industry_news


def aggregate_news_by_stock(
    news_list: List[dict],
    candidate_codes: List[str]
) -> Dict[str, List[str]]:
    """
    将新闻列表按个股聚合（仅保留候选股票的新闻）

    参数:
        news_list: [{"title": str, "ts_code": str, "name": str, ...}]
        candidate_codes: 候选股票代码列表 (如 ["000001", "600519"])

    返回:
        {代码_名称: [新闻标题列表]}
    """
    # 构建候选股 ts_code 集合
    candidate_ts_codes = set()
    for c in candidate_codes:
        if c.startswith('6'):
            candidate_ts_codes.add(f"{c}.SH")
        else:
            candidate_ts_codes.add(f"{c}.SZ")

    stock_news = {}
    for news in news_list:
        ts_code = news.get("ts_code", "")
        if ts_code not in candidate_ts_codes:
            continue

        title = news.get("title", "").strip()
        if not title:
            continue

        name = news.get("name", ts_code)
        code = ts_code.split('.')[0]
        key = f"{code}_{name}"
        if key not in stock_news:
            stock_news[key] = []
        stock_news[key].append(title)

    return stock_news


def _extract_industry_from_title(title: str) -> str:
    """从新闻标题中粗略提取行业关键词（降级匹配）"""
    industry_keywords = {
        "半导体": "半导体", "芯片": "半导体", "集成电路": "半导体",
        "新能源": "新能源", "光伏": "光伏设备", "锂电": "电池",
        "储能": "电池", "风电": "电力设备",
        "医药": "医药生物", "医疗": "医药生物", "创新药": "医药生物",
        "银行": "银行", "券商": "证券", "保险": "保险",
        "房地产": "房地产开发", "地产": "房地产开发",
        "白酒": "白酒", "食品": "食品饮料", "消费": "商贸零售",
        "汽车": "汽车整车", "新能源车": "汽车整车",
        "人工智能": "计算机", "AI": "计算机", "算力": "通信",
        "5G": "通信", "机器人": "自动化设备",
        "钢铁": "钢铁", "煤炭": "煤炭开采", "石油": "石油石化",
        "军工": "国防军工", "航天": "国防军工",
    }
    for keyword, industry in industry_keywords.items():
        if keyword in title:
            return industry
    return ""


# 全局实例
sentiment_analyzer = SentimentAnalyzer()
