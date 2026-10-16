"""把建好的数据集按地理格子重新分区 —— 独立模块，不依赖检索引擎。

### 为什么必须做这一步

`pbf.build()` 写出的分片是按 OSM 原始顺序（节点 ID 升序）落的，
同一片里的要素在地理上东一个西一个。于是**任何带 bbox 的查询都无法跳过数据块**，
实测全国检索里"取回目标要素"能花掉 157 秒，全耗在扫描不相干的数据上。

按 3°×3° 的地理格子分区后，带 bbox 的查询只会碰到少数几个文件。
配合查询里的 `cell IN (...)` 显式剪枝，同一次 bbox 查询从"遍历全部文件"
降到 0.09 秒量级。

### 一个反直觉的坑

分区写出时 DuckDB 会为「每个分区 × 每个线程」各写一个文件。
用 4 线程写出来 1876 个文件，而实际只有 170 个格子——
每次查询光读这些文件的页脚就要好几秒。
**限制成单线程后文件数就等于格子数**，查询反而更快。

    from dizhi.ingest import partition
    partition.run("parquet/", "parquet_geo/", cell_deg=3.0)
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import duckdb

from dizhi.settings import MEMORY_LIMIT, TEMP_DIR


def run(src: str, dst: str, cell_deg: float = 3.0,
        temp: str = "", memory: str = "", quiet: bool = False) -> int:
    """把 `src` 目录下的 parquet 按地理格子重新写到 `dst`。

    返回 0 表示行数一致，1 表示不一致（用于脚本判成败）。
    """
    t0 = time.time()
    src_glob = os.path.join(src, "*.parquet").replace("\\", "/")
    tmp = temp or TEMP_DIR
    Path(tmp).mkdir(parents=True, exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst)

    con = duckdb.connect()
    con.execute("PRAGMA threads=1")          # 见模块文档：多线程会把文件数放大
    con.execute(f"SET memory_limit='{memory or MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{tmp.replace(chr(92), '/')}'")

    n = con.execute(f"SELECT count(*) FROM read_parquet('{src_glob}')").fetchone()[0]
    if not quiet:
        print(f"[源] {n} 行  [{time.time() - t0:.0f}s]", flush=True)

    d = cell_deg
    expr = (f"CAST(FLOOR(clat / {d}) AS INT) * 1000 "
            f"+ CAST(FLOOR(clon / {d}) AS INT)")
    dst_posix = dst.replace("\\", "/")
    if not quiet:
        print(f"[写出] 按 {d}° 格子分区 ...", flush=True)
    con.execute(f"""
        COPY (
            SELECT osm_type, osm_id, layer, kind, name, tags, geom_type, wkb,
                   clon, clat, minx, miny, maxx, maxy,
                   {expr} AS cell
            FROM read_parquet('{src_glob}')
        ) TO '{dst_posix}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000,
            PARTITION_BY (cell), OVERWRITE_OR_IGNORE
        )
    """)

    files = sum(len(fs) for _, _, fs in os.walk(dst))
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(dst) for f in fs) / 1024 / 1024
    n2 = con.execute(
        f"SELECT count(*) FROM read_parquet('{dst_posix}/**/*.parquet')"
    ).fetchone()[0]
    con.close()
    if not quiet:
        print(f"[完成] {files} 个文件，{size:.0f} MB，{n2} 行  "
              f"[{time.time() - t0:.0f}s]")
    return 0 if n2 == n else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="按地理格子重新分区（查询提速的关键一步）")
    ap.add_argument("--src", required=True, help="输入的 parquet 目录")
    ap.add_argument("--dst", required=True, help="输出目录")
    ap.add_argument("--cell-deg", type=float, default=3.0, help="格子边长（度）")
    ap.add_argument("--temp", default="", help="DuckDB 溢写目录")
    a = ap.parse_args()
    return run(a.src, a.dst, a.cell_deg, a.temp)


if __name__ == "__main__":
    raise SystemExit(main())
