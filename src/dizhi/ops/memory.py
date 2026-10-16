"""要素存储：把 elements.parquet 读进内存，投影到米制坐标，建空间索引。

为什么要投影：
    经纬度（WGS-84）下算出来的"距离"是角度不是米，直接用会得到离谱的结果。
    统一投影到 UTM（中国东部为 EPSG:326xx）后，距离单位就是米。

为什么要空间索引：
    "附近 3 公里内有化工厂"这种条件，如果对每个候选点都遍历全部要素，
    几十万次几何运算会非常慢。STRtree 是 R 树，能直接跳过绝大多数不相交的要素。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import duckdb
import numpy as np
from pyproj import Transformer
from shapely import wkb as shapely_wkb
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree


@dataclass
class Element:
    idx: int
    osm_type: str
    osm_id: int
    layer: str
    kind: str
    name: str | None
    tags: dict
    geom_ll: BaseGeometry          # 经纬度坐标（给输出用）
    geom_m: BaseGeometry           # 米制坐标（给计算用）
    clon: float
    clat: float

    @property
    def named(self) -> bool:
        return bool(self.name)

    @property
    def ref(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    def one_line(self) -> str:
        label = self.name or "(无名)"
        return f"{label} [{self.kind}] {self.clon:.5f},{self.clat:.5f} ({self.ref})"


def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


class Store:
    def __init__(self, parquet_path: str):
        self.path = parquet_path
        con = duckdb.connect()
        cols = con.execute(
            "SELECT osm_type, osm_id, layer, kind, name, tags, geom_type, wkb, "
            "clon, clat, minx, miny, maxx, maxy FROM read_parquet(?)",
            [parquet_path],
        ).fetchall()
        con.close()
        if not cols:
            raise SystemExit(f"empty parquet: {parquet_path}")

        lons = [row[8] for row in cols]
        lats = [row[9] for row in cols]
        self.center = (float(np.mean(lons)), float(np.mean(lats)))
        self.epsg = utm_epsg(*self.center)
        self.to_m = Transformer.from_crs("EPSG:4326", f"EPSG:{self.epsg}",
                                          always_xy=True).transform

        self.elements: list[Element] = []
        for i, row in enumerate(cols):
            geom_ll = shapely_wkb.loads(row[7])
            self.elements.append(Element(
                idx=i,
                osm_type=row[0], osm_id=int(row[1]), layer=row[2], kind=row[3],
                name=row[4], tags=json.loads(row[5]) if row[5] else {},
                geom_ll=geom_ll,
                geom_m=shapely_transform(self.to_m, geom_ll),
                clon=row[8], clat=row[9],
            ))

        # 全量树的坐标数组是共享的，按需在子集上再建小树
        self._all_geoms = [e.geom_m for e in self.elements]

    # ------------------------------------------------------------ 选择
    @staticmethod
    def match(tags: dict, filters: list[dict[str, str]] | None) -> bool:
        """filters 是一组 OR 条件，每个条件是 {key: value} 的 AND。"""
        if not filters:
            return True
        for f in filters:
            if all(tags.get(k) == v for k, v in f.items()):
                return True
        return False

    def select(self, filters=None, layer=None, named_only=False,
               name_contains=None) -> list[int]:
        out = []
        for e in self.elements:
            if layer and e.layer != layer:
                continue
            if named_only and not e.named:
                continue
            if name_contains and name_contains not in (e.name or ""):
                continue
            if not self.match(e.tags, filters):
                continue
            out.append(e.idx)
        return out

    # ------------------------------------------------------------ 空间
    def tree(self, idxs: list[int]) -> STRtree:
        return STRtree([self.elements[i].geom_m for i in idxs])

    def within_any(self, qgeom: BaseGeometry, idxs: list[int], dist_m: float) -> bool:
        """qgeom 与 idxs 中任一要素的距离是否 <= dist_m。"""
        if not idxs:
            return False
        tree = self.tree(idxs)
        hits = tree.query(qgeom, predicate="dwithin", distance=dist_m)
        return len(hits) > 0

    def min_distance(self, qgeom: BaseGeometry, idxs: list[int],
                     search_radius: float = 60000.0) -> float:
        """qgeom 到 idxs 中最近要素的距离（米）；超出搜索半径返回 inf。"""
        if not idxs:
            return float("inf")
        tree = self.tree(idxs)
        hits = tree.query(qgeom, predicate="dwithin", distance=search_radius)
        if len(hits) == 0:
            return float("inf")
        return min(qgeom.distance(self.elements[idxs[int(h)]].geom_m) for h in hits)

    def union_geometry(self, idxs: list[int]) -> BaseGeometry | None:
        if not idxs:
            return None
        from shapely.ops import unary_union
        return unary_union([self.elements[i].geom_m for i in idxs])

    # ------------------------------------------------------------ 统计
    def summary(self) -> str:
        from collections import Counter
        by_layer = Counter(e.layer for e in self.elements)
        by_kind = Counter(e.kind for e in self.elements)
        named = sum(1 for e in self.elements if e.named)
        lines = [
            f"要素总数 {len(self.elements)}  其中有名 {named}",
            f"分层: {dict(by_layer)}",
            f"投影: EPSG:{self.epsg}  中心: {self.center[0]:.4f},{self.center[1]:.4f}",
            "类别 Top15:",
        ]
        for kind, n in by_kind.most_common(15):
            lines.append(f"    {kind:34s} {n}")
        return "\n".join(lines)
