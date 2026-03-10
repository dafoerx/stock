#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 大模型调用模块
使用 DeepSeek V3.2 进行A股分析
"""
import json
import logging
import requests
from BBBIG.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = logging.getLogger("BBBIG")


class DeepSeekClient:
    """DeepSeek API 客户端"""

    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = DEEPSEEK_BASE_URL
        self.model = DEEPSEEK_MODEL
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        })

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.3,
             max_tokens: int = 4096) -> str:
        """
        调用 DeepSeek Chat API
        :param system_prompt: 系统提示词
        :param user_prompt: 用户提示词
        :param temperature: 温度参数
        :param max_tokens: 最大返回token数
        :return: 模型回复文本
        """
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }
        try:
            resp = self.session.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            logger.error("DeepSeek API 请求超时")
            return ""
        except requests.exceptions.HTTPError as e:
            logger.error(f"DeepSeek API HTTP错误: {e}, 响应: {resp.text}")
            return ""
        except Exception as e:
            logger.error(f"DeepSeek API 调用异常: {e}")
            return ""

    def analyze_for_json(self, system_prompt: str, user_prompt: str,
                         temperature: float = 0.2, max_tokens: int = 4096) -> dict:
        """
        调用 DeepSeek 并期望返回 JSON 格式结果
        会尝试从回复中提取 JSON
        """
        result_text = self.chat(system_prompt, user_prompt, temperature, max_tokens)
        if not result_text:
            return {}
        try:
            # 尝试直接解析
            return json.loads(result_text)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 块
        try:
            start = result_text.find("```json")
            if start != -1:
                start = result_text.index("\n", start) + 1
                end = result_text.index("```", start)
                return json.loads(result_text[start:end].strip())
        except (ValueError, json.JSONDecodeError):
            pass
        # 尝试提取 { ... } 块
        try:
            start = result_text.index("{")
            end = result_text.rindex("}") + 1
            return json.loads(result_text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass
        # 尝试提取 [ ... ] 块
        try:
            start = result_text.index("[")
            end = result_text.rindex("]") + 1
            return json.loads(result_text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass
        logger.warning(f"无法从DeepSeek回复中提取JSON，原始回复:\n{result_text[:500]}")
        return {"raw_response": result_text}


# 全局实例
deepseek = DeepSeekClient()
