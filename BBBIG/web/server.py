#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBBIG Web 服务
基于 Tornado 的独立 Web 服务，端口 9999
"""
import os
import sys
import json
import logging
import tornado.ioloop
import tornado.web
import tornado.gen
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from BBBIG.config import RESULT_DIR, DATA_DIR, load_portfolio, save_portfolio
from BBBIG.stock_selector import run_stock_selection
from BBBIG.portfolio_analyzer import run_portfolio_analysis, add_holding, remove_holding

logger = logging.getLogger("BBBIG.web")

# 线程池用于执行耗时的分析任务
executor = ThreadPoolExecutor(max_workers=2)

# 全局任务状态
task_status = {
    "selection": {"running": False, "progress": "", "result": None, "last_run": ""},
    "portfolio": {"running": False, "progress": "", "result": None, "last_run": ""}
}

WEB_DIR = os.path.dirname(os.path.abspath(__file__))


class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")


# ==================== 选股 API ====================

class SelectionRunHandler(BaseHandler):
    """触发选股任务"""
    async def post(self):
        if task_status["selection"]["running"]:
            self.write(json.dumps({"ok": False, "msg": "选股任务正在执行中，请稍候"}, ensure_ascii=False))
            return
        task_status["selection"]["running"] = True
        task_status["selection"]["progress"] = "正在执行..."
        tornado.ioloop.IOLoop.current().run_in_executor(executor, self._run_task)
        self.write(json.dumps({"ok": True, "msg": "选股任务已启动"}, ensure_ascii=False))

    @staticmethod
    def _run_task():
        try:
            result = run_stock_selection()
            task_status["selection"]["result"] = result
            task_status["selection"]["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 持久化
            fpath = os.path.join(RESULT_DIR, f"selection_{datetime.now().strftime('%Y%m%d')}.json")
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"选股任务异常: {e}", exc_info=True)
            task_status["selection"]["result"] = {"error": str(e)}
        finally:
            task_status["selection"]["running"] = False
            task_status["selection"]["progress"] = ""


class SelectionStatusHandler(BaseHandler):
    """查询选股任务状态"""
    def get(self):
        self.write(json.dumps({
            "running": task_status["selection"]["running"],
            "progress": task_status["selection"]["progress"],
            "last_run": task_status["selection"]["last_run"],
            "has_result": task_status["selection"]["result"] is not None
        }, ensure_ascii=False))


class SelectionResultHandler(BaseHandler):
    """获取选股结果"""
    def get(self):
        result = task_status["selection"]["result"]
        if result is None:
            result = self._load_latest()
        if result:
            self.write(json.dumps(result, ensure_ascii=False))
        else:
            self.write(json.dumps({"recommendations": [], "analysis": "", "hot_sectors": ""}, ensure_ascii=False))

    @staticmethod
    def _load_latest():
        """加载最新的选股结果文件"""
        try:
            files = [f for f in os.listdir(RESULT_DIR) if f.startswith("selection_") and f.endswith(".json")]
            if not files:
                return None
            files.sort(reverse=True)
            with open(os.path.join(RESULT_DIR, files[0]), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None


# ==================== 持仓 API ====================

class PortfolioListHandler(BaseHandler):
    """获取持仓列表"""
    def get(self):
        portfolio = load_portfolio()
        self.write(json.dumps(portfolio, ensure_ascii=False))


class PortfolioAddHandler(BaseHandler):
    """添加持仓"""
    def post(self):
        try:
            data = json.loads(self.request.body)
            code = data.get("code", "").strip()
            cost = float(data.get("cost", 0))
            shares = int(data.get("shares", 0))
            name = data.get("name", "").strip()
            if not code or cost <= 0:
                self.write(json.dumps({"ok": False, "msg": "股票代码和成本价不能为空"}, ensure_ascii=False))
                return
            add_holding(code, cost, shares, name)
            self.write(json.dumps({"ok": True, "msg": f"已添加 {code}"}, ensure_ascii=False))
        except Exception as e:
            self.write(json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))


class PortfolioRemoveHandler(BaseHandler):
    """移除持仓"""
    def post(self):
        try:
            data = json.loads(self.request.body)
            code = data.get("code", "").strip()
            if not code:
                self.write(json.dumps({"ok": False, "msg": "股票代码不能为空"}, ensure_ascii=False))
                return
            remove_holding(code)
            self.write(json.dumps({"ok": True, "msg": f"已移除 {code}"}, ensure_ascii=False))
        except Exception as e:
            self.write(json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))


class PortfolioAnalyzeHandler(BaseHandler):
    """触发持仓分析"""
    async def post(self):
        if task_status["portfolio"]["running"]:
            self.write(json.dumps({"ok": False, "msg": "持仓分析任务正在执行中"}, ensure_ascii=False))
            return
        task_status["portfolio"]["running"] = True
        task_status["portfolio"]["progress"] = "正在分析..."
        tornado.ioloop.IOLoop.current().run_in_executor(executor, self._run_task)
        self.write(json.dumps({"ok": True, "msg": "持仓分析任务已启动"}, ensure_ascii=False))

    @staticmethod
    def _run_task():
        try:
            result = run_portfolio_analysis()
            task_status["portfolio"]["result"] = result
            task_status["portfolio"]["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fpath = os.path.join(RESULT_DIR, f"portfolio_{datetime.now().strftime('%Y%m%d')}.json")
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"持仓分析异常: {e}", exc_info=True)
            task_status["portfolio"]["result"] = {"error": str(e)}
        finally:
            task_status["portfolio"]["running"] = False
            task_status["portfolio"]["progress"] = ""


class PortfolioAnalyzeStatusHandler(BaseHandler):
    """查询持仓分析状态"""
    def get(self):
        self.write(json.dumps({
            "running": task_status["portfolio"]["running"],
            "progress": task_status["portfolio"]["progress"],
            "last_run": task_status["portfolio"]["last_run"],
            "has_result": task_status["portfolio"]["result"] is not None
        }, ensure_ascii=False))


class PortfolioAnalyzeResultHandler(BaseHandler):
    """获取持仓分析结果"""
    def get(self):
        result = task_status["portfolio"]["result"]
        if result is None:
            result = self._load_latest()
        if result:
            self.write(json.dumps(result, ensure_ascii=False))
        else:
            self.write(json.dumps({"holdings_analysis": []}, ensure_ascii=False))

    @staticmethod
    def _load_latest():
        try:
            files = [f for f in os.listdir(RESULT_DIR) if f.startswith("portfolio_") and f.endswith(".json")]
            if not files:
                return None
            files.sort(reverse=True)
            with open(os.path.join(RESULT_DIR, files[0]), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None


# ==================== 历史记录 API ====================

class HistoryListHandler(BaseHandler):
    """获取历史分析结果列表"""
    def get(self):
        kind = self.get_argument("type", "all")
        try:
            files = os.listdir(RESULT_DIR)
            results = []
            for f in sorted(files, reverse=True):
                if not f.endswith(".json"):
                    continue
                if kind == "selection" and not f.startswith("selection_"):
                    continue
                if kind == "portfolio" and not f.startswith("portfolio_"):
                    continue
                fpath = os.path.join(RESULT_DIR, f)
                stat = os.stat(fpath)
                results.append({
                    "filename": f,
                    "type": "selection" if f.startswith("selection_") else "portfolio",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
            self.write(json.dumps(results[:50], ensure_ascii=False))
        except Exception as e:
            self.write(json.dumps([], ensure_ascii=False))


class HistoryDetailHandler(BaseHandler):
    """获取历史分析结果详情"""
    def get(self):
        filename = self.get_argument("filename", "")
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            self.set_status(400)
            self.write(json.dumps({"error": "无效文件名"}, ensure_ascii=False))
            return
        fpath = os.path.join(RESULT_DIR, filename)
        # 防止路径穿越
        if not os.path.realpath(fpath).startswith(os.path.realpath(RESULT_DIR)):
            self.set_status(403)
            self.write(json.dumps({"error": "禁止访问"}, ensure_ascii=False))
            return
        if not os.path.exists(fpath):
            self.set_status(404)
            self.write(json.dumps({"error": "文件不存在"}, ensure_ascii=False))
            return
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.write(json.dumps(data, ensure_ascii=False))


def make_app():
    settings = {
        "template_path": os.path.join(WEB_DIR, "templates"),
        "static_path": os.path.join(WEB_DIR, "static"),
        "static_url_prefix": "/static/",
        "debug": True,
    }
    return tornado.web.Application([
        (r"/", IndexHandler),
        # 选股
        (r"/api/selection/run", SelectionRunHandler),
        (r"/api/selection/status", SelectionStatusHandler),
        (r"/api/selection/result", SelectionResultHandler),
        # 持仓
        (r"/api/portfolio/list", PortfolioListHandler),
        (r"/api/portfolio/add", PortfolioAddHandler),
        (r"/api/portfolio/remove", PortfolioRemoveHandler),
        (r"/api/portfolio/analyze", PortfolioAnalyzeHandler),
        (r"/api/portfolio/analyze/status", PortfolioAnalyzeStatusHandler),
        (r"/api/portfolio/analyze/result", PortfolioAnalyzeResultHandler),
        # 历史
        (r"/api/history/list", HistoryListHandler),
        (r"/api/history/detail", HistoryDetailHandler),
    ], **settings)


def start_web(port=9999):
    app = make_app()
    app.listen(port)
    logger.info(f"BBBIG Web 服务启动: http://localhost:{port}")
    print(f"\n  BBBIG Web 服务已启动: http://localhost:{port}\n")
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    start_web()
