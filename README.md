# dizhi · 地图地点约束检索

> 用一句中文描述，把"我要找的地方"变成若干**可验证的地理约束**，
> 在 OpenStreetMap 数据上逐层筛掉不可能的候选，按贴合度排序，
> 并生成一个本地地图页面供人工确认。

**它解决什么问题**：你记得某个地方的一些零碎特征——"旁边有条河""学校里面有
个湖和一家肯德基"——但想不起它在哪。这类问题的搜索空间是全国，而线索只有几条。

dizhi 把线索变成约束（`附近`/`内部`/`沿线`/`没有`），在两百多万个要素上逐层求交，
最后给你一个**按置信度排序的候选清单**和一张能点开看的地图。

> ⚠️ **它做的是"缩小范围"，不是"唯一确定"。** 输出永远是候选列表而非确定答案，
> 请结合实地信息人工核对。详见 [ENGINEERING.md](ENGINEERING.md) 的免责声明。

---

## 效果示例

```bash
dizhi query "一所高校，校园内有水体，有肯德基，有游泳馆" --nation --top 10
```

```
【第 1 步】我理解到的条件 （模型解析）
  1. 本身就是高校   [硬条件]
  2. 内部有水体     [硬条件]
  3. 内部有肯德基   [硬条件]
  4. 内部有游泳馆   [硬条件]

【第 2 步】逐层缩小范围 （全国 SQL 引擎）
  [sql] 本身就是高校: 2066192 -> 2774
  [sql] 内部有游泳馆:    2774 ->  912
  [sql] 内部有肯德基:     912 ->  835
  [sql] 内部有水体:       835 ->  813
  [geo] 精确判定后 142 个候选
  耗时 44.1s
```

（数据规模：全国 2070 万行 OSM 要素，约 2 GB Parquet。）

---

## 安装

```bash
git clone <你的仓库地址>
cd dizhi
pip install -e ".[all]"        # 全国建库还需要 osmium：pip install -e ".[ingest]"
cp .env.example .env           # 按需填写，不填也能跑规则式解析
```

## 快速开始

**① 还没有数据？** 小范围先试（不必下全国包）：

```bash
# 从 Overpass 抓一个城市范围
python -m dizhi.ingest.overpass --bbox 118.6,31.9,119.0,32.3 --out data/nanjing.parquet
dizhi query "长江边上2公里内，附近有化学厂" --data data/nanjing.parquet
```

**② 要全国规模？** 三步建库：

```bash
# 1) 下载 .osm.pbf（约 1.5 GB；分块并发 + 断点续传，可随时中断重跑）
python -m dizhi.ingest.osm --region china --dest data/china.osm.pbf

# 2) 解析成 Parquet（这一步最慢，约 1.5 小时；分片落在 data/parquet/）
python -m dizhi.ingest.pbf --pbf data/china.osm.pbf \
       --out data/parquet --tmp data/tmp

# 3) 按地理格子重分区 —— **不做这步全国查询会慢一个数量级**
python -m dizhi.ingest.partition --src data/parquet --dst data/parquet_geo
```

然后在 `.env` 里指一下：

```ini
DIZHI_NATION=data/parquet_geo/**/*.parquet
```

```bash
dizhi query "一所高校，校园内有水体，有肯德基" --nation --top 10 --map map.html
```

**③ 用地图看结果**

```bash
python tools/serve_map.py map.html     # 打开 http://127.0.0.1:8899/
```

（为什么需要一个本地服务器：见下面"已知问题"。）

---

## 支持的约束

| 说法 | 算子 | 含义 |
|---|---|---|
| 长江**边上**2公里内 | `along` | 在某条线要素（河流/道路）附近 |
| 学校**附近**有地铁 | `near` | 目标在候选**外部**若干米（默认 2 公里） |
| 校**内**有肯德基 / **内部**有水体 | `inside` | 目标**真的落在候选多边形里面** |
| 不要有居民区 | `not_exists` | 附近不存在某类要素 |
| 找一个大学 | `exists` | 候选本身就是这类要素 |

**`inside` 与 `near` 的区别是刻意的**：前者要求目标落在多边形内部
（点目标用严格包含判定），后者只要求距离在某范围内。混用会显著改变结果。

## 自然语言解析：两种后端

