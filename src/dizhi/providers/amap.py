"""高德开放平台 Web 服务客户端。

只做一件事：**按关键字做 POI 检索**，其中最有价值的是按行政区逐个搜过去。

为什么必须按城市遍历：高德的 `place/text` 在全国范围直接搜只返回极少量结果
（实测搜一个具体学院名，全国不限定城市只回 1 条；按 341 个地级市逐个搜才拿全）。
另外高德是**模糊匹配**——搜"智能建造学院"会返回"人工智能学院"，
所以拿到结果后要按名称自己再筛一遍。

个人 key 的并发限制很紧（撞到会返回 `CUQPS_HAS_EXCEEDED_THE_LIMIT`），
所以内置了限速和退避重试。

坐标注意：高德返回 **GCJ-02**，与 WGS-84 有 50~500 米偏移，
要和 OSM / 天地图叠图必须先转换（见 `gcj02_to_wgs84`）。
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from dizhi.settings import AMAP_BASE, AMAP_KEY

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 全国地级行政区 adcode（覆盖 341 个）。要精确到区县可以再往下展开。
NATIONAL_CITIES = [
    "110000", "120000",
    "130100", "130200", "130300", "130400", "130500", "130600", "130700",
    "130800", "130900", "131000", "131100",
    "140100", "140200", "140300", "140400", "140500", "140600", "140700",
    "140800", "140900", "141000", "141100",
    "150100", "150200", "150300", "150400", "150500", "150600", "150700",
    "150800", "150900",
    "210100", "210200", "210300", "210400", "210500", "210600", "210700",
    "210800", "210900", "211000", "211100", "211200", "211300", "211400",
    "220100", "220200", "220300", "220400", "220500", "220600", "220700",
    "220800", "222400",
    "230100", "230200", "230300", "230400", "230500", "230600", "230700",
    "230800", "230900", "231000", "231100", "231200", "232700",
    "310000",
    "320100", "320200", "320300", "320400", "320500", "320600", "320700",
    "320800", "320900", "321000", "321100", "321200", "321300",
    "330100", "330200", "330300", "330400", "330500", "330600", "330700",
    "330800", "330900", "331000", "331100",
    "340100", "340200", "340300", "340400", "340500", "340600", "340700",
    "340800", "341000", "341100", "341200", "341300", "341500", "341600",
    "341700", "341800",
    "350100", "350200", "350300", "350400", "350500", "350600", "350700",
    "350800", "350900",
    "360100", "360200", "360300", "360400", "360500", "360600", "360700",
    "360800", "360900", "361000", "361100",
    "370100", "370200", "370300", "370400", "370500", "370600", "370700",
    "370800", "370900", "371000", "371100", "371300", "371400", "371500",
    "371600", "371700",
    "410100", "410200", "410300", "410400", "410500", "410600", "410700",
    "410800", "410900", "411000", "411100", "411200", "411300", "411400",
    "411500", "411600", "411700", "419001",
    "420100", "420200", "420300", "420500", "420600", "420700", "420800",
    "420900", "421000", "421100", "421200", "421300", "422800",
    "430100", "430200", "430300", "430400", "430500", "430600", "430700",
    "430800", "430900", "431000", "431100", "431200", "431300", "433100",
    "440100", "440200", "440300", "440400", "440500", "440600", "440700",
    "440800", "440900", "441200", "441300", "441400", "441500", "441600",
    "441700", "441800", "441900", "442000", "445100", "445200", "445300",
    "450100", "450200", "450300", "450400", "450500", "450600", "450700",
    "450800", "450900", "451000", "451100", "451200", "451300", "451400",
    "460100", "460200", "460300", "460400",
    "500000",
    "510100", "510300", "510400", "510500", "510600", "510700", "510800",
    "510900", "511000", "511100", "511300", "511400", "511500", "511600",
    "511700", "511800", "511900", "512000", "513200", "513300", "513400",
    "520100", "520200", "520300", "520400", "520500", "520600", "522300",
    "522600", "522700",
    "530100", "530300", "530400", "530500", "530600", "530700", "530800",
    "530900", "532300", "532500", "532600", "532800", "532900", "533100",
    "533300", "533400",
    "540100", "540200", "540300", "540400", "540500", "540600", "542100",
    "542200", "542300", "542400", "542500", "542600",
    "610100", "610200", "610300", "610400", "610500", "610600", "610700",
    "610800", "610900", "611000",
    "620100", "620200", "620300", "620400", "620500", "620600", "620700",
    "620800", "620900", "621000", "621100", "621200", "622900", "623000",
    "630100", "630200", "632200", "632300", "632500", "632600", "632700",
    "632800", "632801",
    "640100", "640200", "640300", "640400", "640500",
    "650100", "650200", "650400", "650500", "652300", "652700", "652800",
    "652900", "653000", "653100", "653200", "654000", "654200", "654300",
]

# ---- GCJ-02 -> WGS-84（高德坐标转标准经纬度）----
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _t_lat(x: float, y: float) -> float:
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi)
          + 20.0 * math.sin(2.0 * x * math.pi)) * 2 / 3
    r += (20.0 * math.sin(y * math.pi)
          + 40.0 * math.sin(y / 3.0 * math.pi)) * 2 / 3
    r += (160.0 * math.sin(y / 12.0 * math.pi)
          + 320.0 * math.sin(y * math.pi / 30.0)) * 2 / 3
    return r


def _t_lng(x: float, y: float) -> float:
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi)
          + 20.0 * math.sin(2.0 * x * math.pi)) * 2 / 3
    r += (20.0 * math.sin(x * math.pi)
          + 40.0 * math.sin(x / 3.0 * math.pi)) * 2 / 3
    r += (150.0 * math.sin(x / 12.0 * math.pi)
          + 300.0 * math.sin(x / 30.0 * math.pi)) * 2 / 3
    return r


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """高德/腾讯坐标 -> WGS-84。与 OSM、天地图叠图前必须调用。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _t_lat(lng - 105.0, lat - 35.0)
    dlng = _t_lng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sm) * math.pi)
    dlng = (dlng * 180.0) / (_A / sm * math.cos(rad) * math.pi)
    return lng - dlng, lat - dlat


