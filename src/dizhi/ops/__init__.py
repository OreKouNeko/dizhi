"""检索引擎层：约束求值、候选打分排序。

    memory.Store          —— 内存引擎，适合小范围数据、启动快
    engine.SqlEngine      —— DuckDB 空间引擎，走 GeoParquet，支撑全国规模
    operators             —— 两者共用的数据模型（Constraint / Scored / SearchResult）
"""
