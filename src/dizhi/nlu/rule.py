"""规则式中文解析器：把一句话变成约束列表。

覆盖的句式（P0）：
    "长江边上2公里内"        -> along  长江, tolerance=2000
    "附近3公里有化工厂"      -> near   化工厂, 3000m
    "附近有铁塔"             -> near   铁塔, 默认 2000m
    "不要有居民区"           -> not_exists 居民区, 默认 200m
    "找一个寺庙"             -> exists 寺庙

解析不出来时不会瞎猜：返回 warnings，由 CLI 提示用户换种说法。
"""
from __future__ import annotations

import re

from dizhi.config import ALIASES
from dizhi.ops.operators import Constraint

SEP_RE = re.compile(r"[，,、;；]|并且|而且|同时|以及")
DIST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(公里|千米|[kK][mM]|米|[mM])")
ALONG_RE = re.compile(
    r"(?:沿着|沿|在)?\s*([\u4e00-\u9fa5A-Za-z0-9]{1,14}?)\s*(?:沿线|边上|两侧|两岸|沿岸|一带)")
NEG_WORDS = ("没有", "不要", "不含", "不挨着", "远离", "避开", "别挨着")
FIND_WORDS = ("找一个", "找个", "找", "目标是", "我要找", "想要")
# 表示"外部邻近"的标记，说明这一项（以及紧随其后的清单项）都是 near
NEAR_SCOPE_WORDS = ("附近", "旁边", "周边", "周围", "近旁", "邻近",
                    "不远处", "紧邻", "隔壁")
# 清单项：以"有/还有/要有/包括"开头的那种，它自己不带作用域，
# 要继承前面那句话定下的作用域。
LIST_RE = re.compile(r"^\s*(?:还|也|又|再)?\s*(?:要|会)?\s*(?:有|包括|含有)")
# "内部包含"的说法。必须和"附近有"区分开：
#   "校园里有肯德基"   -> inside（点在校园多边形内部）
#   "校园旁边有肯德基" -> near （点在校园外若干米）
# 这里只用"地点内部"的标记词；"附近/旁边/周边"不在其中，所以不会被误判。
INSIDE_WORDS = ("内部有", "内部还有", "内部", "里面有", "里面", "内有",
                "校内", "园内", "厂内", "院内", "馆内", "境内",
                "内部包含", "包含")

DEFAULT_NEAR = 2000.0
DEFAULT_ALONG = 2000.0
DEFAULT_NOT = 200.0
DEFAULT_INSIDE = 120.0


def to_meters(value: str, unit: str) -> float:
    u = unit.lower()
    if u in ("公里", "千米", "km"):
        return float(value) * 1000.0
    return float(value)


def find_distance(seg: str) -> tuple[float | None, str]:
    """返回 (米, 去掉距离后的句子)。"""
    m = DIST_RE.search(seg)
    if not m:
        return None, seg
    d = to_meters(m.group(1), m.group(2))
    return d, (seg[:m.start()] + seg[m.end():])


def match_alias(seg: str) -> tuple[str | None, list[dict[str, str]] | None]:
    """找出句子里最长的中文同义词，返回 (词, 标签条件组)。"""
    best = None
    for word in ALIASES:
        if word in seg and (best is None or len(word) > len(best)):
            best = word
    return (best, ALIASES[best]) if best else (None, None)