class AmapClient:
    """高德 Web 服务客户端。只需一个"Web服务"类型的 key。"""

    def __init__(self, key: str = "", base: str = "", sleep: float = 0.55):
        self.key = (key or AMAP_KEY).strip()
        self.base = (base or AMAP_BASE).rstrip("/")
        self.sleep = sleep

    # ---------------------------------------------------------------- 底层
    def _get(self, path: str, **params) -> dict:
        params["key"] = self.key
        url = f"{self.base}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _get_retry(self, path: str, tries: int = 4, **params) -> dict:
        """撞到 QPS 限流就退避重试。个人 key 的并发限制很紧。"""
        for attempt in range(tries):
            try:
                d = self._get(path, **params)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1.0 + attempt)
                continue
            info = d.get("info") or ""
            if "CUQPS" in info or "OVER_LIMIT" in info:
                time.sleep(1.5 * (attempt + 1))
                continue
            return d
        return {"status": "0", "info": "限流重试失败", "pois": []}

    # ---------------------------------------------------------------- 检索
    def search(self, keyword: str, city: str = "", page: int = 1,
               offset: int = 25, citylimit: bool = True) -> list[dict]:
        """一次关键字检索，返回该页 POI 原始列表。"""
        params = {"keywords": keyword, "offset": offset, "page": page,
                  "extensions": "base"}
        if city:
            params["city"] = city
            params["citylimit"] = "true" if citylimit else "false"
        d = self._get_retry("place/text", **params)
        if d.get("status") != "1":
            raise RuntimeError(f"高德接口报错：{d.get('info')} ({d.get('infocode')})")
        return d.get("pois") or []

    def search_city(self, keyword: str, city: str, max_pages: int = 5) -> list[dict]:
        """把一个城市的某关键字翻页取全。高德是模糊匹配，一页常装不下。"""
        out: list[dict] = []
        for p in range(1, max_pages + 1):
            pois = self.search(keyword, city=city, page=p)
            time.sleep(self.sleep)
            out.extend(pois)
            if len(pois) < 25:
                break
        return out

    def search_nationwide(self, keyword: str, cities: list[str] | None = None,
                          exact: bool = True, progress=None) -> list[dict]:
        """按行政区逐个城市搜过去，全国取全。

        `exact=True` 时只保留名称里含关键字的（过滤掉模糊匹配的噪声，
        比如搜"智能建造学院"会带出"人工智能学院"）。
        返回统一的 dict 结构：name / address / location / city / district / gcj02 / wgs84。
        """
        cities = cities or NATIONAL_CITIES
        seen: set[str] = set()
        out: list[dict] = []
        for i, city in enumerate(cities, 1):
            try:
                pois = self.search_city(keyword, city)
            except Exception as exc:  # noqa: BLE001
                if progress:
                    progress(i, len(cities), f"{city} 跳过：{exc}")
                continue
            for p in pois:
                name = p.get("name") or ""
                if exact and keyword not in name:
                    continue
                loc = p.get("location") or ""
                pid = p.get("id") or f"{name}|{loc}"
                if pid in seen:
                    continue
                seen.add(pid)
                item = {"name": name, "address": p.get("address") or "",
                        "location": loc, "city": p.get("cityname") or "",
                        "district": p.get("adname") or "",
                        "typecode": p.get("typecode") or "",
                        "gcj02": None, "wgs84": None}
                try:
                    lng, lat = (float(v) for v in loc.split(","))
                    item["gcj02"] = [lng, lat]
                    item["wgs84"] = list(gcj02_to_wgs84(lng, lat))
                except (ValueError, AttributeError):
                    pass
                out.append(item)
            if progress:
                progress(i, len(cities), f"累计 {len(out)} 条")
        return out
