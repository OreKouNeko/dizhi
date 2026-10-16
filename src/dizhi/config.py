"""全局配置：标签白名单、中文同义词表、默认范围。

设计原则（对应方案 v0.2 第 8 节）：
- OSM 标签有"方言"问题，同一个意思可能有好几种写法 → 用「同义词表」映射成"一组 OR 条件"。
- 排除建筑轮廓（building=*）等数量巨大但检索价值低的要素，否则全国数据体积会失控。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 范围默认值
# P0 只处理南京 — 扬州 — 镇江 一带的长江沿线（约 200km × 200km）。
# 链路验证通过后，把这里换成全国即可，代码无需改动。
DEFAULT_BBOX = (117.60, 31.10, 119.95, 33.05)  # (min_lon, min_lat, max_lon, max_lat)
DEFAULT_BBOX_LABEL = "南京—镇江—扬州 长江沿线"

# ---------------------------------------------------------------- 标签处理
# 只保留含下列任一 key 的要素（建筑除外），大幅压缩数据体积。
INTERESTING_KEYS = {
    "amenity", "shop", "tourism", "historic", "man_made", "power", "leisure",
    "office", "craft", "healthcare", "emergency", "public_transport",
    "landuse", "industrial", "natural", "waterway", "railway", "highway",
    "boundary", "place", "military", "aeroway", "harbour", "water", "bridge",
    "tunnel", "dam", "reservoir", "quarry", "dock", "port", "barrier",
}

# 建筑单独处理：中国有上千万栋建筑轮廓，全收进来会把数据撑爆；
# 但**带名字的建筑**（"4号楼""图书馆""物流中心"）恰恰是校园里最有辨识度的结构，
# 全国只有几十万到几百万个，体量完全可控。
# 规则：building 只保留**同时有 name** 的要素。
BUILDING_KEY = "building"
KEY_FILTER_KEYS = INTERESTING_KEYS | {BUILDING_KEY}

# 明确丢弃：数量巨大、或纯元数据、或涉及隐私的标签。
TAG_BLOCKLIST = {
    "building", "addr:housenumber", "addr:street", "addr:postcode", "addr:city",
    "source", "source:name", "created_by", "note", "fixme", "import",
    "tiger:county", "tiger:tlid", "odbl", "attribution", "wikidata",
    "wikipedia", "website", "phone", "email", "fax", "opening_hours",
}
BLOCKLIST_PREFIXES = ("addr:", "source:", "tiger:", "ref:", "gnis:", "nysgissam:")

# highway 只保留"有检索意义"的值（否则会把全国每条小路都收进来）。
HIGHWAY_KEEP = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "bus_stop", "motorway_junction", "services",
    "rest_area", "ford", "dam", "ferry", "crossing",
}
RAILWAY_KEEP = {
    "station", "halt", "yard", "junction", "level_crossing", "rail",
    "tram", "subway_entrance", "roundhouse", "turntable", "buffer_stop",
}

# 判断 way 是"面"还是"线"时参考的 key。
AREA_KEYS = {
    "landuse", "leisure", "amenity", "boundary", "power", "man_made",
    "industrial", "aeroway", "military", "place", "natural", "water",
    "reservoir", "quarry", "harbour", "dock", "port",
}
LINE_KEYS = {"waterway", "railway", "highway", "power", "barrier", "natural"}

# 用于生成 kind（归一化主类别）的 key 优先级。
KIND_PRIORITY = [
    "waterway", "railway", "highway", "aeroway", "power", "man_made",
    "landuse", "industrial", "amenity", "shop", "tourism", "historic",
    "leisure", "office", "craft", "healthcare", "emergency",
    "public_transport", "natural", "place", "military", "boundary",
    "barrier", "harbour", "water",
    # building 放最后：只有前面都没命中时才用它分类，这样
    # "带名字的教学楼"如果同时有 amenity=* 会优先按 amenity 分类
    "building",
]


def is_blocked(key: str) -> bool:
    return key in TAG_BLOCKLIST or key.startswith(BLOCKLIST_PREFIXES)


# ---------------------------------------------------------------- 中文同义词表
# 每条：中文说法 -> 一组 OR 条件（每个条件是 {key: value} 的 AND 关系）。
# 加新说法只需在这里加一行，不用改代码。
ALIASES: dict[str, list[dict[str, str]]] = {
    # —— 校园相关（目标实体是大学时最常用）
    "大学": [{"amenity": "university"}],
    "高校": [{"amenity": "university"}, {"amenity": "college"}],
    "学院": [{"amenity": "college"}, {"amenity": "university"}],
    "校区": [{"amenity": "university"}, {"name~": "校区"}],
    "智能制造学院": [{"name~": "智能制造"}],
    "图书馆": [{"amenity": "library"}],
    "食堂": [{"amenity": "canteen"}, {"amenity": "restaurant"}, {"name~": "食堂"}],
    "宿舍": [{"name~": "宿舍"}, {"name~": "公寓"}],
    "体育馆": [{"leisure": "sports_centre"}, {"leisure": "stadium"}],
    # 游泳馆必须**严格**判定：leisure=sports_centre 覆盖面太宽（任何体育馆都算），
    # 实测会让"校园内有游泳馆"这条把只有体育馆的学校也放进来。
    # 只有 leisure=swimming_pool（泳池本体）或 sports_centre + sport=swimming
    # （带游泳标签的体育场馆）才算数。
    "游泳馆": [{"leisure": "swimming_pool"},
               {"leisure": "sports_centre", "sport": "swimming"},
               {"amenity": "swimming_pool"}],
    "游泳池": [{"leisure": "swimming_pool"}, {"amenity": "swimming_pool"}],
    "田径场": [{"leisure": "track"}, {"leisure": "pitch", "sport": "athletics"}],
    "操场": [{"leisure": "pitch"}, {"leisure": "track"}],
    "教学楼": [{"name~": "教学楼"}, {"name~": "教学"}],
    "实验楼": [{"name~": "实验楼"}, {"name~": "实验室"}],
    # —— 品牌 / 连锁。必须按名称匹配：只写 amenity=fast_food 会把所有快餐都算进来
    "肯德基": [{"name~": "肯德基"}, {"name~": "KFC"}],
    "麦当劳": [{"name~": "麦当劳"}, {"name~": "McDonald"}],
    "星巴克": [{"name~": "星巴克"}, {"name~": "Starbucks"}],
    "瑞幸": [{"name~": "瑞幸"}, {"name~": "luckin"}],
    "全家": [{"name~": "全家"}],
    "罗森": [{"name~": "罗森"}],
    # —— 水体
    "水体": [{"natural": "water"}, {"waterway": "riverbank"}],
    "水面": [{"natural": "water"}],
    "池塘": [{"natural": "water", "water": "pond"}],
    # —— 化工 / 工业
    "化工厂": [
        {"landuse": "industrial", "industrial": "chemical"},
        {"man_made": "works"},
        {"landuse": "industrial"},
    ],
    "工厂": [{"man_made": "works"}, {"landuse": "industrial"}],
    "工业园区": [{"landuse": "industrial"}],
    "工业区": [{"landuse": "industrial"}],
    "油库": [{"man_made": "storage_tank"}, {"landuse": "industrial"}],
    "储罐": [{"man_made": "storage_tank"}],
    "烟囱": [{"man_made": "chimney"}],
    "码头": [{"man_made": "pier"}, {"waterway": "dock"}, {"harbour": "yes"}],
    "港口": [{"harbour": "yes"}, {"landuse": "harbour"}],
    "矿山": [{"landuse": "quarry"}, {"man_made": "mineshaft"}],
    "采石场": [{"landuse": "quarry"}],
    "污水处理厂": [{"man_made": "wastewater_plant"}],
    "垃圾处理": [{"landuse": "landfill"}],
    "变电站": [{"power": "substation"}],
    "发电厂": [{"power": "plant"}],
    "电厂": [{"power": "plant"}],
    # —— 电力
    "铁塔": [{"power": "tower"}, {"man_made": "tower"}],
    "输电塔": [{"power": "tower"}],
    "高压线": [{"power": "line"}, {"power": "minor_line"}],
    "电线杆": [{"power": "pole"}],
    "风车": [{"generator:source": "wind"}, {"power": "generator"}],
    "风力发电机": [{"generator:source": "wind"}],
    "光伏": [{"generator:source": "solar"}, {"power": "plant"}],
    # —— 宗教 / 文化
    "寺庙": [{"amenity": "place_of_worship"}],
    "教堂": [{"amenity": "place_of_worship", "religion": "christian"}],
    "清真寺": [{"amenity": "place_of_worship", "religion": "muslim"}],
    "道观": [{"amenity": "place_of_worship", "religion": "taoist"}],
    "塔": [{"man_made": "tower"}, {"historic": "tower"}],
    "古塔": [{"historic": "tower"}, {"man_made": "tower"}],
    "碑": [{"historic": "monument"}],
    "遗址": [{"historic": "ruins"}, {"historic": "archaeological_site"}],
    # —— 交通
    "火车站": [{"railway": "station"}],
    "高铁站": [{"railway": "station", "highspeed": "yes"}, {"railway": "station"}],
    "地铁站": [{"railway": "station", "subway": "yes"}, {"station": "subway"}],
    "汽车站": [{"amenity": "bus_station"}],
    "公交站": [{"highway": "bus_stop"}],
    "机场": [{"aeroway": "aerodrome"}],
    "桥": [{"man_made": "bridge"}, {"bridge": "yes"}],
    "隧道": [{"tunnel": "yes"}],
    "收费站": [{"barrier": "toll_booth"}],
    "加油站": [{"amenity": "fuel"}],
    "服务区": [{"highway": "services"}],
    "停车场": [{"amenity": "parking"}],
    # —— 公共服务
    "医院": [{"amenity": "hospital"}, {"amenity": "clinic"}],
    "学校": [{"amenity": "school"}],
    "大学": [{"amenity": "university"}],
    "政府": [{"office": "government"}, {"amenity": "townhall"}],
    "派出所": [{"amenity": "police"}],
    "消防": [{"amenity": "fire_station"}],
    "邮局": [{"amenity": "post_office"}],
    "银行": [{"amenity": "bank"}],
    "超市": [{"shop": "supermarket"}],
    "市场": [{"amenity": "marketplace"}],
    "饭店": [{"amenity": "restaurant"}],
    "酒店": [{"tourism": "hotel"}],
    "景点": [{"tourism": "attraction"}, {"tourism": "viewpoint"}],
    # —— 自然 / 水系
    "山": [{"natural": "peak"}],
    "山峰": [{"natural": "peak"}],
    "湖": [{"natural": "water", "water": "lake"}, {"natural": "water"}],
    "水库": [{"landuse": "reservoir"}, {"natural": "water", "water": "reservoir"}],
    "河": [{"waterway": "river"}],
    "运河": [{"waterway": "canal"}],
    "溪流": [{"waterway": "stream"}],
    "海滩": [{"natural": "beach"}],
    "森林": [{"landuse": "forest"}, {"natural": "wood"}],
    "农田": [{"landuse": "farmland"}],
    "居民区": [{"landuse": "residential"}],
    "村庄": [{"place": "village"}, {"place": "hamlet"}],
    "城镇": [{"place": "town"}],
    "城市": [{"place": "city"}],
}

# 主动提问的问题库：候选太多时，按"信息增益最大"依次问。
# 只放**一眼能看出来**且在中国普遍存在的要素——用户答不上来的问题没有价值。
# 提问一律用 inside 语义：候选是校园/园区这类面要素时问"里面有没有"，
# 候选是点要素时自动退化成"周边 700 米内有没有"。
QUESTION_LIBRARY: list[tuple[str, list[dict[str, str]]]] = [
    # —— 校园场景（目标实体是大学时最有用）
    ("湖或河（校园里的水面）", [{"natural": "water"}, {"waterway": "river"}]),
    ("田径场或操场", [{"leisure": "track"}, {"leisure": "pitch"}]),
    ("宿舍楼", [{"name~": "宿舍"}, {"name~": "公寓"}]),
    ("食堂", [{"amenity": "canteen"}, {"amenity": "restaurant"}, {"name~": "食堂"}]),
    ("图书馆", [{"amenity": "library"}]),
    ("体育馆", [{"leisure": "sports_centre"}, {"leisure": "stadium"}]),
    # —— 通用
    ("肯德基或麦当劳", [{"name~": "肯德基"}, {"name~": "KFC"},
                        {"name~": "麦当劳"}, {"name~": "McDonald"}]),
    ("地铁站", [{"railway": "station", "subway": "yes"}, {"station": "subway"},
                {"railway": "subway_entrance"}]),
    ("火车站", [{"railway": "station"}]),
    ("超市", [{"shop": "supermarket"}, {"shop": "convenience"}]),
    ("银行", [{"amenity": "bank"}]),
    ("医院或诊所", [{"amenity": "hospital"}, {"amenity": "clinic"}]),
    ("寺庙或教堂", [{"amenity": "place_of_worship"}]),
    ("加油站", [{"amenity": "fuel"}]),
    ("公园", [{"leisure": "park"}]),
    ("高速公路或主干道", [{"highway": "motorway"}, {"highway": "trunk"},
                          {"highway": "primary"}]),
    ("输电铁塔", [{"power": "tower"}, {"man_made": "tower"}]),
    ("工厂", [{"man_made": "works"}, {"landuse": "industrial"}]),
    ("机场", [{"aeroway": "aerodrome"}]),
    ("山或丘陵", [{"natural": "peak"}, {"natural": "hill"}]),
]

# 中文里表示"距离"的写法 → 米
DISTANCE_UNITS = {
    "公里": 1000.0, "千米": 1000.0, "km": 1000.0, "KM": 1000.0,
    "米": 1.0, "m": 1.0, "M": 1.0,
}

# 中文里表示"沿线"的写法
ALONG_WORDS = ("沿", "沿着", "沿线", "边上", "旁边", "两侧", "两岸")
