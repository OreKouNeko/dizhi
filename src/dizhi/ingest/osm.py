"""OSM 数据获取 —— 独立模块，不依赖检索引擎。

只负责把 `.osm.pbf` 拉到本地。之后要建库请调 `dizhi.ingest.pbf.build()`。

### 为什么不用现成的下载工具

全国 OSM 包约 1.5–1.8 GB，而 Geofabrik 单线程实测只有 **0.03 MB/s**（要 16 小时）。
改成分块并发后实测能到 **8.5 MB/s** 量级。下载中途断线很常见，
所以每个分块的进度都记在 `.state.json` 里，重跑自动续传。

    from dizhi.ingest import osm
    osm.fetch("china", "china.osm.pbf", conns=16)
    # 或者给一个完整 URL
    osm.download("https://.../china-latest.osm.pbf", "china.osm.pbf")
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dizhi.settings import GEOFABRIK_BASE

UA = {"User-Agent": "dizhi/0.1 (OSM bulk download)"}
CHUNK = 1024 * 1024

# Geofabrik 上的中国相关包（子区域名 → 路径）
REGIONS = {
    "china": "asia/china-latest.osm.pbf",
    "taiwan": "asia/taiwan-latest.osm.pbf",
    "japan": "asia/japan-latest.osm.pbf",
    "hong-kong": "asia/china/hong-kong-latest.osm.pbf",
    "macau": "asia/china/macau-latest.osm.pbf",
}


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def region_url(region: str, base: str = "") -> str:
    """把区域名（如 'china'）或 Geofabrik 路径换成完整下载地址。"""
    root = (base or GEOFABRIK_BASE).rstrip("/")
    if region.startswith("http"):
        return region
    path = REGIONS.get(region, region)
    return f"{root}/{path}"


def remote_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        cl = resp.headers.get("Content-Length")
        if not cl:
            raise RuntimeError("服务端未返回 Content-Length，无法分块下载")
        return int(cl)


class Downloader:
    """分块并发 + 断点续传的下载器。"""

    def __init__(self, url: str, dest: str, conns: int = 16, quiet: bool = False):
        self.url = url
        self.dest = dest
        self.conns = max(1, conns)
        self.quiet = quiet
        self.total = remote_size(url)
        self.state_path = dest + ".state.json"
        self.lock = threading.Lock()
        self.stop = False

        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                self.state = json.load(fh)
            if not quiet:
                done = sum(self.state["parts"].values())
                print(f"[续传] 已完成 {human(done)}")
        else:
            self.state = {"total": self.total, "size": self.total,
                          "parts": {}, "chunk": max(self.total // self.conns, 1)}
            with open(dest, "wb") as fh:
                fh.truncate(self.total)      # 预分配，各线程按偏移写入
            self._save()

        self.chunk = self.state["chunk"]
        self.ranges: list[tuple[str, int, int]] = []
        start = 0
        while start < self.total:
            end = min(start + self.chunk - 1, self.total - 1)
            key = str(len(self.ranges))
            self.ranges.append((key, start, end))
            self.state["parts"].setdefault(key, 0)
            start = end + 1

    def _save(self) -> None:
        with self.lock:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)

    def _worker(self, key: str, start: int, end: int) -> None:
        offset = start + self.state["parts"][key]
        attempts = 0
        while offset <= end and not self.stop:
            try:
                req = urllib.request.Request(
                    self.url, headers={**UA, "Range": f"bytes={offset}-{end}"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    with open(self.dest, "r+b") as fh:
                        fh.seek(offset)
                        while offset <= end:
                            buf = resp.read(CHUNK)
                            if not buf:
                                break
                            fh.write(buf)
                            offset += len(buf)
                            with self.lock:
                                self.state["parts"][key] += len(buf)
                attempts = 0
            except Exception as exc:  # noqa: BLE001
                attempts += 1
                if attempts > 30:
                    if not self.quiet:
                        print(f"  [分块 {key}] 重试 30 次仍失败：{exc}")
                    return
                time.sleep(min(2 * attempts, 20))
        self._save()

    def run(self) -> int:
        todo = [(k, s, e) for k, s, e in self.ranges
                if self.state["parts"][k] < (e - s + 1)]
        if not todo:
            if not self.quiet:
                print("[跳过] 已完整")
            return 0
        if not self.quiet:
            print(f"[开始] 共 {human(self.total)}，剩 {len(todo)} 块，{self.conns} 线程")

        idx = 0
        cursor = threading.Lock()

        def next_part():
            nonlocal idx
            with cursor:
                if idx >= len(todo):
                    return None
                p = todo[idx]
                idx += 1
                return p

        def loop():
            while not self.stop:
                p = next_part()
                if p is None:
                    return
                self._worker(*p)

        threads = [threading.Thread(target=loop, daemon=True)
                   for _ in range(self.conns)]
        t0 = time.time()
        for t in threads:
            t.start()
        last = 0
        while any(t.is_alive() for t in threads):
            time.sleep(10)
            done = sum(self.state["parts"].values())
            if done == last or self.quiet:
                continue
            last = done
            rate = done / max(time.time() - t0, 1e-6)
            eta = (self.total - done) / rate if rate > 0 else 0
            print(f"  {done / self.total * 100:5.1f}%  "
                  f"{human(done)}/{human(self.total)}  "
                  f"{rate / 1024 / 1024:.2f} MB/s  ETA {eta / 60:.0f} 分钟",
                  flush=True)
        for t in threads:
            t.join()

        done = sum(self.state["parts"].values())
        if done < self.total:
            if not self.quiet:
                print(f"[未完成] {human(done)}/{human(self.total)}（重跑可续传）")
            return 1
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        if not self.quiet:
            print(f"[完成] {self.dest} {human(os.path.getsize(self.dest))} "
                  f"用时 {(time.time() - t0) / 60:.1f} 分钟")
        return 0


def download(url: str, dest: str, conns: int = 16, quiet: bool = False) -> int:
    """下载任意大文件（分块并发 + 续传）。"""
    return Downloader(url, dest, conns, quiet).run()


def fetch(region: str, dest: str, conns: int = 16,
          base: str = "", quiet: bool = False) -> int:
    """从 Geofabrik 下载指定区域的 OSM 包。"""
    url = region_url(region, base)
    if not quiet:
        print(f"[源] {url}")
        print(f"[目标] {dest}")
    return download(url, dest, conns, quiet)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="下载 OSM 数据（分块并发 + 断点续传）")
    ap.add_argument("--region", default="china",
                    help=f"Geofabrik 区域名，可选：{', '.join(REGIONS)}")
    ap.add_argument("--url", default="", help="也可以直接给完整 URL（优先）")
    ap.add_argument("--dest", required=True, help="保存路径，如 china.osm.pbf")
    ap.add_argument("--conns", type=int, default=16, help="并发连接数")
    a = ap.parse_args()
    if a.url:
        return download(a.url, a.dest, a.conns)
    return fetch(a.region, a.dest, a.conns)


if __name__ == "__main__":
    raise SystemExit(main())
