"""数据导入层：把原始 OSM 变成检索引擎能用的 GeoParquet。

三段互相独立，可以分开调用、也可以只用其中一段：

    osm.fetch("china", "china.osm.pbf")   —— 下载 .osm.pbf（分块并发 + 续传）
    pbf.build(pbf, out, tmp)              —— .osm.pbf -> parquet（三遍扫描）
    partition.run(src, dst)               —— 按地理格子重分区（查询提速的关键）
    overpass.fetch_bbox(...)              —— 小范围数据走 Overpass，不必下全国包
"""