| 后端 | 命令 | 特点 |
|---|---|---|
| 规则式 | `--nlu rule` | 零依赖、离线、快。覆盖常见句式，靠同义词表 |
| 大模型 | `--nlu llm` | 容错强，能处理复杂句式。需要一个 API key |
| 自动 | `--nlu auto`（默认） | 先试模型，失败自动降级到规则式 |

**模型只负责"翻译"，不负责"判断"**：它输出一份受严格约束的 JSON
（算子 + OSM 标签 + 距离），标签 key 必须在白名单内、距离必须被夹在合理区间，
任何不合规的输出都会被丢弃。**模型看不到任何地图数据**，因此不存在
"编造一个地点"的路径——结果永远来自 OSM。

## 主动提问

候选太多时，可以让它自己挑"最能砍掉一半候选"的问题来问你：

```bash
dizhi query "找一个大学" --nation --ask
```

```
【主动提问 1】候选还有 592 个，问一个能砍掉最多的：内部或旁边有【地铁站】吗？
             （答 y 有 / n 没有 / 回车跳过；答完剩下的不会超过 301 个）
  > y
  -> 缩小到 275 个
```

问题库和打分逻辑都在 `src/dizhi/config.py`，容易按自己的场景扩充。

---

## 项目结构

```
dizhi/
├── src/dizhi/
│   ├── cli.py            命令行入口（query / info / parse / version）
│   ├── settings.py       **所有路径与密钥集中在这里，代码里零硬编码**
│   ├── config.py         OSM 标签词典、中文别名表、提问库（纯数据）
│   ├── nlu/              自然语言 -> 约束
│   │   ├── rule.py         规则式解析器
│   │   └── llm.py          大模型解析器
│   ├── ops/              约束求值与打分
│   │   ├── operators.py    数据模型 + 贴合度函数
│   │   ├── engine.py       DuckDB 空间引擎（全国规模）
│   │   └── memory.py       内存引擎（小范围、启动快）
│   ├── ingest/           原始 OSM -> 可检索数据（三段互相独立）
│   │   ├── osm.py          下载 .osm.pbf
│   │   ├── pbf.py          .osm.pbf -> Parquet
│   │   ├── partition.py    按地理格子重分区
│   │   └── overpass.py     小范围数据走 Overpass
│   └── providers/        第三方地图 API（独立可调用）
│       ├── amap.py         高德 Web 服务（含 GCJ-02 -> WGS-84 转换）
│       └── tianditu.py     天地图（搜索 + 瓦片）
├── tools/
│   ├── fetch_deps.py     在没有网络的构建机上下依赖
│   └── serve_map.py      本地看地图（代理天地图瓦片）
├── ENGINEERING.md        工程轨迹、踩坑记录与免责声明
└── pyproject.toml
```

**每一层都可以单独用**，互相之间不产生隐式依赖：

```python
# 只要解析
from dizhi.nlu.rule import parse
cs, warns = parse("长江边上2公里内，附近有化学厂")

# 只要第三方 API
from dizhi.providers import AmapClient
pois = AmapClient("你的key").search_nationwide("智能建造学院")

# 只要下载
from dizhi.ingest import osm
osm.fetch("china", "china.osm.pbf", conns=16)
```

---

## 已知问题与取舍

- **坐标偏移**：高德返回 GCJ-02，与 WGS-84 有 50–500 米偏移。要叠图必须先转换
  （`providers/amap.py` 里有 `gcj02_to_wgs84`）。天地图是 CGCS2000，与 WGS-84 基本重合。
- **天地图底图在本地文件里是白图**：服务器端 key 强制要求 `Referer`，
  而浏览器对 `file://` 页面发的 Referer 无法满足、JS 也伪造不了。
  用 `tools/serve_map.py`，或在控制台另申请一个**浏览器端** key。
- **OSM 的 POI 覆盖不均**：连锁餐饮、校内设施在内地城市的覆盖率明显偏低。
  如果某个约束一直筛不出结果，很可能是数据本身没有，而不是算法问题。
  可以拿另一个数据源（高德/天地图）交叉验证——这正是 `providers/` 存在的原因。
- **近似预排序**：候选超过阈值时会先用 SQL 里顺带算出的近似距离预排，
  只对最靠前的一批做精确几何判定。命令行输出里会明确标注"精确校验了多少个"。

## 许可

[MIT](LICENSE)。
使用 OSM 数据请遵守 [ODbL](https://www.openstreetmap.org/copyright)；
使用高德/天地图服务请遵守各自的服务条款。
