#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 大模型调用模块
支持 DeepSeek / 自定义兼容 OpenAI 格式的 API（含 SSE 流式响应解析）
"""
import json
import logging
import requests
from BBBIG.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = logging.getLogger("BBBIG")


class DeepSeekClient:
    """AI 大模型 API 客户端（兼容 OpenAI / DeepSeek / 自定义 API）"""

    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = DEEPSEEK_BASE_URL
        self.model = DEEPSEEK_MODEL
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        })

    @staticmethod
    def _parse_sse_response(text: str) -> str:
        """解析 SSE (text/event-stream) 流式响应，拼接所有 chunk 的 content"""
        chunks = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    chunks.append(content)
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
        return "".join(chunks)

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.3,
             max_tokens: int = 4096) -> str:
        """
        调用 AI Chat API
        :param system_prompt: 系统提示词
        :param user_prompt: 用户提示词
        :param temperature: 温度参数（部分API不支持，会自动忽略）
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
            "max_tokens": max_tokens,
            "stream": False
        }
        # temperature 仅在标准 OpenAI / DeepSeek API 时添加
        # 自定义 API（如 quan2go）不支持此参数
        is_custom_api = "quan2go" in self.base_url or "deepseek" not in self.base_url
        if not is_custom_api:
            payload["temperature"] = temperature

        try:
            resp = self.session.post(url, json=payload, timeout=180)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            # 强制 UTF-8 解码（部分API返回的Content-Type不带charset，requests默认用ISO-8859-1会乱码）
            body = resp.content.decode("utf-8")

            # 情况1: 标准 JSON 响应
            if "application/json" in content_type:
                data = json.loads(body)
                return data["choices"][0]["message"]["content"]

            # 情况2: SSE 流式响应 (text/event-stream)
            if "text/event-stream" in content_type or body.lstrip().startswith("data: "):
                result = self._parse_sse_response(body)
                if result:
                    return result
                logger.warning("SSE 响应解析为空")
                return ""

            # 情况3: 尝试按 JSON 解析
            try:
                data = json.loads(body)
                return data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

            # 情况4: 尝试按 SSE 解析
            result = self._parse_sse_response(body)
            if result:
                return result

            logger.error(f"AI API 未知响应格式 (Content-Type: {content_type}): {body[:300]}")
            return ""

        except requests.exceptions.Timeout:
            logger.error("AI API 请求超时")
            return ""
        except requests.exceptions.HTTPError as e:
            body_text = resp.content.decode("utf-8", errors="replace")
            logger.error(f"AI API HTTP错误: {e}, 响应: {body_text[:500]}")
            return ""
        except Exception as e:
            logger.error(f"AI API 调用异常: {e}")
            return ""

    def analyze_for_json(self, system_prompt: str, user_prompt: str,
                         temperature: float = 0.2, max_tokens: int = 4096) -> dict:
        """
        调用 AI 并期望返回 JSON 格式结果
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
        logger.warning(f"无法从AI回复中提取JSON，原始回复:\n{result_text[:500]}")
        return {"raw_response": result_text}


# 全局实例
deepseek = DeepSeekClient()
