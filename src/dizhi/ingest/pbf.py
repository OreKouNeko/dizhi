"""全国 OSM PBF -> Parquet，不用稀疏节点索引的快速版。

为什么要另写一版（实测教训）：
    pyosmium 的 `apply_file(locations=True)` 会建一个按「节点 ID」寻址的稀疏文件。
    OSM 节点 ID 是全球统一编号，中国的节点散落在 0 到 120 亿之间，稀疏文件的块
    分配因此极其零散。实测：第一个 40 万行分片只用了 3.6 分钟，第二个就要 23 分钟，
    按这个速率全国要跑十几个小时。瓶颈不是解析，而是随机写。

本方案改用三次**顺序扫描**，全程没有随机写：

    第 1 遍  读节点和 way。节点（POI）自带坐标，直接产出；way 的标签 + 节点引用
             写进临时文件，同时把所有被引用的节点 ID 追加到一个二进制文件。
             —— 这一步已经能产出绝大部分数据（POI 占大头）。
    第 2 遍  把节点 ID 文件排序去重（numpy），再扫一遍节点。因为 PBF 里节点是按
             ID 升序排列的，而我们的目标 ID 也已排序，所以用一个**前进指针**做归并
             即可，每个节点 O(1)，不需要二分查找。命中的坐标按位存进数组。
    第 3 遍  读第 1 遍存下的 way 定义，用 numpy 的 searchsorted 批量查坐标，
             拼几何、写 Parquet。

内存占用只跟「被 way 引用的节点数」有关（约几亿字节量级），与全国总量无关；
磁盘临时文件用完即删。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import numpy as np
import osmium
import pyarrow as pa
import pyarrow.parquet as pq
from osmium.filter import KeyFilter
from shapely import wkb as swkb
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))

from dizhi.config import (  # noqa: E402
    AREA_KEYS, BUILDING_KEY, INTERESTING_KEYS, KEY_FILTER_KEYS, KIND_PRIORITY,
    is_blocked,
)

SCHEMA = pa.schema([
    ("osm_type", pa.string()), ("osm_id", pa.int64()), ("layer", pa.string()),
    ("kind", pa.string()), ("name", pa.string()), ("tags", pa.string()),
    ("geom_type", pa.string()), ("wkb", pa.binary()),
    ("clon", pa.float64()), ("clat", pa.float64()),
    ("minx", pa.float64()), ("miny", pa.float64()),
    ("maxx", pa.float64()), ("maxy", pa.float64()),
])

LINE_HIGHWAY = {"motorway", "trunk", "primary", "secondary", "tertiary"}
LINE_RAILWAY = {"rail", "tram", "light_rail", "subway", "narrow_gauge", "monorail", "funicular"}
LINE_WATERWAY = {"river", "stream", "canal", "drain", "ditch", "rapids"}
LINE_POWER = {"line", "minor_line", "cable"}
REL_TYPES = {"multipolygon", "waterway"}

FLUSH_ROWS = 400_000
ID_BUFFER = 8_000_000          # 节点 ID 写入缓冲（个），约 64 MB

# 关键优化：用 pyosmium 的 C++ 层过滤器先把无关要素挡掉。
# 全国有大约三亿个节点，如果每个都回调进 Python 再判断标签，光回调开销就要十几分钟；
# 交给 C++ 过滤后，只有带目标标签的要素才会进入我们的回调。
KEY_FILTER = KeyFilter(*sorted(KEY_FILTER_KEYS))


def clean_tags(tags) -> dict:
    out = {}
    for t in tags:
        k = t.k
        if is_blocked(k):
            continue
        out[k] = t.v
    return out


def kind_of(tags: dict) -> str:
    for key in KIND_PRIORITY:
        if key in tags:
            return f"{key}={tags[key]}"
    return "other"


def is_interesting(tags: dict) -> bool:
    """判断这个要素值不值得收。

    建筑特殊处理：中国有上千万栋建筑轮廓，全收进来会把数据撑爆；
    但"4号楼""图书馆""物流中心""实验楼"这类**带名字的建筑**恰恰是校园里
    最有辨识度的结构，全国只有几十万个，体量完全可控。所以建筑要求必须有名字。
    """
    if any(k in INTERESTING_KEYS for k in tags):
        return True
    if BUILDING_KEY in tags:
        return bool(tags.get("name") or tags.get("name:zh"))
    return False


def layer_of(tags: dict, refs: list[int]) -> str:
    if tags.get("highway") in LINE_HIGHWAY:
        return "line"
    if tags.get("railway") in LINE_RAILWAY:
        return "line"
    if tags.get("waterway") in LINE_WATERWAY:
        return "line"
    if tags.get("power") in LINE_POWER:
        return "line"
    closed = len(refs) >= 4 and refs[0] == refs[-1]
    if closed:
        return "area"
    return "line"


class Writer:
    """攒够一批就写一个 Parquet 分片。

    续跑时（--skip-pass1）必须从已有分片的编号之后接着写，否则会把上一步
    辛苦产出的分片直接覆盖掉。
    """

    def __init__(self, outdir: str, prefix: str = "part"):
        self.outdir = outdir
        self.prefix = prefix
        os.makedirs(outdir, exist_ok=True)
        self.buf: list[tuple] = []
        self.n = 0
        self.rows = 0
        self.t0 = time.time()
        # 找出现有分片的最大编号，接着往下写
        existing = [f for f in os.listdir(outdir)
                    if f.startswith(f"{prefix}-") and f.endswith(".parquet")]
        for f in existing:
            try:
                self.n = max(self.n, int(f[len(prefix) + 1:-8]) + 1)
            except ValueError:
                continue
        if existing:
            print(f"  [writer] 已有 {len(existing)} 个分片，从 {prefix}-{self.n:04d} 继续",
                  flush=True)

    def add(self, etype, eid, layer, tags, geom) -> None:
        cx, cy = geom.centroid.x, geom.centroid.y
        minx, miny, maxx, maxy = geom.bounds
        tags = {k: v for k, v in tags.items() if k not in ("__layer__",)}
        self.buf.append((
            etype, int(eid), layer, kind_of(tags),
            tags.get("name") or tags.get("name:zh") or None,
            json.dumps(tags, ensure_ascii=False),
            geom.geom_type, geom.wkb, cx, cy, minx, miny, maxx, maxy,
        ))
        if len(self.buf) >= FLUSH_ROWS:
            self.flush()

    def add_member(self, way_id: int, geom) -> None:
        """relation 成员 way 只需要 id 和几何，不需要标签。"""
        minx, miny, maxx, maxy = geom.bounds
        self.buf.append((
            "way", int(way_id), "member", "", None, "{}",
            geom.geom_type, geom.wkb, geom.centroid.x, geom.centroid.y,
            minx, miny, maxx, maxy,
        ))
        if len(self.buf) >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.buf:
            return
        cols = list(zip(*self.buf))
        table = pa.Table.from_pydict(
            {SCHEMA.field(i).name: list(cols[i]) for i in range(len(SCHEMA))},
            schema=SCHEMA)
        path = os.path.join(self.outdir, f"{self.prefix}-{self.n:04d}.parquet")
        pq.write_table(table, path, compression="zstd", row_group_size=200_000)
        self.rows += len(self.buf)
        self.n += 1
        print(f"  [flush] {os.path.basename(path)} rows={len(self.buf)} "
              f"total={self.rows}  {time.time() - self.t0:.0f}s", flush=True)
        self.buf.clear()


# ================================================================ 第 1 遍
class PassOne(osmium.SimpleHandler):
    def __init__(self, writer: Writer, ways_fh, ids_fh, rels_fh):
        super().__init__()
        self.w = writer
        self.ways_fh = ways_fh
        self.ids_fh = ids_fh
        self.rels_fh = rels_fh
        self.id_buf: list[int] = []
        self.stats = {"poi": 0, "ways": 0, "skipped": 0, "rel": 0}

    def _push_ids(self, refs) -> None:
        self.id_buf.extend(refs)
        if len(self.id_buf) >= ID_BUFFER:
            np.asarray(self.id_buf, dtype=np.int64).tofile(self.ids_fh)
            self.id_buf.clear()

    def node(self, n) -> None:
        if n.deleted:
            return
        tags = clean_tags(n.tags)
        if not is_interesting(tags):
            self.stats["skipped"] += 1
            return
        try:
            geom = Point(n.location.lon, n.location.lat)
        except Exception:  # noqa: BLE001
            return
        self.w.add("node", n.id, "poi", tags, geom)
        self.stats["poi"] += 1

    def way(self, w) -> None:
        if w.deleted:
            return
        tags = clean_tags(w.tags)
        if not is_interesting(tags):
            self.stats["skipped"] += 1
            return
        try:
            refs = [nd.ref for nd in w.nodes]
        except Exception:  # noqa: BLE001
            return
        if len(refs) < 2:
            return
        layer = layer_of(tags, refs)
        # 第 3 遍才拼几何，这里只把"配方"存下来
        self.ways_fh.write(json.dumps([int(w.id), tags, refs, layer],
                                      ensure_ascii=False) + "\n")
        self._push_ids(refs)
        self.stats["ways"] += 1

    def relation(self, r) -> None:
        if r.deleted:
            return
        tags = clean_tags(r.tags)
        if not is_interesting(tags) or tags.get("type") not in REL_TYPES:
            return
        ways = [m.ref for m in r.members if m.type == "w"]
        if not ways:
            return
        self.rels_fh.write(json.dumps([int(r.id), tags, ways], ensure_ascii=False) + "\n")
        self.stats["rel"] += 1

    def close(self) -> None:
        if self.id_buf:
            np.asarray(self.id_buf, dtype=np.int64).tofile(self.ids_fh)
            self.id_buf.clear()


# ================================================================ 第 1b 遍
class PassOneB(osmium.SimpleHandler):
    """按 way ID 精确捞出 relation 的成员 way。

    为什么必须单独走一遍：
        multipolygon 的成员 way **通常不带任何标签**——它们只是几何碎片，
        `amenity=university` 和名称都在 relation 上。第 1 遍按标签过滤时
        这些 way 全被跳过了，结果就是大学、机场、大湖这类关系型要素永远拼不出来。
    """

    def __init__(self, member_ids: np.ndarray, mem_fh, ids_fh):
        super().__init__()
        self.member_ids = member_ids
        self.mem_fh = mem_fh
        self.ids_fh = ids_fh
        self.id_buf: list[int] = []
        self.hit = 0
        self.scanned = 0
        self.t0 = time.time()

    def _push_ids(self, refs) -> None:
        self.id_buf.extend(refs)
        if len(self.id_buf) >= ID_BUFFER:
            np.asarray(self.id_buf, dtype=np.int64).tofile(self.ids_fh)
            self.id_buf.clear()

    def way(self, w) -> None:
        if w.deleted:
            return
        self.scanned += 1
        n = len(self.member_ids)
        if n == 0:
            return
        p = int(np.searchsorted(self.member_ids, w.id))
        if p >= n or int(self.member_ids[p]) != w.id:
            return
        try:
            refs = [nd.ref for nd in w.nodes]
        except Exception:  # noqa: BLE001
            return
        if len(refs) < 2:
            return
        self.mem_fh.write(json.dumps([int(w.id), refs]) + "\n")
        self._push_ids(refs)
        self.hit += 1
        if self.hit % 500_000 == 0:
            print(f"    ... 已扫 {self.scanned / 1e6:.0f}M way，命中成员 "
                  f"{self.hit / 1e6:.1f}M  [{time.time() - self.t0:.0f}s]", flush=True)

    def close(self) -> None:
        if self.id_buf:
            np.asarray(self.id_buf, dtype=np.int64).tofile(self.ids_fh)
            self.id_buf.clear()


def member_way_ids(rel_path: str) -> np.ndarray:
    """从 relation 定义文件里取出所有成员 way 的 ID（升序去重）。"""
    if not os.path.exists(rel_path) or os.path.getsize(rel_path) == 0:
        return np.empty(0, dtype=np.int64)
    ids: list[int] = []
    with open(rel_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                _, _, ways = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ids.extend(ways)
    if not ids:
        return np.empty(0, dtype=np.int64)
    return np.unique(np.asarray(ids, dtype=np.int64))


# ================================================================ 第 2 遍
def build_sorted_ids(ids_path: str, out_path: str, tmp_dir: str, t0: float) -> int:
    """把节点引用文件外排成"去重且升序"的 ID 表，全程内存占用有界。

    为什么不能直接 np.unique：全国被 way 引用的节点有一两亿个，int64 就是 1-2 GB，
    np.unique 还要再复制一份排序结果，峰值直接顶到 3 GB 以上。改成：
    分块各自排序落盘（每块只占 160 MB），再做 k 路归并，边归并边去重。
    """
    import heapq

    chunk_files: list[str] = []
    CH = 20_000_000
    with open(ids_path, "rb") as fh:
        i = 0
        while True:
            arr = np.fromfile(fh, dtype=np.int64, count=CH)
            if arr.size == 0:
                break
            arr.sort()
            p = os.path.join(tmp_dir, f"chunk-{i:03d}.bin")
            arr.tofile(p)
            chunk_files.append(p)
            i += 1
            del arr
    print(f"  [ids] {len(chunk_files)} sorted chunks  [{time.time() - t0:.0f}s]", flush=True)

    def reader(path: str):
        with open(path, "rb") as fh:
            while True:
                a = np.fromfile(fh, dtype=np.int64, count=1_000_000)
                if a.size == 0:
                    return
                for v in a:
                    yield int(v)

    total = 0
    last: int | None = None
    buf: list[int] = []
    with open(out_path, "wb") as fo:
        for v in heapq.merge(*(reader(p) for p in chunk_files)):
            if v == last:
                continue
            last = v
            buf.append(v)
            total += 1
            if len(buf) >= 5_000_000:
                np.asarray(buf, dtype=np.int64).tofile(fo)
                buf.clear()
        if buf:
            np.asarray(buf, dtype=np.int64).tofile(fo)
    for p in chunk_files:
        os.remove(p)
    print(f"  [ids] {total} unique ids  [{time.time() - t0:.0f}s]", flush=True)
    return total


def collect_coords(pbf: str, ids_path: str, tmp_dir: str, t0: float):
    """扫一遍节点，把目标节点的坐标按位置填进磁盘映射数组。

    PBF 里节点按 ID 升序，目标 ID 也已排序，所以用一个前进指针归并即可，
    每个节点摊还 O(1)。坐标写进 memmap 文件而不是内存数组，
    这样内存占用跟数据量无关（3 GB 上限下这是必须的）。
    """
    ids_file = os.path.join(tmp_dir, "ids.sorted")
    # 外排很贵（1.9 亿个 ID 要一百多秒），已经排好就直接复用
    if os.path.exists(ids_file) and os.path.getsize(ids_file) > 0:
        n = os.path.getsize(ids_file) // 8
        print(f"  [ids] 复用已排序的 {n} 个 ID  [{time.time() - t0:.0f}s]", flush=True)
    else:
        n = build_sorted_ids(ids_path, ids_file, tmp_dir, t0)
    if n == 0:
        raise SystemExit("no node ids collected")
    n_ids = int(n)          # 注意：不能叫 n，否则会被下面 node(self, n) 的参数遮蔽
    ids = np.memmap(ids_file, dtype=np.int64, mode="r", shape=(n_ids,))
    lons = np.memmap(os.path.join(tmp_dir, "lons.bin"), dtype=np.float32,
                     mode="w+", shape=(n_ids,))
    lats = np.memmap(os.path.join(tmp_dir, "lats.bin"), dtype=np.float32,
                     mode="w+", shape=(n_ids,))
    lons[:] = np.nan
    lats[:] = np.nan
    state = {"ptr": 0, "hit": 0, "last": 0, "scanned": 0}

    class PassTwo(osmium.SimpleHandler):
        def node(self, n) -> None:
            p = state["ptr"]
            if p >= n_ids:
                return
            nid = n.id
            state["scanned"] += 1
            if nid < state["last"]:          # 文件理论上有序，防御一下
                return
            state["last"] = nid
            if nid < ids[p]:
                return
            while p < n_ids and ids[p] < nid:
                p += 1
            state["ptr"] = p
            if p < n_ids and ids[p] == nid:
                try:
                    loc = n.location
                    lons[p] = loc.lon
                    lats[p] = loc.lat
                    state["hit"] += 1
                except Exception:  # noqa: BLE001
                    pass
            if state["scanned"] % 50_000_000 == 0:
                print(f"    ... 已扫描 {state['scanned'] / 1e6:.0f}M 节点，"
                      f"命中 {state['hit'] / 1e6:.1f}M  [{time.time() - t0:.0f}s]", flush=True)

    h = PassTwo()
    h.apply_file(pbf, locations=False)
    lons.flush()
    lats.flush()
    print(f"  [coords] scanned {state['scanned'] / 1e6:.0f}M nodes, "
          f"matched {state['hit'] / 1e6:.1f}M / {n_ids / 1e6:.1f}M  "
          f"[{time.time() - t0:.0f}s]", flush=True)
    return ids, lons, lats


# ================================================================ 第 3 遍
def build_ways(ways_path: str, ids, lons, lats, writer: Writer, t0: float,
               member: bool = False) -> None:
    """把 way 的"配方"还原成几何。

    member=True 时读的是 relation 成员 way（配方里只有 id 和节点引用，没有标签，
    标签在 relation 上）。
    """
    print(f"  [ways] building geometries ({'members' if member else 'tagged'}) ...  "
          f"[{time.time() - t0:.0f}s]", flush=True)
    n_done = 0
    with open(ways_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                if member:
                    wid, refs = json.loads(line)
                    tags, layer = {}, "line"
                else:
                    wid, tags, refs, layer = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            n_done += 1
            if n_done % 500_000 == 0:
                print(f"    ... {n_done / 1e6:.1f}M ways  [{time.time() - t0:.0f}s]", flush=True)
            if len(ids) == 0:
                break
            arr = np.asarray(refs, dtype=np.int64)
            pos = np.clip(np.searchsorted(ids, arr), 0, len(ids) - 1)
            good = ids[pos] == arr
            if good.sum() < 2:
                continue
            coords = np.column_stack([lons[pos[good]], lats[pos[good]]]).astype(np.float64)
            if member:
                # 成员 way 一律按线处理：环的闭合与成面交给后面的 ST_Polygonize
                try:
                    geom = LineString([tuple(p) for p in coords])
                except Exception:  # noqa: BLE001
                    continue
                writer.add_member(wid, geom)
                continue
            try:
                geom = _geom(coords, layer)
            except Exception:  # noqa: BLE001
                continue
            if geom is None or geom.is_empty:
                continue
            writer.add("way", wid, layer, tags, geom)


def _geom(coords, layer):
    pts = [tuple(p) for p in coords]
    if len(pts) < 2:
        return None
    if layer == "area" and pts[0] == pts[-1] and len(pts) >= 4:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        return LineString(pts) if poly.is_empty else poly
    return LineString(pts)


# ================================================================ relation
def assemble_relations(outdir: str, rel_path: str, mem_glob: str, tmp_dir: str,
                       t0: float) -> int:
    """用 DuckDB 把 relation 的成员几何拼起来，写出 rel-*.parquet。

    - multipolygon（大学、机场、大湖…）：先 ST_Union_Agg 合并成员线段，
      再 ST_Polygonize 成面。成员 way 普遍不带标签，所以必须靠第 1b 遍单独捞出来。
    - waterway（长江这类命名河流）：本来就是线，直接合并。

    聚合交给 DuckDB 做，内存由它自己按限额管理——几十万个多边形一次性
    在 Python 里 union 会直接爆掉（本机可用内存只有约 3 GB）。
    """
    if not os.path.exists(rel_path) or os.path.getsize(rel_path) == 0:
        print("[rels] none")
        return 0
    import duckdb

    meta_ids: list[int] = []
    meta_tags: list[str] = []
    meta_type: list[str] = []
    mem_rel: list[int] = []
    mem_way: list[int] = []
    with open(rel_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rid, tags, ways = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            meta_ids.append(int(rid))
            meta_tags.append(json.dumps(tags, ensure_ascii=False))
            meta_type.append(str(tags.get("type", "")))
            for w in ways:
                mem_rel.append(int(rid))
                mem_way.append(int(w))
    if not meta_ids:
        print("[rels] none")
        return 0
    print(f"[rels] {len(meta_ids)} relations, {len(mem_way)} member refs  "
          f"[{time.time() - t0:.0f}s]", flush=True)

    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute("SET memory_limit='2GB'")
    # 关掉"保持插入顺序"：8 万多个关系分组做 ST_Polygonize 时，
    # 光是维持结果顺序就会吃掉大量内存——实测开着它直接 OOM，
    # 关掉后同样的查询能跑完。
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA threads=2")
    con.execute(f"SET temp_directory='{tmp_dir.replace(chr(92), '/')}'")
    con.register("rel_meta", pa.table({
        "rel_id": pa.array(meta_ids, type=pa.int64()),
        "tags": pa.array(meta_tags, type=pa.string()),
        "rtype": pa.array(meta_type, type=pa.string()),
    }))
    con.register("rel_members", pa.table({
        "rel_id": pa.array(mem_rel, type=pa.int64()),
        "way_id": pa.array(mem_way, type=pa.int64()),
    }))

    tags_by_id = {i: t for i, t in zip(meta_ids, meta_tags)}
    rows: list[tuple] = []
    for rtype, use_poly in (("multipolygon", True), ("waterway", False)):
        # multipolygon：把成员线段交给 ST_Polygonize 拼回闭合面
        # （实测 4 条独立线段能正确拼成一个面）。
        # 河流本来就是线，直接合并即可。两种都算出来，下面按情况取用。
        t1 = time.time()
        print(f"  [rels] 拼 {rtype} ...", flush=True)
        got = con.execute(f"""
            SELECT r.rel_id,
                   ST_AsWKB(ST_Polygonize(list(ST_GeomFromWKB(g.wkb)))) AS poly,
                   ST_AsWKB(ST_Union_Agg(ST_GeomFromWKB(g.wkb))) AS lines
            FROM rel_members r
            JOIN rel_meta m ON m.rel_id = r.rel_id
            JOIN read_parquet('{mem_glob}') g ON g.osm_id = r.way_id
            WHERE m.rtype = '{rtype}'
            GROUP BY r.rel_id
        """).fetchall()
        print(f"  [rels] {rtype}: {len(got)} 个关系  [{time.time() - t1:.0f}s]", flush=True)

        layer = "area" if use_poly else "line"
        for rid, poly, lines in got:
            raw = None
            if use_poly:
                if poly:
                    try:
                        g0 = swkb.loads(bytes(poly))
                        if not g0.is_empty and getattr(g0, "area", 0.0) > 0:
                            raw = poly
                    except Exception:  # noqa: BLE001
                        pass
                if raw is None and lines:
                    raw = lines
            else:
                raw = lines
            if raw is None:
                continue
            try:
                geom = swkb.loads(bytes(raw))
            except Exception:  # noqa: BLE001
                continue
            if geom.is_empty:
                continue
            tags = json.loads(tags_by_id[int(rid)])
            cx, cy = geom.centroid.x, geom.centroid.y
            minx, miny, maxx, maxy = geom.bounds
            rows.append((
                "relation", int(rid), layer, kind_of(tags),
                tags.get("name") or tags.get("name:zh") or None,
                json.dumps(tags, ensure_ascii=False),
                geom.geom_type, geom.wkb, cx, cy, minx, miny, maxx, maxy,
            ))
    con.close()

    for i in range(0, len(rows), 100_000):
        chunk = rows[i:i + 100_000]
        cols = list(zip(*chunk))
        table = pa.Table.from_pydict(
            {SCHEMA.field(k).name: list(cols[k]) for k in range(len(SCHEMA))},
            schema=SCHEMA)
        path = os.path.join(outdir, f"rel-{i // 100_000:04d}.parquet")
        pq.write_table(table, path, compression="zstd", row_group_size=50_000)
        print(f"  [rels] wrote {path} rows={len(chunk)}", flush=True)
    print(f"[rels] done: {len(rows)} relation geometries  [{time.time() - t0:.0f}s]")
    return len(rows)


# ================================================================ 对外接口
def build(pbf: str, out: str, tmp: str, *, relations: bool = True,
          keep_tmp: bool = False, skip_pass1: bool = False,
          only_relations: bool = False) -> int:
    """把 .osm.pbf 转成检索用的 parquet 数据集。返回 0 表示成功。

    本模块唯一的对外入口，命令行只是它的薄壳。
    三段扫描 + relation 装配的中间产物都会留在磁盘上，
    所以单步重跑（only_relations / skip_pass1）是可行的。
    """
    t0 = time.time()
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    ways_path = os.path.join(tmp, "ways.jsonl")
    memways_path = os.path.join(tmp, "memways.jsonl")
    ids_path = os.path.join(tmp, "nodeids.bin")
    rels_path = os.path.join(tmp, "_relations.jsonl")

    print(f"[pbf ] {pbf} ({os.path.getsize(pbf) / 1024 / 1024:.0f} MB)")
    print(f"[out ] {out}")

    if only_relations:
        # relation 装配是整条链路的最后一步，前面几遍已经产出并留在磁盘上。
        # 单独重跑它可以把"只是最后一步写错"的代价从一小时降到一个函数调用。
        members_dir = os.path.join(out, "_members")
        mem_glob = os.path.join(members_dir, "memway-*.parquet").replace("\\", "/")
        if not os.path.exists(members_dir):
            print(f"[ERROR] 找不到 {members_dir}，需要先完整跑一遍前面的 passes")
            return 1
        assemble_relations(out, rels_path, mem_glob, tmp, t0)
        shutil.rmtree(members_dir, ignore_errors=True)
        print("[clean] removed member-way fragments")
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            print("[clean] removed temp files")
        return 0

    writer = Writer(out)
    # 成员 way 写到单独目录：它们只是拼 relation 用的几何碎片，没有标签、
    # 也不该进候选池。混在主目录里会让每次查询多扫几百万行。
    members_dir = os.path.join(out, "_members")
    mem_writer = Writer(members_dir, prefix="memway")
    if skip_pass1:
        print("[pass1] 跳过（复用已有临时文件）")
    else:
        print("[pass1] 扫节点和 way（C++ 层标签过滤，无随机写）...")
        with open(ways_path, "w", encoding="utf-8") as wf, \
             open(ids_path, "wb") as idf, \
             open(rels_path, "w", encoding="utf-8") as rf:
            h = PassOne(writer, wf, idf, rf)
            h.apply_file(pbf, locations=False, filters=[KEY_FILTER])
            h.close()
        writer.flush()
        print(f"[pass1] done {h.stats}  [{time.time() - t0:.0f}s]")

    if relations:
        print("[pass1b] 按 ID 捞出 relation 的成员 way（它们通常不带标签）...")
        mids = member_way_ids(rels_path)
        print(f"  [pass1b] 成员 way 共 {len(mids)} 个  [{time.time() - t0:.0f}s]", flush=True)
        if len(mids):
            with open(memways_path, "w", encoding="utf-8") as mf, \
                 open(ids_path, "ab") as idf:
                hb = PassOneB(mids, mf, idf)
                hb.apply_file(pbf, locations=False)
                hb.close()
            print(f"  [pass1b] 命中 {hb.hit} 个成员 way  [{time.time() - t0:.0f}s]")

    print("[pass2] 收集 way 需要的节点坐标（外排 + 磁盘映射）...")
    ids, lons, lats = collect_coords(pbf, ids_path, tmp, t0)
    # 原始引用文件已经没用了，早点删掉省磁盘
    if os.path.exists(ids_path):
        os.remove(ids_path)

    print("[pass3] 拼 way 几何 ...")
    build_ways(ways_path, ids, lons, lats, writer, t0)
    writer.flush()

    if relations and os.path.exists(memways_path):
        print("[pass3b] 拼 relation 成员 way 的几何 ...")
        build_ways(memways_path, ids, lons, lats, mem_writer, t0, member=True)
        mem_writer.flush()
        mem_glob = os.path.join(members_dir, "memway-*.parquet").replace("\\", "/")
        assemble_relations(out, rels_path, mem_glob, tmp, t0)
        shutil.rmtree(members_dir, ignore_errors=True)
        print("[clean] removed member-way fragments")

    if not keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)
        print("[clean] removed temp files")

    total_mb = sum(os.path.getsize(os.path.join(out, f))
                   for f in os.listdir(out)) / 1024 / 1024
    print(f"[done] {out}  {total_mb:.0f} MB, {writer.rows} rows  "
          f"total {time.time() - t0:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OSM PBF -> 检索用 parquet")
    ap.add_argument("--pbf", required=True, help="输入 .osm.pbf")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--tmp", required=True, help="中间文件目录（需较大磁盘空间）")
    ap.add_argument("--no-relations", action="store_true",
                    help="跳过 multipolygon 关系（大学校园多为关系形态，一般别跳）")
    ap.add_argument("--keep-tmp", action="store_true")
    ap.add_argument("--skip-pass1", action="store_true",
                    help="复用已有中间文件，只重跑 pass2/pass3")
    ap.add_argument("--only-relations", action="store_true",
                    help="只重跑关系装配")
    a = ap.parse_args()
    return build(a.pbf, a.out, a.tmp, relations=not a.no_relations,
                 keep_tmp=a.keep_tmp, skip_pass1=a.skip_pass1,
                 only_relations=a.only_relations)


if __name__ == "__main__":
    sys.exit(main())
