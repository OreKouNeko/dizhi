"""天地图（国家地理信息公共服务平台）客户端。

两个用途：
  1. **地名搜索** —— `TiandituClient.search(keyword)`
  2. **瓦片地址** —— `TiandituClient.tile_url(layer)`，给 folium/Leaflet 当底图

天地图最大的好处是坐标系为 **CGCS2000，与 WGS-84 基本重合，没有 GCJ-02 偏移**，
而且国内直连速度快。

### 两个必踩的坑

1. **服务器端 key 强制要求 Referer。** 用服务器端 key 请求任何接口（含瓦片），
   如果请求头没有 `Referer: http://lbs.tianditu.gov.cn/`，
   会返回 403 `{"code":301013,"msg":"权限类型错误"}`。
   浏览器打开本地 HTML 时 Referer 是 `file:///...`，且 **JS 无法伪造 Referer**，
   所以天地图底图在本地文件里必然是白图。
   解决办法：用 `tools/serve_map.py` 起一个本地代理补上 Referer；
   或者在控制台再申请一个**浏览器端**类型的 key 专供网页底图。

2. **限流很紧。** 短时间连发会返回 429 `{"code":302010,"msg":"该tk已限流"}`，
   请求之间至少要间隔 2 秒以上。

搜索结果里的 `poiType` 与高德不是一套，别混用。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from dizhi.settings import TIANDITU_BASE, TIANDITU_REFERER, TIANDITU_TK

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 可用图层：img=影像底图 cia=影像注记 vec=矢量底图 cva=矢量注记
LAYERS = {"img": "影像底图", "cia": "影像注记",
          "vec": "矢量底图", "cva": "矢量注记"}

# 全国范围的视图包围框，作为 mapBound 的默认值（天地图按视图范围检索）
CHINA_BOUND = "73,3,136,54"


class TiandituClient:
    def __init__(self, tk: str = "", base: str = "",
                 referer: str = "", sleep: float = 2.5):
        self.tk = (tk or TIANDITU_TK).strip()
        self.base = (base or TIANDITU_BASE).rstrip("/")
        self.referer = referer or TIANDITU_REFERER
        self.sleep = sleep

    # ---------------------------------------------------------------- 搜索
    def search(self, keyword: str, start: int = 0, count: int = 300,
               map_bound: str = CHINA_BOUND, level: str = "12",
               query_type: str = "1") -> list[dict]:
        """地名搜索。返回 POI 列表（含 name / address / lonlat）。"""
        post = {"keyWord": keyword, "queryType": query_type, "start": start,
                "count": count, "mapBound": map_bound, "level": level}
        url = (f"{self.base}/v2/search?postStr="
               + urllib.parse.quote(json.dumps(post, ensure_ascii=False))
               + "&type=query&tk=" + self.tk)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "*/*", "Referer": self.referer})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"天地图 HTTP {exc.code}: {body}") from exc
        return d.get("pois") or []

    def search_all(self, keyword: str, exact: bool = True) -> list[dict]:
        """反复翻页取全，并去重。天地图的 POI 总量远小于高德，通常几次就取完。"""
        seen, out = set(), []
        start = 0
        for _ in range(10):
            pois = self.search(keyword, start=start)
            if not pois:
                break
            new = 0
            for p in pois:
                name = p.get("name") or ""
                if exact and keyword not in name:
                    continue
                pid = p.get("hotPointID") or f"{name}|{p.get('lonlat')}"
                if pid in seen:
                    continue
                seen.add(pid)
                out.append({"name": name, "address": p.get("address") or "",
                            "location": p.get("lonlat") or "",
                            "poiType": p.get("poiType") or ""})
                new += 1
            if new == 0:
                break
            start += len(pois)
            time.sleep(self.sleep)
        return out

    # ---------------------------------------------------------------- 瓦片
    def tile_url(self, layer: str = "img") -> str:
        """给 folium/Leaflet 用的 WMTS 瓦片模板。

        注意：模板里的 `{s}` `{x}` `{y}` `{z}` 是给地图库替换的，
        不要拿它去做 str.format。
        """
        if layer not in LAYERS:
            raise ValueError(f"未知图层 {layer}，可选：{list(LAYERS)}")
        return ("https://t{s}.tianditu.gov.cn/" + layer + "_w/wmts"
                "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
                f"&LAYER={layer}&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles"
                "&TILECOL={x}&TILEROW={y}&TILEMATRIX={z}&tk=" + self.tk)

    def fetch_tile(self, layer: str, z: int, x: int, y: int) -> bytes:
        """直接取一张瓦片（服务器端场景用；会自动带上必需的 Referer）。"""
        url = (f"https://t0.tianditu.gov.cn/{layer}_w/wmts?SERVICE=WMTS"
               f"&REQUEST=GetTile&VERSION=1.0.0&LAYER={layer}&STYLE=default"
               f"&TILEMATRIXSET=w&FORMAT=tiles&TILECOL={x}&TILEROW={y}"
               f"&TILEMATRIX={z}&tk={self.tk}")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": self.referer})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