def parse(text: str) -> tuple[list[Constraint], list[str]]:
    """把一句话拆成若干项，逐项判定它的算子。

    关键点是**作用域继承**：中文里"校园内有水体，有肯德基，有游泳馆"
    只在第一项写了"校园内"，后面两项是靠语境共享这个作用域的。
    不继承的话，"有肯德基"会被当成"附近 2 公里内有肯德基"，
    语义完全跑偏（实测会把公园、商场都当成候选）。
    """
    constraints: list[Constraint] = []
    warnings: list[str] = []
    seen: set[str] = set()
    # 清单项默认继承的作用域。被"内部/附近/没有"这类词改写，
    # 但**不会被 along 改写**——"长江边上有化工厂"里的化工厂不是"沿长江"。
    last_scope = "exists"

    for raw in SEP_RE.split(text or ""):
        seg = (raw or "").strip()
        if not seg:
            continue

        along = ALONG_RE.search(seg)
        dist, cleaned = find_distance(seg)
        negated = any(w in seg for w in NEG_WORDS)
        wants_find = any(w in seg for w in FIND_WORDS)
        wants_inside = any(w in seg for w in INSIDE_WORDS)
        wants_near = any(w in seg for w in NEAR_SCOPE_WORDS)

        if along and not negated:
            scope = "along"
        elif negated:
            scope = "not_exists"
        elif wants_inside:
            scope = "inside"
        elif wants_near:
            scope = "near"
        elif wants_find:
            scope = "exists"
        elif LIST_RE.match(seg):
            scope = last_scope        # "有肯德基" 继承前面"校园内有…"的作用域
        else:
            # 光秃秃一个名词（"一所高校"）：表示要找的东西本身就是它
            scope = "exists"
        if scope in ("inside", "near", "not_exists"):
            last_scope = scope

        word, filters = match_alias(cleaned)

        if scope == "along":
            name = along.group(1).strip()
            # "长江边上" 这类：目标要素名可能被误当成长江，做一次校正
            if word and name.endswith(word):
                name = name[: -len(word)]
            tol = dist or DEFAULT_ALONG
            key = f"along:{name}:{tol}"
            if name and key not in seen:
                seen.add(key)
                constraints.append(Constraint(
                    op="along", feature_name=name, tolerance=tol,
                    label=name, cid=f"c{len(constraints) + 1}"))
            # 一个句子里可能同时有河和 POI："长江边上有化工厂"。
            # 只发 along 会把化工厂丢掉，所以再补一条。
            # 注意 along 是**按名称去线要素里查**的（不在别名表里），
            # 名字不是线要素时它会命中 0 个目标、把整个查询弄空——
            # 所以只有确实识别出了一个不同的实体时才补，不硬造。
            if word and name and word != name and word not in name:
                d2 = DEFAULT_NEAR
                key2 = f"near:{word}:{d2}"
                if key2 not in seen:
                    seen.add(key2)
                    constraints.append(Constraint(
                        op="near", filters=filters, max_distance=d2,
                        label=word, cid=f"c{len(constraints) + 1}"))
            continue

        if not word:
            if dist is not None or along:
                warnings.append(f"「{seg}」里识别到距离/位置，但没认出要找的东西")
            else:
                warnings.append(f"「{seg}」没看懂，已忽略")
            continue

        # "内部有 X" 排在"附近有 X"前面：语义完全不同
        if scope == "inside":
            d = dist if dist is not None else DEFAULT_INSIDE
            key = f"inside:{word}:{d}"
            if key not in seen:
                seen.add(key)
                constraints.append(Constraint(
                    op="inside", filters=filters, max_distance=d, label=word,
                    cid=f"c{len(constraints) + 1}"))
            continue

        if scope == "not_exists":
            d = dist if dist is not None else DEFAULT_NOT
            key = f"not:{word}:{d}"
            if key not in seen:
                seen.add(key)
                constraints.append(Constraint(
                    op="not_exists", filters=filters, max_distance=d, label=word,
                    cid=f"c{len(constraints) + 1}"))
            continue

        if scope == "exists" and dist is None:
            key = f"exists:{word}"
            if key not in seen:
                seen.add(key)
                constraints.append(Constraint(
                    op="exists", filters=filters, label=word,
                    cid=f"c{len(constraints) + 1}"))
            continue

        d = dist if dist is not None else DEFAULT_NEAR
        key = f"near:{word}:{d}"
        if key not in seen:
            seen.add(key)
            constraints.append(Constraint(
                op="near", filters=filters, max_distance=d, label=word,
                cid=f"c{len(constraints) + 1}"))

    if not constraints:
        warnings.append("整句话没有解析出任何有效条件")
    return constraints, warnings
