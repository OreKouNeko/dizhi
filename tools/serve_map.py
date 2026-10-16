"""本地地图查看服务器：给天地图瓦片代理补上 Referer。

### 为什么需要它

天地图的**服务器端 key 强制要求请求头带 `Referer: http://lbs.tianditu.gov.cn/`**，
否则任何接口（含瓦片）都返回 403 `{"code":301013,"msg":"权限类型错误"}`。

浏览器打开本地 HTML 时 Referer 是 `file:///...`，而 **JS 无法伪造 Referer**，
所以带天地图底图的地图在本地文件里必然是白图。这个服务器做两件事：

  1. 把 HTML 里指向 `t{s}.tianditu.gov.cn` 的瓦片地址改写成走本地代理
  2. 转发请求时补上 Referer，并把 key 一起带上

用法：
    python tools/serve_map.py 你的地图.html
    # 然后浏览器打开 http://127.0.0.1:8899/

另一种更省事的做法：在天地图控制台再申请一个**浏览器端**类型的 key 专供网页底图，
那样直接双击 HTML 就能显示，不需要这个服务器。
"""
from __future__ import annotations

import argparse
import http.server
import os
import re
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from dizhi.settings import TIANDITU_REFERER, TIANDITU_TK
except ImportError:                                    # 允许脱离项目单独跑
    TIANDITU_TK = os.environ.get("TIANDITU_TK", "")
    TIANDITU_REFERER = os.environ.get("TIANDITU_REFERER",
                                      "http://lbs.tianditu.gov.cn/")

TDT_RE = re.compile(r"https://t\{s\}\.tianditu\.gov\.cn/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HTML_PATH = ""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):        # 静音，别刷屏
        pass

    def do_GET(self):                    # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/tdt/"):
            self._proxy_tile(self.path[len("/tdt/"):])
            return
        if path in ("/", "/index.html"):
            self._serve_html()
            return
        self.send_response(404)
        self.end_headers()

    def _proxy_tile(self, rest: str) -> None:
        url = "https://t0.tianditu.gov.cn/" + rest
        if "tk=" not in url:
            url += ("&" if "?" in url else "?") + "tk=" + TIANDITU_TK
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": TIANDITU_REFERER,      # ← 关键，少了就是 403
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                data, ctype = r.read(), r.headers.get("Content-Type", "image/png")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            self._fail(exc.code, f"天地图返回 {exc.code}")
        except Exception as exc:              # noqa: BLE001
            self._fail(502, f"瓦片代理失败：{exc}")

    def _serve_html(self) -> None:
        p = Path(HTML_PATH)
        if not p.exists():
            self._fail(404, "找不到地图 HTML")
            return
        if not TIANDITU_TK:
            print("[提示] 没有配置 TIANDITU_TK，天地图瓦片不会显示。"
                  "请在 .env 里设置。")
        html = TDT_RE.sub("/tdt/", p.read_text(encoding="utf-8"))
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global HTML_PATH
    ap = argparse.ArgumentParser(description="本地查看地图（代理天地图瓦片）")
    ap.add_argument("html", help="要打开的 HTML 地图路径")
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()

    HTML_PATH = os.path.abspath(a.html)
    if not os.path.exists(HTML_PATH):
        print(f"文件不存在：{HTML_PATH}")
        return 1
    print(f"地图服务已启动：http://127.0.0.1:{a.port}/")
    print(f"  源文件：{HTML_PATH}")
    if TIANDITU_TK:
        print("  天地图瓦片已通过本地代理补上 Referer。")
    else:
        print("  [注意] 未配置 TIANDITU_TK，天地图底图会是白的。")
    print("  按 Ctrl+C 停止。")
    with Server(("127.0.0.1", a.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
