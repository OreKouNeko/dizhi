"""全国规模的查询引擎：SQL 粗筛（DuckDB + R 树） + Python 分簇精确判定。

核心约束（本机实测得出，决定了整个设计）：
1. 可用内存只有约 3 GB，所以千万级要素绝不能进内存；数据始终留在 Parquet，
   由 DuckDB 下推过滤。
2. DuckDB 的 ST_DWithin 用的是坐标单位（度）不是米；而 ST_Distance_Sphere /
   ST_Distance_Spheroid 只接受 POINT 几何，对线/面直接报错。
   因此：SQL 阶段用「偏保守的度阈值」粗筛（只多留、不错杀，能吃到 R 树索引），
   Python 阶段对剩下的少量候选做**分簇投影**（按 2° 格子分组，各自投到本地 UTM）
   得到米制精确距离，再收口。分簇是必须的——全国候选横跨几十个经度，
   单一投影会让距离严重失真。
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict

import duckdb
import numpy as np
import pyarrow as pa
from pyproj import Transformer
from shapely import wkb as swkb
from shapely.geometry import Point
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

from dizhi.ops.operators import (
    Constraint, Scored, SearchResult, Step, proximity,
)
from dizhi.ops.memory import utm_epsg

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0
POOL_WHERE = "name IS NOT NULL AND layer IN ('poi', 'area')"
# 第一个条件要在 join 里用，列名必须带表别名，否则和 el t 的列歧义
POOL_WHERE_P = "p.name IS NOT NULL AND p.layer IN ('poi', 'area')"
# 约束链中间表携带的列。刻意自带几何和展示字段，这样每一步都不用再按 ID
# 回查主表——那些回查无法用索引，每次都是 2000 万行全表扫描（实测每步约 20 秒）。
CHAIN_COLS = ("osm_type", "osm_id", "layer", "kind", "name", "n_tags",
              "wkb", "minx", "miny", "maxx", "maxy", "geom_type")
# 第一个条件直接作用在 el 上时用的选择列表。注意 el 里没有 n_tags 列，
# 标签个数要现算（而且是 length(tags)，不把整份标签 JSON 带出来）。
SEL_P = ("p.osm_type, p.osm_id, p.layer, p.kind, p.name, "
         "length(p.tags) AS n_tags, p.wkb, p.minx, p.miny, p.maxx, p.maxy, "
         "p.geom_type")
# 精确判定阶段的区域划分粒度（度）。
#
# 实测对照（大学查询，813 个候选，全国散布）：
#   2.0°  -> 63 个区域，126 次目标查询，取回 18.7 万目标，82.0s
#   0.5°  -> 112 个区域，224 次目标查询，取回 10.0 万目标，97.5s
# 格子小确实少取目标，但区域数变多，而**每次查询约 0.27 秒的固定开销才是主导**，
# 所以小格子反而更慢。真正的修法是把同一约束下所有区域的取回**合并成一次查询**
# （把各区域包围框放进临时表做连接，见 10.3e），而不是继续调格子大小。
CELL_DEG = 2.0
# inside（内部包含）的默认容差：贴在围墙外的食堂、银行也该算"校内"。
# 候选如果是点（校园被画成一个节点而没有多边形），退化成以该点为心的大半径。
INSIDE_TOL_M = 120.0
# 目标要素超过这个数、又几乎筛不掉候选的条件，推迟到精确阶段再算（见 search）
DEFER_TARGET_COUNT = 1_000_000
INSIDE_POINT_TOL_M = 700.0
# True = 严格的"内部包含"：目标要素必须真的落在候选面**里面**（covers，含边界）。
# False = 旧行为：用 ST_DWithin(面, 目标, 120米)，会把校园**外** 120 米内的
#         目标也算作"内部有"。默认关掉旧行为。
# 校园只被标成一个点时无法谈"内部"，INSIDE_STRICT 下这类候选直接排除
# （可以用 --loose-inside 放宽成 700 米半径近似）。
INSIDE_STRICT = True


def deg_guard(d_m: float, max_abs_lat: float) -> float:
    """米 -> 保守度阈值（取纬向/经向里较大的那个，保证不错杀）。"""
    lat = min(max(abs(max_abs_lat), 0.0), 89.0)
    d_lat = d_m / M_PER_DEG_LAT
    d_lon = d_m / (M_PER_DEG_LON_EQ * max(math.cos(math.radians(lat)), 1e-6))
    return max(d_lat, d_lon)


def match_tags(tags: dict, filters: list[dict[str, str]] | None) -> bool:
    if not filters:
        return True
    return any(all(tags.get(k) == v for k, v in f.items()) for f in filters)


class SqlEngine:
    def __init__(self, glob: str, temp_dir: str, threads: int = 4,
                 memory_limit: str = "2GB", verbose: bool = True):
        self.verbose = verbose
        self.glob = glob.replace("\\", "/").replace("'", "''")
        con = duckdb.connect()
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.execute(f"SET temp_directory='{temp_dir.replace(chr(92), '/')}'")
        con.execute(f"PRAGMA threads={threads}")
        con.execute(f"SET memory_limit='{memory_limit}'")
        # 这两个设置对本机（可用内存只有约 3 GB）是关键的：
        # "保持插入顺序"会让大聚合（比如把 200 万候选按 12 列分组求 MIN 距离）
        # 一直握着全部中间结果，实测直接 OOM；关掉后同样的查询能跑完。
        con.execute("SET preserve_insertion_order=false")
        # 地理分区数据带一个 cell 列（3° 格子）。按它过滤能让 DuckDB 在
        # 「目录级」就把不相干的数据整片跳过——实测同一次 bbox 查询：
        # 带 cell 过滤 0.09 秒，不带则要遍历所有文件。
        try:
            dcols = {d[0] for d in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{self.glob}') LIMIT 1"
            ).fetchall()}
        except Exception:  # noqa: BLE001
            dcols = set()
        self.has_cell = "cell" in dcols
        cell_expr = "cell" if self.has_cell else "NULL::BIGINT AS cell"
        con.execute(
            "CREATE VIEW el AS SELECT osm_type, osm_id, layer, kind, name, tags, "
            f"geom_type, wkb, clon, clat, minx, miny, maxx, maxy, {cell_expr}, "
            "ST_GeomFromWKB(wkb) AS geom "
            f"FROM read_parquet('{self.glob}')")
        self.con = con
        self._n = 0
        self._cnt_cache: dict[str, int] = {}
        self._bbox_cache: dict[str, tuple | None] = {}
        self.cell_deg = 3.0

    def cell_in(self, bbox: tuple[float, float, float, float] | None,
                max_cells: int = 400, alias: str | None = None) -> str:
        """把经纬度范围换成 cell 分区值的 IN 列表。格子太多就不加（得不偿失）。"""
        if not self.has_cell or bbox is None:
            return ""
        try:
            lo_lat = math.floor(bbox[1] / self.cell_deg)
            hi_lat = math.floor(bbox[3] / self.cell_deg)
            lo_lon = math.floor(bbox[0] / self.cell_deg)
            hi_lon = math.floor(bbox[2] / self.cell_deg)
        except (TypeError, ValueError):
            return ""
        vals = [la * 1000 + lo for la in range(lo_lat, hi_lat + 1)
                for lo in range(lo_lon, hi_lon + 1)]
        if not vals or len(vals) > max_cells:
            return ""
        col = f"{alias}.cell" if alias else "cell"
        return f" AND {col} IN ({','.join(str(v) for v in vals)})"

    # ------------------------------------------------------------ 基础设施
    def _t(self) -> str:
        self._n += 1
        return f"t{self._n}"

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def close(self) -> None:
        self.con.close()

    def _count(self, table: str) -> int:
        return int(self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    # ------------------------------------------------------------ SQL 片段
    @staticmethod
    def filters_sql(filters: list[dict[str, str]] | None, alias: str | None = None) -> str:
        """把标签条件编译成 SQL。

        支持一个特殊键 `name~`：表示"名称里包含"。品牌类目标必须用它——
        "肯德基"不是 amenity=fast_food（那会把所有快餐都算进来），
        而是名称包含"肯德基"。
        """
        if not filters:
            return "TRUE"
        pre = f"{alias}." if alias else ""
        ors = []
        for f in filters:
            parts = []
            for k, v in f.items():
                if k == "name~":
                    safe = str(v).replace("'", "''")
                    parts.append(f"{pre}name LIKE '%{safe}%'")
                else:
                    safe = str(v).replace("'", "''")
                    parts.append(f"json_extract_string({pre}tags, '$.{k}') = '{safe}'")
            ors.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(ors) + ")"

    def target_where(self, c: Constraint, alias: str | None = None) -> str:
        pre = f"{alias}." if alias else ""
        if c.op == "along":
            nm = (c.feature_name or "").replace("'", "''")
            # 精确名字优先。直接用 LIKE 会把「长江路」「长江大桥」也算成长江，
            # 目标集被污染后候选范围会明显变大。
            cache = getattr(self, "_along_cache", None)
            if cache is None:
                cache = self._along_cache = {}
            if nm not in cache:
                exact = int(self.con.execute(
                    f"SELECT count(*) FROM el WHERE layer = 'line' AND name = '{nm}'"
                ).fetchone()[0])
                cache[nm] = exact > 0
                if not exact:
                    print(f"  [sql] 没有精确叫「{nm}」的线要素，改用包含匹配")
            clause = (f"{pre}name = '{nm}'" if cache[nm]
                      else f"{pre}name LIKE '%{nm}%'")
            return f"{pre}layer = 'line' AND {clause}"
        return self.filters_sql(c.filters, alias)

    def target_count(self, c: Constraint) -> int:
        """目标要素个数。带缓存——它要扫全表，而排序和展示会重复问同一个条件。"""
        key = self.target_where(c, "el")
        if key not in self._cnt_cache:
            self._cnt_cache[key] = int(self.con.execute(
                f"SELECT count(*) FROM el WHERE {key}").fetchone()[0])
        return self._cnt_cache[key]

    def pool_count(self, bbox: tuple[float, float, float, float] | None = None) -> int:
        where = POOL_WHERE + (f" AND {self.bbox_sql(bbox)}" if bbox else "") \
            + self.cell_in(bbox)
        return int(self.con.execute(f"SELECT count(*) FROM el WHERE {where}").fetchone()[0])

    @staticmethod
    def bbox_sql(bbox: tuple[float, float, float, float], alias: str | None = None) -> str:
        """用行的外包框做区域限定。maxx/ minx 等列在 Parquet 里有统计信息，
        所以这个条件能让 DuckDB 直接跳过不相干的数据块，是"秒级 vs 分钟级"的关键。"""
        min_lon, min_lat, max_lon, max_lat = bbox
        pre = f"{alias}." if alias else ""
        return (f"{pre}maxx >= {min_lon} AND {pre}minx <= {max_lon} "
                f"AND {pre}maxy >= {min_lat} AND {pre}miny <= {max_lat}")

    def _bbox(self, where: str):
        """某个条件下的外包框。带缓存（这是全表扫描，会被反复问到）。"""
        if where not in self._bbox_cache:
            self._bbox_cache[where] = self.con.execute(
                f"SELECT min(minx), min(miny), max(maxx), max(maxy) "
                f"FROM el WHERE {where}").fetchone()
        return self._bbox_cache[where]

    # ------------------------------------------------------------ 硬约束（SQL 粗筛）
    def _approx_expr(self, calias: str) -> str:
        """候选到目标要素的**近似**距离（米），在 SQL 里顺带算出来。

        用"候选中心到目标质心"的等距圆柱近似。点目标时它就是精确点距；
        小面/短线误差在几百米内，够用来预排序。

        刻意不用"到目标外包框的距离"：长江这种超长线要素的外包框覆盖半个中国，
        会让几乎所有候选的近似距离都变成 0，排序彻底失效。

        用途只有一个——把最可能是答案的少量候选挑出来做精确几何判定，
        最终结果仍以精确判定为准。
        """
        cx = f"(({calias}.minx + {calias}.maxx) / 2.0)"
        cy = f"(({calias}.miny + {calias}.maxy) / 2.0)"
        return (f"SQRT(POWER(({cx} - t.clon) * 111320.0 * COS(RADIANS({cy})), 2) + "
                f"POWER(({cy} - t.clat) * 110574.0, 2))")

    def apply_constraint(self, src: str, c: Constraint,
                         first_where: str | None = None,
                         skip_ad: bool = False,
                         ci_index: int = -1) -> tuple[str, int]:
        """在 src（自带几何的约束链表）上应用一个约束。

        first_where 不为空时表示"这是第一个条件，直接作用在全表 el 上"：
        这样就不必先把两百多万个候选池连几何一起物化成临时表再筛一遍
        （那一步本身就要十几秒，而且写出来的临时表非常大）。
        """
        first = first_where is not None
        cfrom = "el p" if first else f"{src} s"
        calias = "p" if first else "s"
        cgeom = "p.geom" if first else "ST_GeomFromWKB(s.wkb)"
        ccols = SEL_P if first else ", ".join(f"s.{x}" for x in CHAIN_COLS)
        base = f"({first_where})" if first else None

        out = self._t()

        if c.op == "exists":
            # 链表里只有 n_tags（标签个数），没有完整 tags 列——那是为省内存刻意不带的。
            # 所以 exists 要回主表按 ID 做半连接，不能直接在链表上过滤标签。
            conds = [f"({calias}.osm_type, {calias}.osm_id) IN ("
                     f"SELECT osm_type, osm_id FROM el WHERE "
                     f"{self.filters_sql(c.filters, 'el')})"]
            if base:
                conds.insert(0, base)
            carry = "".join(f", {calias}.{a}" for a in self._ad_cols)
            self.con.execute(f"""
                CREATE TEMP TABLE {out} AS
                SELECT {ccols}{carry} FROM {cfrom} WHERE {' AND '.join(conds)}
            """)
            return out, self.target_count(c)

        tw_t = self.target_where(c, "t")
        bbox = self._bbox(self.target_where(c, "el"))
        if bbox is None or bbox[0] is None:
            self.con.execute(f"CREATE TEMP TABLE {out} AS "
                             f"SELECT {ccols} FROM {cfrom} WHERE FALSE")
            return out, 0

        max_lat = max(abs(bbox[1]), abs(bbox[3]))
        if c.op == "inside":
            limit_m = float(c.max_distance or INSIDE_TOL_M)
            eff = max(limit_m, INSIDE_POINT_TOL_M)
        elif c.op == "along":
            limit_m = float(c.tolerance or 2000.0)
            eff = limit_m
        else:
            limit_m = float(c.max_distance or 2000.0)
            eff = limit_m
        guard = deg_guard(eff, max_lat)

        # 用候选集自身的经纬度范围给目标查询加空间剪枝：可能命中的目标
        # 只会在候选范围外扩一点点之内。加上这个条件，DuckDB 就能按地理分区的
        # 统计信息跳过绝大部分数据块。
        if first:
            # first_where 带 p. 前缀（用于 join），而 _bbox 是在裸 el 上查的，
            # 所以要把前缀去掉
            sb = self._bbox(first_where.replace("p.", ""))
        else:
            sb = self.con.execute(
                f"SELECT min(minx), min(miny), max(maxx), max(maxy) FROM {src}").fetchone()
        tfilter = ""
        if sb and sb[0] is not None:
            p = deg_guard(eff, max(abs(sb[1]), abs(sb[3])))
            tfilter = (f" AND t.maxx >= {sb[0] - p} AND t.minx <= {sb[2] + p} "
                       f" AND t.maxy >= {sb[1] - p} AND t.miny <= {sb[3] + p}"
                       + self.cell_in((sb[0] - p, sb[1] - p, sb[2] + p, sb[3] + p),
                                      alias="t"))

        # inside / near / along 在 SQL 阶段都只是**粗筛**，精确判定在 Python 里做。
        #
        # 这里刻意不用 ST_DWithin 而用外包框数值比较：ST_DWithin 需要把候选表
        # 每一行都 ST_GeomFromWKB 一次（全国候选池两百多万行），
        # 实测单个条件要 560-690 秒。外包框测试是它的**保守超集**
        # （几何距离一定 ≥ 外包框距离），纯数值比较，快一个数量级。
        #
        # 单位仍然必须是度（guard），原因见下面 INIT 里的说明。
        op_sql = (f"{calias}.maxx >= t.minx - {guard} AND "
                  f"{calias}.minx <= t.maxx + {guard} AND "
                  f"{calias}.maxy >= t.miny - {guard} AND "
                  f"{calias}.miny <= t.maxy + {guard}")

        where_sql = f" WHERE {base}" if base else ""
        if c.op in ("near", "along", "inside"):
            carry = "".join(f", {calias}.{a}" for a in self._ad_cols)
            if first or skip_ad:
                # 源表是两百多万行的候选池时**不能**做 MIN 聚合：
                # GROUP BY 要在 200 万行（还带 WKB）上建 200 万个分组，实测直接 OOM。
                # 先老老实实筛出小结果集，近似距离列交给后续步骤补。
                self.con.execute(f"""
                    CREATE TEMP TABLE {out} AS
                    SELECT DISTINCT {ccols}{carry}
                    FROM {cfrom}
                    JOIN el t ON ({tw_t}){tfilter} AND {op_sql}
                    {where_sql}
                """)
                return out, self.target_count(c)
            # 顺带算一列近似距离。空间连接本来就要做，多一个 MIN 聚合几乎不要钱，
            # 但能省掉后面为上万个候选做精确几何判定的开销。
            ad = f"ad{len(self._ad_cols)}"
            self._ad_cols.append(ad)
            self._ad_ci.append(ci_index)
            ncol = len(CHAIN_COLS) + len(self._ad_cols) - 1
            gcols = ", ".join(str(i + 1) for i in range(ncol))
            self.con.execute(f"""
                CREATE TEMP TABLE {out} AS
                SELECT {ccols}{carry}, MIN({self._approx_expr(calias)}) AS {ad}
                FROM {cfrom}
                JOIN el t ON ({tw_t}){tfilter} AND {op_sql}
                {where_sql}
                GROUP BY {gcols}
            """)
        else:  # not_exists（粗筛阶段故意放宽，精确收口放在 Python）
            carry = "".join(f", {calias}.{a}" for a in self._ad_cols)
            self.con.execute(f"""
                CREATE TEMP TABLE {out} AS
                SELECT {ccols}{carry} FROM {cfrom}
                WHERE NOT EXISTS (
                    SELECT 1 FROM el t
                    WHERE ({tw_t}){tfilter} AND {op_sql}
                ){(' AND ' + base) if base else ''}
            """)
        return out, self.target_count(c)

    # ------------------------------------------------------------ 主流程
    def search(self, constraints: list[Constraint], top: int = 10,
               order_by_selectivity: bool = True,
               bbox: tuple[float, float, float, float] | None = None) -> SearchResult:
        t0 = time.time()
        res = SearchResult()
        res.pool_size = self.pool_count(bbox)
        where = (POOL_WHERE + (f" AND {self.bbox_sql(bbox)}" if bbox else "")
                 + self.cell_in(bbox))
        where_first = (POOL_WHERE_P
                       + (f" AND {self.bbox_sql(bbox, 'p')}" if bbox else "")
                       + self.cell_in(bbox, alias="p"))
        self._ad_cols = []
        self._ad_ci: list[int] = []
        self.log(f"  [sql] 候选池 {res.pool_size} 个"
                 + (f"（已限定区域 {bbox}）" if bbox else "（全国有名字的点/面要素）"))

        hard = [c for c in constraints if c.hard]
        if order_by_selectivity and len(hard) > 1:
            counts = {id(c): self.target_count(c) for c in hard}
            # 标签类条件（exists）一律排在距离类条件前面：
            # 它是纯集合过滤，作用在两百多万行的候选池上只要几秒；
            # 而距离类条件要做空间连接，先在池子上跑会非常慢（实测 672 秒），
            # 先被 exists 砍小之后再跑就快得多。
            hard = sorted(hard, key=lambda c: (0 if c.op == "exists" else 1,
                                               counts[id(c)]))
            self.log("  [sql] 条件顺序: "
                     + " -> ".join(f"{c.describe()}[{counts[id(c)]}]" for c in hard))

        # 第一个条件直接作用在全表 el 上，省掉"先把 200 万行候选池连几何
        # 物化成临时表"这一步（实测那一步要几分钟）。
        #
        # 注意它必须配 skip_ad=True：在全表上做 MIN 聚合要建 200 万个分组，会 OOM。
        # 精确度不受影响——近似距离只是用来预排序，精确判定仍以几何计算为准。
        current = None
        pool_n = res.pool_size
        start_idx = 0
        # 融合优化：第一个条件如果是 exists（纯标签过滤，比如"本身就是大学"），
        # 把它和"建立候选池"合并成**一次**扫描。
        # 原来是先把 207 万行物化成临时表、再对它做一次 ID 半连接，要扫两遍 el
        # （实测 18 秒）；融合后只扫一遍。
        if hard and hard[0].op == "exists":
            _tbl = self._t()
            # 先把目标 ID 集合物化成一张小表（实测 2 秒），再做**哈希半连接**。
            # 不这样做的话，`(a,b) IN (SELECT ...)` 直接套在 CREATE TABLE AS 里
            # 会让 DuckDB 选到很差的计划：实测物化 2774 行要 11.5 秒，
            # 而同样的条件只做 count 只要 3.9 秒。
            _ids = self._t()
            self.con.execute(
                f"CREATE TEMP TABLE {_ids} AS SELECT osm_type, osm_id FROM el "
                f"WHERE {self.filters_sql(hard[0].filters, 'el')}")
            # 顺序试过两种：① 池过滤+哈希连接一起做（13 秒）；② 先用 ID 缩到
            # 2829 行再套池过滤（反而 15 秒，多建一张临时表不划算）。
            # 采用 ①。真正的成本似乎是为 2070 万行读 name/tags 列的固有开销。
            self.con.execute(
                f"CREATE TEMP TABLE {_tbl} AS SELECT {SEL_P} FROM el p "
                f"JOIN {_ids} i ON i.osm_type = p.osm_type AND i.osm_id = p.osm_id "
                f"WHERE {where}")
            _after = self._count(_tbl)
            res.steps.append(Step(constraint=hard[0], before=pool_n, after=_after,
                                  target_count=self.target_count(hard[0])))
            self.log(f"  [sql] {hard[0].describe()}（与候选池合并扫描）: "
                     f"{pool_n} -> {_after}  [{time.time() - t0:.0f}s]")
            start_idx = 1
            current = _tbl if _after else None
        for idx in range(start_idx, len(hard)):
            c = hard[idx]
            # 目标要素极多、几乎不筛掉任何候选的条件，**不在 SQL 链路里做**。
            #
            # 实测"5 公里内有铁塔"：候选 481575 -> 479573（一个都没砍掉），
            # 却因为 48 万候选 × 385 万目标的 4 条件不等式连接耗了 329 秒。
            # 这类条件没有区分度，推迟到精确阶段即可——那里只对预排后的
            # ~1000 个候选逐个精确判定，既省掉几百秒，也不影响最终排序。
            if current is not None and self.target_count(c) > DEFER_TARGET_COUNT:
                self.log(f"  [sql] 跳过「{c.describe()}」（目标 {self.target_count(c)} 个，"
                         f"几乎不筛候选）→ 推迟到精确阶段")
                continue
            if current is None:
                before = pool_n
                nxt, tcount = self.apply_constraint("", c, first_where=where_first,
                                                    skip_ad=True,
                                                    ci_index=constraints.index(c))
            else:
                before = self._count(current)
                nxt, tcount = self.apply_constraint(current, c,
                                                    ci_index=constraints.index(c))
            after = self._count(nxt)
            self.log(f"  [sql] {c.describe()}: {before} -> {after}（目标要素 {tcount}）"
                     f"  [{time.time() - t0:.0f}s]")
            res.steps.append(Step(constraint=c, before=before, after=after,
                                  target_count=tcount))
            current = nxt
            if after == 0:
                break
        if current is None:
            current = self._t()
            self.con.execute(f"CREATE TEMP TABLE {current} AS "
                             f"SELECT {SEL_P} FROM el p WHERE {where}")

        rows = self._fetch(current)
        res.table = current
        self.log(f"  [sql] 取出 {len(rows)} 个候选做精确判定  [{time.time() - t0:.0f}s]")
        keep = max(top * 100, 1000)
        if len(rows) > keep:
            total_before = len(rows)
            rows = self._preselect(rows, constraints, keep)
            res.preselect = (total_before, len(rows))
            self.log(f"  [sql] 用近似距离预排，只对最靠前的 {len(rows)} 个做精确几何判定"
                     f"  [{time.time() - t0:.0f}s]")
        res.ranked = self._exact(rows, constraints, t0)
        res.ranked.sort(key=lambda s: s.score, reverse=True)
        return res

    def _attach_geom(self, scored: list[Scored]) -> None:
        """给最终要输出的要素补几何。优先复用 _exact 已经取回的原始几何，
        避免为了几十条结果再做一次全表扫描。"""
        raw = getattr(self, "_raw_geo", {})
        missing = []
        for s in scored:
            rg = raw.get((s.element.osm_type, s.element.osm_id))
            if rg:
                s.element.wkb = rg[3]
            else:
                missing.append(s)
        if missing:
            geo = self._geoms_of([(s.element.osm_type, s.element.osm_id)
                                  for s in missing])
            for s in missing:
                s.element.wkb = geo.get((s.element.osm_type, s.element.osm_id))

    def _fetch(self, current: str):
        """候选已经在约束链表里自带全部需要的列，直接取，不需要再回查主表。

        标签只需要"有几个"（打分用它衡量数据完整度），不取完整 JSON——
        全国规模下标签 JSON 是最大的内存开销。
        """
        cols = ", ".join(CHAIN_COLS + tuple(self._ad_cols))
        return self.con.execute(f"SELECT {cols} FROM {current}").fetchall()

    def _preselect(self, rows, constraints: list[Constraint], keep: int):
        """用 SQL 顺带算出的近似距离先排序，只保留最靠前的 keep 个。

        对一万多个候选全部做精确几何判定非常贵（要按区域分批取目标几何再逐个算距离）。
        而近似距离已经足够把"明显不像"的候选排到后面——最终结果仍以精确判定为准，
        这里只是决定"谁值得被精确检查"。
        """
        from dizhi.ops.operators import proximity
        n_base = len(CHAIN_COLS)
        if not self._ad_cols:
            return rows
        scored = []
        for r in rows:
            tot, wsum, ok = 0.0, 0.0, True
            # 近似距离列只对应"真正加了聚合"的那些约束（第一个条件在候选池上
            # 会跳过聚合以避免 OOM），所以必须用记录下来的下标，不能假设一一对应
            for k, ci in enumerate(self._ad_ci):
                c = constraints[ci]
                d = r[n_base + k]
                if d is None:
                    ok = False
                    break
                if c.op == "inside":
                    lim = float(c.max_distance or INSIDE_TOL_M)
                elif c.op == "along":
                    lim = float(c.tolerance or 2000.0)
                else:
                    lim = float(c.max_distance or 2000.0)
                tot += c.weight * proximity(float(d), lim)
                wsum += c.weight
            if not ok or wsum == 0:
                continue
            scored.append((tot / wsum, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:keep]]

    def _geoms_of(self, ids: list[tuple[str, int]]) -> dict[tuple[str, int], bytes]:
        """按需取一批要素的几何（用于最终 Top-K 出图）。"""
        if not ids:
            return {}
        tbl = pa.table({
            "t": pa.array([a for a, _ in ids], type=pa.string()),
            "i": pa.array([b for _, b in ids], type=pa.int64()),
        })
        self.con.register("want_geom", tbl)
        rows = self.con.execute("""
            SELECT p.osm_type, p.osm_id, p.wkb FROM el p
            SEMI JOIN want_geom w ON p.osm_type = w.t AND p.osm_id = w.i
        """).fetchall()
        return {(r[0], int(r[1])): r[2] for r in rows}

    # ------------------------------------------------------------ 精确判定（分簇投影）
    @staticmethod
    def _project_many(to_m, items: list[tuple]) -> dict:
        """批量投影，返回 {key: 米制几何}。

        items 里每一项是 (key, geom_type, clon, clat, wkb)。
        占绝大多数的点要素（铁塔三百多万个、POI 等）直接用预先算好的 clon/clat 列，
        再用 numpy **一次性**投影；只有面/线才去解析 WKB 逐个投影。

        这是精确阶段最大的性能杠杆：逐要素调用 pyproj 的固定开销极高，
        实测全国查询 347 秒里有约 300 秒耗在这里。
        """
        pts: list[tuple[float, float]] = []
        pt_keys: list = []
        others: list[tuple] = []
        out: dict = {}
        for key, gtype, lon, lat, raw in items:
            if gtype == "Point" and lon is not None and lat is not None:
                pts.append((lon, lat))
                pt_keys.append(key)
            elif raw:
                others.append((key, raw))
        if pts:
            arr = np.asarray(pts, dtype=np.float64)
            xs, ys = to_m(arr[:, 0], arr[:, 1])
            xs = np.atleast_1d(np.asarray(xs, dtype=np.float64))
            ys = np.atleast_1d(np.asarray(ys, dtype=np.float64))
            for i, k in enumerate(pt_keys):
                out[k] = Point(float(xs[i]), float(ys[i]))
        for key, raw in others:
            try:
                out[key] = shp_transform(to_m, swkb.loads(raw))
            except Exception:  # noqa: BLE001
                continue
        return out

    @staticmethod
    def _min_dists(cand_m: dict, tgt_m: list, search_r: float) -> dict:
        """算每个候选到目标集的最近距离（米）。

        点对点用完全向量化的 numpy：候选和目标的坐标各取成数组，分块算距离矩阵。
        之前是"每个候选单独调一次 numpy"（1.8 万个候选就是 1.8 万次调用，
        每次还要过一遍全部目标），改成矩阵一次算完之后快很多。
        面/线候选或面/线目标仍走 R 树（几何相交不能靠坐标差算）。
        """
        tgt_xy: list[tuple[float, float]] = []
        others: list = []
        for g in tgt_m:
            if g.geom_type == "Point":
                tgt_xy.append((g.x, g.y))
            else:
                others.append(g)
        tarr = np.asarray(tgt_xy, dtype=np.float64) if tgt_xy else None
        tree_other = STRtree(others) if others else None
        tree_all = None

        pt_idx = [i for i, g in enumerate(cand_m.values()) if g.geom_type == "Point"]
        pt_set = set(pt_idx)
        pt_keys = list(cand_m.keys())
        out: dict[int, float] = {}

        # ---- 点候选 × 点目标：矩阵化 ----
        if tarr is not None and pt_idx:
            cxy = np.asarray([(cand_m[pt_keys[j]].x, cand_m[pt_keys[j]].y)
                              for j in pt_idx], dtype=np.float64)
            nil = np.full(len(cxy), np.inf)
            for s in range(0, len(cxy), 512):
                e = min(s + 512, len(cxy))
                dx = cxy[s:e, 0, None] - tarr[None, :, 0]
                dy = cxy[s:e, 1, None] - tarr[None, :, 1]
                nil[s:e] = np.sqrt((dx * dx + dy * dy).min(axis=1))
            for k, j in enumerate(pt_idx):
                out[pt_keys[j]] = float(nil[k])

        # ---- 点候选 × 非点目标，以及所有非点候选：走 R 树 ----
        for j, key in enumerate(pt_keys):
            g = cand_m[key]
            if j in pt_set and tree_other is None:
                continue
            if g.geom_type == "Point" and j in pt_set:
                best = out.get(key, float("inf"))
                r = min(search_r, best)
                hits = tree_other.query(g, predicate="dwithin", distance=r)
                if len(hits):
                    out[key] = min(best, min(g.distance(others[int(h)]) for h in hits))
            else:
                if tree_all is None:
                    tree_all = STRtree(tgt_m)
                hits = tree_all.query(g, predicate="dwithin", distance=search_r)
                out[key] = (min(g.distance(tgt_m[int(h)]) for h in hits)
                            if len(hits) else float("inf"))
        return out

    # ------------------------------------------------------------ 主动提问
    def candidate_count(self, table: str) -> int:
        return self._count(table)

    def suggest_questions(self, table: str, total: int,
                          library: list[tuple[str, list[dict[str, str]]]],
                          max_q: int = 8) -> list[dict]:
        """在当前候选集上挑"最能砍掉范围"的下一个问题。

        对每个候选问题算一次"有多少候选满足它"（只做 count，不物化结果表），
        然后按**分割的均衡程度**排序：一刀切成两半的问题信息量最大
        （全部命中或全部不命中都等于没问）。

        问一次大约 1-3 秒，所以限制问题数量；信息增益大的先问。
        """
        if total <= 0:
            return []
        out: list[dict] = []
        for label, filters in library[:max_q]:
            # 一律用 inside 语义：候选是校园/园区这类面要素时问"里面有没有"，
            # 候选是点要素时自动退化成"周边 700 米内有没有"。
            c = Constraint(op="inside", filters=filters, max_distance=INSIDE_TOL_M,
                           label=label, cid="q")
            try:
                n1 = self._count_where(table, c)
            except Exception:  # noqa: BLE001
                continue
            n0 = total - n1
            if n1 == 0 or n0 == 0:
                gain = 0.0
            else:
                # 信息增益 = 熵。p 接近 0.5（一刀切成两半）时最大；
                # 全部命中或全部不命中（p 接近 0 或 1）都是白问，增益为 0。
                p = n1 / total
                import math as _m
                gain = -(p * _m.log2(p) + (1 - p) * _m.log2(1 - p))
            out.append({"label": label, "filters": filters, "yes": n1, "no": n0,
                        "gain": round(gain, 4),
                        "expected_left": max(n1, n0)})
        out.sort(key=lambda d: (d["gain"], -d["expected_left"]), reverse=True)
        return out

    def _count_where(self, table: str, c: Constraint, sample: int = 3000) -> int:
        """统计满足条件的候选数（返回**估计值**）。

        候选多的时候在全量上跑空间查询会非常慢（每个问题都要一次空间连接，
        问 8 个问题就卡住了）。这里改成在随机抽样上算，再按比例放大——
        估算信息增益根本不需要精确值，抽 3000 个足够判断"是不是对半分"。

        op_sql 必须和 apply_constraint 里保持一致，否则"预测能砍到多少"
        和实际缩小结果会对不上。
        """
        tw_t = self.target_where(c, "t")
        # 刻意不算目标要素的外包框：那是一次 2000 万行的全表扫描，
        # 问 8 个问题就是 8 次，直接把交互卡死。用保守的纬度上限代替即可。
        if c.op == "inside":
            limit_m = float(c.max_distance or INSIDE_TOL_M)
            eff = max(limit_m, INSIDE_POINT_TOL_M)
        else:
            limit_m = float(c.max_distance or 1000.0)
            eff = limit_m
        guard = deg_guard(eff, 45.0)
        total = self._count(table)
        src = (f"(SELECT * FROM {table} USING SAMPLE {sample} ROWS)"
               if total > sample else f"{table}")
        sb = self.con.execute(
            f"SELECT min(minx), min(miny), max(maxx), max(maxy) FROM {src}").fetchone()
        tfilter = ""
        if sb and sb[0] is not None:
            p = deg_guard(eff, max(abs(sb[1]), abs(sb[3])))
            tfilter = (f" AND t.maxx >= {sb[0] - p} AND t.minx <= {sb[2] + p} "
                       f" AND t.maxy >= {sb[1] - p} AND t.miny <= {sb[3] + p}"
                       + self.cell_in((sb[0] - p, sb[1] - p, sb[2] + p, sb[3] + p),
                                      alias="t"))
        # 同样必须用 guard（度）而不是 eff（米），原因见 apply_constraint
        op_sql = f"ST_DWithin(ST_GeomFromWKB(s.wkb), t.geom, {guard})"
        n_src = min(total, sample)
        # 必须用 JOIN 而不是相关子查询 EXISTS：EXISTS 会让 DuckDB
        # 为每一行候选重建一次目标要素的 R 树（3000 行 × 5 万个目标 = 卡死，
        # 实测一个问题要 75 秒、8 个问题 600 秒）。JOIN 只建一次 R 树。
        hit = int(self.con.execute(f"""
            SELECT count(DISTINCT s.osm_type || '/' || s.osm_id)
            FROM {src} s
            JOIN el t ON ({tw_t}){tfilter} AND {op_sql}
        """).fetchone()[0])
        if n_src <= 0:
            return 0
        return int(round(hit * total / n_src)) if total > sample else hit

    @staticmethod
    def _as_areal(g):
        """把候选几何压成"面"；压不出来（真的是点/线）就返回 None。

        **关系型校园的几何类型是 GeometryCollection**——拼关系时把
        multipolygon 的面和 waterway 的线合在了一起。只认 Polygon/MultiPolygon
        的话，所有 relation 型校园都会被当成"点状校园"而无法做包含判定
        （实测武汉大学、华中师范大学等全被误杀）。这里把其中的面提出来。
        """
        if g.geom_type in ("Polygon", "MultiPolygon"):
            return g
        if g.geom_type == "GeometryCollection":
            polys = []
            for x in g.geoms:
                if x.geom_type == "Polygon":
                    polys.append(x)
                elif x.geom_type == "MultiPolygon":
                    polys.extend(x.geoms)
            if not polys:
                return None
            from shapely.geometry import MultiPolygon
            return polys[0] if len(polys) == 1 else MultiPolygon(polys)
        return None

    def _exact_inside(self, ci: int, c: Constraint, cand_m: dict, tgt_m: list,
                      limit: float, pass_hard: list, dist: list) -> None:
        """严格判定"目标要素是否真的在候选面**内部**"。

        原实现走的是距离路径 `ST_DWithin(校园, 目标, 120米)`，它会
        **把校园外 120 米内的肯德基也算成"内部有"**——不符合"严格在校园内部"。
        这里改成真正的包含判定 `campus.covers(target)`（covers 含边界，
        比 contains 宽容一点点，适合处理贴边的点位）。

        校园只被标成一个**点**时无法谈"内部"：
          * INSIDE_STRICT=True（默认）→ 这类候选直接排除，并计数上报；
          * INSIDE_STRICT=False → 退化成 700 米半径近似，且**打分也用 700 米**
            （旧代码硬性放宽到 700 米却仍按 120 米打分，300 米外的肯德基
             得分只有 14%，看起来像"判定失败"，其实是打分口径不一致）。
        """
        areas: list = []
        pts: list = []
        for i, g in cand_m.items():
            ag = self._as_areal(g)
            if ag is not None:
                areas.append((i, ag))
            else:
                pts.append(i)
        if not areas:
            # 该区域全是点状校园
            if INSIDE_STRICT:
                self._geo_stats["inside_point_skipped"] = (
                    self._geo_stats.get("inside_point_skipped", 0) + len(pts))
                for i in pts:
                    dist[ci][i] = float("inf")
                return
            dm = self._min_dists(cand_m, tgt_m, max(limit, INSIDE_POINT_TOL_M))
            for i in pts:
                d = dm.get(i, float("inf"))
                dist[ci][i] = d
                if d <= INSIDE_POINT_TOL_M:
                    pass_hard[ci].add(i)
            return

        tree = STRtree(tgt_m) if tgt_m else None
        for i, g in areas:
            # multipolygon 关系用 ST_Polygonize / ST_Union_Agg 拼出来的几何
            # 常有自相交，**无效几何会让 covers() 返回 false**，
            # 导致真实在校园里的肯德基被判成"不在内部"（实测武汉大学、
            # 华中师范大学、浙大紫金港这些 relation 型校园全被误杀，
            # 而 DuckDB 的 ST_Contains 对同样的几何是返回 true 的）。
            # 修一遍有效性再判定。
            if not g.is_valid:
                try:
                    from shapely.validation import make_valid
                    fixed = make_valid(g)
                    if fixed is not None and not fixed.is_empty:
                        g = fixed
                except Exception:  # noqa: BLE001
                    pass
            hit = False
            if tree is not None:
                try:
                    idxs = tree.query(g)
                except Exception:  # noqa: BLE001
                    idxs = range(len(tgt_m))
                for k in idxs:
                    t = tgt_m[int(k)]
                    try:
                        # 点状目标（肯德基、泳池）必须**真的落在校园里**；
                        # 面状目标（水体）用相交即可——否则一条穿过校园的大河
                        # 永远不可能被"完全包含"，反而会把真实情况排除掉。
                        hit = (g.covers(t) if t.geom_type == "Point"
                               else g.intersects(t))
                    except Exception:  # noqa: BLE001
                        hit = False
                    if hit:
                        break
            dist[ci][i] = 0.0 if hit else float("inf")
            if hit:
                pass_hard[ci].add(i)

        if not pts:
            return
        if INSIDE_STRICT:
            self._geo_stats["inside_point_skipped"] = (
                self._geo_stats.get("inside_point_skipped", 0) + len(pts))
            for i in pts:
                dist[ci][i] = float("inf")
            return
        sub = {i: cand_m[i] for i in pts}
        dm = self._min_dists(sub, tgt_m, max(limit, INSIDE_POINT_TOL_M))
        for i in pts:
            d = dm.get(i, float("inf"))
            dist[ci][i] = d
            if d <= INSIDE_POINT_TOL_M:
                pass_hard[ci].add(i)

    def _exact(self, rows, constraints: list[Constraint], t0: float) -> list[Scored]:
        if not rows:
            return []
        # 用外包框中心分簇（链表里带的是 minx/miny/maxx/maxy）
        groups: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            cy = (r[8] + r[10]) / 2.0
            cx = (r[7] + r[9]) / 2.0
            groups[(int(math.floor(cy / CELL_DEG)),
                    int(math.floor(cx / CELL_DEG)))].append(i)
        self.log(f"  [geo] 候选分成 {len(groups)} 个区域做精确几何计算")

        n = len(constraints)
        dist: list[dict[int, float]] = [dict() for _ in range(n)]
        pass_hard: list[set[int]] = [set() for _ in range(n)]
        self._geo_stats = {"targets": 0, "tgt_sec": 0.0, "queries": 0,
                           "inside_point_skipped": 0}

        # ---- 预取目标要素：同一约束下把所有区域合并成几批查询 ----
        #
        # 原来是"每个区域 × 每个约束"各发一次查询。实测（大学查询，112 个区域、
        # 2 个距离约束）要发 224 次，每次约 0.27 秒的**固定开销**，
        # 光开销就吃掉 60 秒——比取回数据本身还贵。
        # 这里把各区域的经纬度范围按 24 个一批塞进临时表，一批只发一次查询。
        #
        # 区域范围直接取链表里的 minx/miny/maxx/maxy（就是经纬度包围框），
        # 不需要先把候选几何投影一遍，所以这一步很便宜。
        reg_bounds = {}
        for (glat, glon), idxs in groups.items():
            reg_bounds[(glat, glon)] = (
                min(rows[i][7] for i in idxs), min(rows[i][8] for i in idxs),
                max(rows[i][9] for i in idxs), max(rows[i][10] for i in idxs))
        prefetch: dict[tuple, list] = {}
        # cell 是分区列，只有全国数据集才有；小数据集（旧解析器产出）没有，
        # 无条件引用会直接报 Binder Error。这里先探一次。
        has_cell = "cell" in {d[0] for d in
                              self.con.execute("SELECT * FROM el LIMIT 0").description}
        for ci, c in enumerate(constraints):
            if c.op == "exists":
                continue
            if c.op == "inside":
                _lim0 = float(c.max_distance or INSIDE_TOL_M)
                _sr = max(_lim0, INSIDE_POINT_TOL_M) * 1.2
            elif c.op == "along":
                _sr = float(c.tolerance or 2000.0) * 1.2
            else:
                _sr = float(c.max_distance or 2000.0) * 1.2
            tw_pf = self.target_where(c, "el")
            keys = list(groups.keys())
            for s0 in range(0, len(keys), 24):
                part = keys[s0:s0 + 24]
                self.con.execute("DROP TABLE IF EXISTS _rb")
                self.con.execute("CREATE TEMP TABLE _rb(k INT, minx DOUBLE, miny DOUBLE,"
                                 " maxx DOUBLE, maxy DOUBLE)")
                vals, cells = [], set()
                for j, key in enumerate(part):
                    b = reg_bounds[key]
                    pd = deg_guard(_sr, max(abs(b[1]), abs(b[3])))
                    rbb = (b[0] - pd, b[1] - pd, b[2] + pd, b[3] + pd)
                    vals.append((j, rbb[0], rbb[1], rbb[2], rbb[3]))
                    for cl in range(int(math.floor(rbb[1] / 3)),
                                    int(math.floor(rbb[3] / 3)) + 1):
                        for cn in range(int(math.floor(rbb[0] / 3)),
                                        int(math.floor(rbb[2] / 3)) + 1):
                            cells.add(cl * 1000 + cn)
                self.con.executemany("INSERT INTO _rb VALUES (?,?,?,?,?)", vals)
                tcell = (f" AND el.cell IN ({','.join(map(str, sorted(cells)))})"
                         if has_cell and 0 < len(cells) <= 400 else "")
                _t = time.time()
                got = self.con.execute(f"""
                    SELECT r.k, el.geom_type, el.clon, el.clat, el.wkb
                    FROM _rb r JOIN el ON {tw_pf}
                      AND el.maxx >= r.minx AND el.minx <= r.maxx
                      AND el.maxy >= r.miny AND el.miny <= r.maxy
                    WHERE 1=1{tcell}
                """).fetchall()
                self._geo_stats["queries"] += 1
                self._geo_stats["tgt_sec"] += time.time() - _t
                for g in got:
                    prefetch.setdefault((ci, part[g[0]]), []).append(g)
        self.log(f"  [geo] 目标要素预取：{self._geo_stats['queries']} 次查询 "
                 f"[{time.time() - t0:.0f}s]")

        for gi, ((glat, glon), idxs) in enumerate(groups.items()):
            if gi % 5 == 0 and gi:
                self.log(f"    ... 已处理 {gi}/{len(groups)} 个区域  [{time.time() - t0:.0f}s]")
            epsg = utm_epsg(glon * CELL_DEG + CELL_DEG / 2,
                            glat * CELL_DEG + CELL_DEG / 2)
            to_m = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}",
                                        always_xy=True).transform

            # 几何就在链表行里（第 6 列），不需要再查主表
            items = [(i, None, None, None, rows[i][6]) for i in idxs]
            geo_map = self._project_many(to_m, items)
            cand_m = {}
            bounds = []
            for i in idxs:
                g_m = geo_map.get(i)
                if g_m is None:
                    continue
                cand_m[i] = g_m
                bounds.append(g_m.bounds)
            if not cand_m:
                continue
            if not cand_m:
                continue
            bx = [min(b[0] for b in bounds), min(b[1] for b in bounds),
                  max(b[2] for b in bounds), max(b[3] for b in bounds)]

            # 反推该区域在经纬度上的范围，用于按 bbox 取目标要素
            inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
            ll = [inv(bx[0], bx[1]), inv(bx[2], bx[3])]
            lon0, lat0 = min(ll[0][0], ll[1][0]), min(ll[0][1], ll[1][1])
            lon1, lat1 = max(ll[0][0], ll[1][0]), max(ll[0][1], ll[1][1])

            for ci, c in enumerate(constraints):
                if c.op == "exists":
                    # exists 的标签过滤已经在 SQL 链路里做过了（硬约束），
                    # 能走到这一步就说明通过；这里不再重复读标签 JSON（省内存）。
                    pass_hard[ci].update(cand_m.keys())
                    continue

                # inside 的有效半径跟候选类型有关：面/线是"里面就有的距离"，
                # 点（校园被画成节点）退化成一个大半径。
                # search_r 只影响"取回多少目标要素"，不影响判定结果：
                # 硬条件要求距离 ≤ limit，评分用的 proximity 在 d > limit 后已经饱和，
                # 所以比 limit 稍大一点就够了。
                #
                # 取回的目标数按半径**平方**增长，倍数给大了非常亏：
                # 原来 inside 给 3 倍、near/along 给 2 倍，全国实测一次要取回 19.5 万个
                # 目标要素、耗时 45 秒（占了精确阶段的一大半）。改成 1.2 倍后
                # 取回量降到约四分之一。
                if c.op == "inside":
                    limit = float(c.max_distance or INSIDE_TOL_M)
                    search_r = max(limit, INSIDE_POINT_TOL_M) * 1.2
                elif c.op == "along":
                    limit = float(c.tolerance or 2000.0)
                    search_r = limit * 1.2
                else:
                    limit = float(c.max_distance or 2000.0)
                    search_r = limit * 1.2

                # 目标要素优先用预取阶段的结果（见上面的 reg_bounds / prefetch）。
                raw = prefetch.get((ci, (glat, glon)), [])
                if raw:
                    tgt_m = list(self._project_many(
                        to_m, [(k, g[1], g[2], g[3], g[4])
                               for k, g in enumerate(raw)]).values())
                else:
                    # 回退：预取没拿到东西时按区域单独查一次。
                    # 保证正确性优先——预取只是提速手段，不能因为它少算目标而漏掉结果。
                    tw_fb = self.target_where(c, "el")
                    plat_fb = max(abs(lat0), abs(lat1))
                    pad_fb = deg_guard(search_r, plat_fb)
                    rbb_fb = (lon0 - pad_fb, lat0 - pad_fb, lon1 + pad_fb, lat1 + pad_fb)
                    tgt_rows_fb = self.con.execute(f"""
                        SELECT geom_type, clon, clat, wkb FROM el
                        WHERE {tw_fb}
                          AND maxx >= {rbb_fb[0]} AND minx <= {rbb_fb[2]}
                          AND maxy >= {rbb_fb[1]} AND miny <= {rbb_fb[3]}
                          {self.cell_in(rbb_fb) if has_cell else ''}
                    """).fetchall()
                    tgt_m = list(self._project_many(
                        to_m, [(k, g[0], g[1], g[2], g[3])
                               for k, g in enumerate(tgt_rows_fb)]).values())
                self._geo_stats["targets"] += len(tgt_m)
                if not tgt_m:
                    for i in cand_m:
                        if c.op == "not_exists":
                            pass_hard[ci].add(i)
                            dist[ci][i] = float("inf")
                    continue

                # inside 走**严格包含判定**，不走距离路径（见 _exact_inside）
                if c.op == "inside":
                    self._exact_inside(ci, c, cand_m, tgt_m, limit,
                                       pass_hard, dist)
                    continue

                dm = self._min_dists(cand_m, tgt_m, search_r)
                for i, g_m in cand_m.items():
                    d = dm.get(i, float("inf"))
                    dist[ci][i] = d
                    if c.op == "not_exists":
                        if d > limit:
                            pass_hard[ci].add(i)
                    else:  # near / along
                        if d <= limit:
                            pass_hard[ci].add(i)

        ok = set(range(len(rows)))
        for ci, c in enumerate(constraints):
            if c.hard:
                ok &= pass_hard[ci]
        st = self._geo_stats
        self.log(f"  [geo] 取回目标要素 {st['targets']} 个，耗时 {st['tgt_sec']:.0f}s  "
                 f"[{time.time() - t0:.0f}s]")
        if st.get("inside_point_skipped"):
            self.log(f"  [geo] 严格内部判定：另有 {st['inside_point_skipped']} 个候选的"
                     f"校园只被标成了一个**点**，无法判定\"内部\"，已排除"
                     f"（要放宽可用 --loose-inside）")

        out: list[Scored] = []
        for i in sorted(ok):
            r = rows[i]
            total, wsum = 0.0, 0.0
            parts: list[tuple[str, float, float]] = []
            for ci, c in enumerate(constraints):
                if c.op == "exists":
                    d, s = 0.0, 1.0
                else:
                    d = dist[ci].get(i, float("inf"))
                    if c.op == "near":
                        s = proximity(d, float(c.max_distance or 1))
                    elif c.op == "along":
                        s = proximity(d, float(c.tolerance or 1))
                    elif c.op == "inside":
                        # 严格包含判定下命中就是 0 米 -> 满分。
                        # 只有放宽模式（点状校园）才可能是个正距离，
                        # 那时也要用 700 米口径打分，而不是 120 米——
                        # 否则 300 米外的肯德基只有 14%，看着像判定失败。
                        lim_s = float(c.max_distance or INSIDE_TOL_M)
                        if r[11] == "Point":
                            lim_s = max(lim_s, INSIDE_POINT_TOL_M)
                        s = proximity(d, lim_s)
                    else:
                        lim = float(c.max_distance or 1)
                        s = min(1.0, d / (2 * lim)) if d != float("inf") else 1.0
                total += c.weight * s
                wsum += c.weight
                parts.append((c.describe(), s, d))
            base = total / wsum if wsum else 0.0
            conf = 0.6 + 0.4 * min(1.0, (r[5] or 0) / 8.0)
            if not r[4]:
                conf *= 0.7
            out.append(Scored(element=_Row(r), score=base * conf, parts=parts))
        return out


class _Row:
    """轻量行包装。

    约束链表已经自带几何和展示字段，所以这里不需要再查任何东西。
    刻意不保存完整的标签 JSON（全国规模下那是最大的内存开销），只留标签个数。
    """

    __slots__ = ("osm_type", "osm_id", "layer", "kind", "name",
                 "n_tags", "wkb", "clon", "clat")

    def __init__(self, r):
        self.osm_type, self.osm_id = r[0], int(r[1])
        self.layer, self.kind, self.name = r[2], r[3], r[4]
        self.n_tags = r[5] or 0
        self.wkb = r[6]
        # 用外包框中心作为展示坐标
        self.clon = (r[7] + r[9]) / 2.0
        self.clat = (r[8] + r[10]) / 2.0

    @property
    def ref(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    @property
    def geom_ll(self):
        return swkb.loads(self.wkb) if self.wkb else None
