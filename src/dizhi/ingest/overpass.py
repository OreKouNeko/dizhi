"""把 Overpass 取回的 JSON 转成精简的 elements.parquet。

为什么要有这一步：
    Overpass 的 JSON 里几何是逐点嵌套的，直接查很慢。转成带 WKB 的列式
    Parquet 后，DuckDB 能按标签/范围秒级过滤，几何列再交给 shapely 使用。

输出表 elements 的列：
    osm_type / osm_id   要素身份（可追溯到 OSM，便于人工复核）
    layer               poi | area | line
    kind                归一化主类别，如 waterway=river、landuse=industrial
    name                名称（中文名在 name 标签里）
    tags                全部标签的 JSON（已剔除无意义的元数据标签）
    geom_type           Point | LineString | Polygon | MultiLineString
    wkb                 几何（WKB 二进制），shapely 直接可读
    clon / clat         几何中心（用于显示与粗筛）
    minx/miny/maxx/maxy 外包框（用于粗筛）

用法:
    python -m dizhi.ingest.overpass \
        --poi data/raw/nanjing_poi.json --line data/raw/nanjing_line.json \
        --out build/elements.parquet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import duckdb
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import unary_union

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))

from dizhi.config import (  # noqa: E402
    AREA_KEYS, INTERESTING_KEYS, KIND_PRIORITY, LINE_KEYS, is_blocked,
)

SCHEMA = """
CREATE TABLE elements (
    osm_type   VARCHAR,
    osm_id     BIGINT,
    layer      VARCHAR,
    kind       VARCHAR,
    name       VARCHAR,
    tags       VARCHAR,
    geom_type  VARCHAR,
    wkb        BLOB,
    clon       DOUBLE,
    clat       DOUBLE,
    minx       DOUBLE,
    miny       DOUBLE,
    maxx       DOUBLE,
    maxy       DOUBLE
)
"""


def clean_tags(tags: dict | None) -> dict:
    if not tags:
        return {}
    return {k: v for k, v in tags.items() if not is_blocked(k)}


def kind_of(tags: dict) -> str:
    for key in KIND_PRIORITY:
        if key in tags:
            return f"{key}={tags[key]}"
    return "other"


def is_interesting(tags: dict) -> bool:
    return any(k in INTERESTING_KEYS for k in tags)


def coords_of(geometry: list | None) -> list[tuple[float, float]]:
    if not geometry:
        return []
    return [(p["lon"], p["lat"]) for p in geometry if p and "lon" in p and "lat" in p]


def build_way_geom(coords: list[tuple[float, float]], tags: dict):
    if len(coords) < 2:
        return None
    closed = len(coords) >= 4 and coords[0] == coords[-1]
    areaish = any(k in AREA_KEYS for k in tags)
    if closed and areaish:
        try:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                return None
            return poly
        except Exception:  # noqa: BLE001
            return None
    try:
        return LineString(coords)
    except Exception:  # noqa: BLE001
        return None


def build_relation_geom(element: dict):
    """把 relation 的成员几何合并成一条线或一个面（如长江、行政边界）。"""
    parts = []
    for member in element.get("members", []) or []:
        coords = coords_of(member.get("geometry"))
        if len(coords) < 2:
            continue
        try:
            parts.append(LineString(coords))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return None
    merged = unary_union(parts)
    if merged.is_empty:
        return None
    if merged.geom_type == "LineString":
        return merged
    return merged


def classify(element: dict, tags: dict, geom) -> str:
    """判定要素属于哪一层。

    注意两点：
    1. 用 `out center` 取回时面要素只有一个中心点，几何类型是 Point，
       不能因此当成线——要结合标签判断。
    2. 公路/铁路/河道本身就是线，即使几何退化成中心点也不算"面"，
       否则"宁镇线"这种公路会挤进候选池，把真正的目标挤下去。
    """
    etype = element.get("type")
    if etype == "node":
        return "poi"

    hw = tags.get("highway")
    if hw and hw not in ("bus_stop", "motorway_junction", "services", "rest_area", "crossing", "ford"):
        return "line"
    if tags.get("railway") in ("rail", "tram", "light_rail", "subway", "narrow_gauge",
                               "monorail", "funicular"):
        return "line"
    if tags.get("waterway") in ("river", "stream", "canal", "drain", "ditch", "rapids"):
        return "line"
    if tags.get("power") in ("line", "minor_line", "cable"):
        return "line"

    if etype == "relation":
        return "area" if tags.get("type") == "multipolygon" else "line"
    if geom is not None and geom.geom_type in ("Polygon", "MultiPolygon"):
        return "area"
    if geom is not None and geom.geom_type == "Point":
        return "area"
    return "line"


def convert(poi_path: str | None, line_path: str | None, out_path: str) -> dict:
    rows: list[tuple] = []
    stats = {"poi": 0, "area": 0, "line": 0, "skipped": 0}

    for path, need_geometry in ((poi_path, True), (line_path, True)):
        if not path or not os.path.exists(path):
            continue
        print(f"[read] {path}")
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        for el in payload.get("elements", []):
            tags = clean_tags(el.get("tags"))
            if not is_interesting(tags):
                stats["skipped"] += 1
                continue

            etype = el.get("type")
            geom = None
            if etype == "node":
                if "lat" in el and "lon" in el:
                    geom = Point(el["lon"], el["lat"])
            elif etype == "way":
                coords = coords_of(el.get("geometry"))
                if coords:
                    geom = build_way_geom(coords, tags)
                elif "center" in el and el["center"]:
                    geom = Point(el["center"]["lon"], el["center"]["lat"])
            elif etype == "relation":
                geom = build_relation_geom(el)
                if geom is None and "center" in el and el["center"]:
                    geom = Point(el["center"]["lon"], el["center"]["lat"])

            if geom is None or geom.is_empty:
                stats["skipped"] += 1
                continue

            layer = classify(el, tags, geom)
            stats[layer] += 1
            cx, cy = geom.centroid.x, geom.centroid.y
            minx, miny, maxx, maxy = geom.bounds
            rows.append((
                etype, int(el["id"]), layer, kind_of(tags),
                (tags.get("name") or tags.get("name:zh") or None),
                json.dumps(tags, ensure_ascii=False),
                geom.geom_type, geom.wkb, cx, cy, minx, miny, maxx, maxy,
            ))

    print(f"[rows] {len(rows)}  {stats}")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    con = duckdb.connect()
    con.execute(SCHEMA)
    if rows:
        con.executemany(
            "INSERT INTO elements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute(
        f"COPY elements TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    n = con.execute("SELECT count(*) FROM elements").fetchone()[0]
    size = os.path.getsize(out_path) / 1024 / 1024
    print(f"[done] {out_path}  rows={n}  {size:.1f} MB")
    con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poi", default="data/raw/nanjing_poi.json")
    ap.add_argument("--line", default="data/raw/nanjing_line.json")
    ap.add_argument("--out", default="build/elements.parquet")
    args = ap.parse_args()
    t0 = time.time()
    convert(args.poi, args.line, args.out)
    print(f"[time] {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
