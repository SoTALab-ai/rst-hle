"""与冻结管线隔离的本机只读HTTP入口。"""
import json
import logging
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg

from .console_data import ConsoleData

WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/katex.min.js": ("vendor/katex.min.js", "text/javascript")}
STATIC["/quality-flow.mjs"] = ("quality-flow.mjs", "text/javascript")
LOG = logging.getLogger(__name__)


def make_handler(data: ConsoleData):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            # 路由参数可能含题目身份，不将请求URL或异常连接字符串写入日志。
            pass

        def reply(self, status, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; img-src 'self' data:; object-src 'none'; "
                             "base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in expected or (origin and origin != "http://" + host):
                self.reply(HTTPStatus.FORBIDDEN, {"error": "仅允许本机同源访问"})
                return
            path = unquote(urlsplit(self.path).path)
            try:
                if path in STATIC:
                    filename, mime = STATIC[path]
                    self.reply(200, (WEB / filename).read_bytes(), mime)
                elif path == "/api/snapshot":
                    self.reply(200, data.snapshot().model_dump())
                elif path.startswith("/api/questions/"):
                    self.reply(200, data.question(path.removeprefix("/api/questions/")))
                elif path.startswith("/api/runs/"):
                    self.reply(200, data.run(path.removeprefix("/api/runs/")))
                elif path.startswith("/api/archives/"):
                    self.reply(200, data.archive(path.removeprefix("/api/archives/")))
                else:
                    self.reply(404, {"error": "页面或记录不存在"})
            except KeyError:
                self.reply(404, {"error": "记录不存在"})
            except (OSError, ValueError, TypeError, sqlite3.Error, psycopg.Error) as error:
                LOG.warning("控制台读取失败：%s", type(error).__name__)
                self.reply(503, {"error": "数据读取失败；请检查数据库、模型结构版本和对象完整性。"})

    return Handler


def serve(database, objects, reports, host, port):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("控制台仅绑定本机地址；远程使用SSH端口转发")
    data = ConsoleData(database, objects, reports)
    with ThreadingHTTPServer((host, port), make_handler(data)) as server:
        print(f"RST-HLE 控制台 http://127.0.0.1:{server.server_port} （只读）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
