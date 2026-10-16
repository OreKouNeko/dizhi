"""用大模型把中文翻译成约束条件（方案 v0.2 第 9 节的方案 A）。

分工原则（很重要）：
    **模型只负责"翻译"，不负责"判断"**。
    - 模型输出一份受严格约束的 JSON：算子 + OSM 标签 + 距离
    - 标签 key 必须在我们允许的集合里，距离必须在合理区间，算子必须在四种之内
    - 任何不合规的输出都会被丢弃并降级到规则式解析器
    - 检索结果永远来自 OSM 数据，模型无法凭空"推荐"一个地点

模型看不到任何地图数据，它只看到用户那句话，所以不存在"编造地点"的路径。
"""
from __future__ import annotations

import json
from dataclasses import replace
import os
import re
import urllib.error
import urllib.request

from dizhi import settings
from dizhi.config import ALIASES, INTERESTING_KEYS
from dizhi.ops.operators import Constraint

OPS = {"along", "near", "exists", "not_exists", "inside"}
MAX_DISTANCE_M = 200_000.0     # 200 公里，超过一律视为模型胡说
MIN_DISTANCE_M = 10.0

ALLOWED_KEYS = sorted(INTERESTING_KEYS - {"building"})
# name~ 是"名称包含"的特殊键（品牌类目标必须用它，例如肯德基不能只按
# amenity=fast_food 匹配）。它不是 OSM 标签键，所以要单独放行。
ALLOWED_KEYS_SET = set(ALLOWED_KEYS) | {"name~"}

SYSTEM_PROMPT = """你是地理检索助手的"翻译模块"。用户会用中文描述一个地点的特征，\
你的任务是把这句话翻译成一份 JSON 形式的检索条件。你不需要、也不允许给出任何具体地点。

只能使用这五种算子：
- "along"       位于某条命名线状要素（河流、公路）沿线。需要 feature_name 和 tolerance
- "inside"      候选地点**内部包含**某类要素（"校园里有肯德基"、"内部有水体"）。需要 filters
- "near"        附近存在某类要素（"旁边有个加油站"）。需要 filters 和 max_distance
- "exists"      本身就该是某类要素（"我要找一个大学"）。需要 filters
- "not_exists"  附近不能有某类要素。需要 filters 和 max_distance

"内部有"和"附近有"必须区分开：说"里面有/内部有/校内有/园区内有"时用 inside，
说"附近/旁边/周边/不远处有"时用 near。

filters 是一个"或"的列表，列表里每一项是若干 OSM 标签的"与"。
例如"化工厂"翻译成：
[{{"landuse":"industrial","industrial":"chemical"}}, {{"man_made":"works"}}]
标签 key 只能从下面这个集合里选：
{allowed}

另外有一个特殊键 `name~`，表示"名称里包含"。**品牌和专有名词必须用它**——
比如"肯德基"要写成 [{{"name~":"肯德基"}}]，不能只写 amenity=fast_food
（那会把所有快餐店都算进来）。同理"智能制造学院"要写成 [{{"name~":"智能制造"}}]。

**不要用过宽的标签**。反例："游泳馆"如果写成 leisure=sports_centre，
任何体育馆/体育中心都会命中，判定就失去意义了。正确写法是
[{{"leisure":"swimming_pool"}}, {{"leisure":"sports_centre","sport":"swimming"}}]。

已知的中文说法对照表（优先使用，也可以用你更准确的判断）：
{alias_sample}

距离单位一律用米。如果用户没说距离，附近默认 2000，沿线默认 2000，排除默认 200，
内部包含默认 120（表示"就在里面或紧贴边界"）。

只输出 JSON，不要任何解释文字，格式：
{{"constraints": [
  {{"op": "exists", "filters": [{{"amenity": "university"}}], "label": "大学"}},
  {{"op": "inside", "filters": [{{"amenity": "fast_food"}}], "label": "肯德基"}},
  {{"op": "along", "feature_name": "长江", "tolerance": 2000, "label": "长江"}}
],
 "unparsed": ["听不懂的片段"]}}
"""


def api_key() -> str | None:
    """LLM 的 key 统一从 settings 取（环境变量 / .env）。"""
    return settings.LLM_API_KEY or None


def _alias_sample(limit: int = 60) -> str:
    items = [f"{w}->{json.dumps(f, ensure_ascii=False)}"
             for w, f in list(ALIASES.items())[:limit]]
    return "\n".join(items)


def call_llm(text: str, timeout: int = 0) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("未配置 DIZHI_LLM_API_KEY（见 .env.example）")
    base = settings.LLM_BASE_URL.rstrip("/")
    model = settings.LLM_MODEL
    timeout = timeout or settings.LLM_TIMEOUT
    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": SYSTEM_PROMPT.format(allowed=", ".join(ALLOWED_KEYS),
                                             alias_sample=_alias_sample())},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return json.loads(payload["choices"][0]["message"]["content"])


# ---------------------------------------------------------------- 校验
def _clean_filters(raw) -> list[dict[str, str]] | None:
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = {str(k): str(v) for k, v in item.items()
             if str(k) in ALLOWED_KEYS_SET and v not in (None, "")}
        if f:
            out.append(f)
    return out or None


def _clamp_distance(v, default: float, warnings: list[str], label: str) -> float:
    try:
        d = float(v)
    except (TypeError, ValueError):
        return default
    if d < MIN_DISTANCE_M:
        warnings.append(f"{label} 的距离 {d} 太小，已按 {MIN_DISTANCE_M:g} 米处理")
        return MIN_DISTANCE_M
    if d > MAX_DISTANCE_M:
        warnings.append(f"{label} 的距离 {d} 米超出合理范围，已按 {MAX_DISTANCE_M / 1000:g} 公里处理")
        return MAX_DISTANCE_M
    return d


def _tighten(cs: list["Constraint"], text: str) -> list["Constraint"]:
    """按用户原话对**覆盖面过宽**的标签做定向收紧。

    模型即使被明确提醒，有时仍会把"游泳馆"写成单独的 leisure=sports_centre，
    而那个标签等于"任何体育馆/体育中心"——实测会让只有体育馆、没有泳池的
    学校也通过判定。这里只在原话确实提到"游泳"时才收紧，避免误伤
    （用户说"体育馆"时 sports_centre 本来就是对的）。
    """
    if "游泳" not in text:
        return cs
    out: list[Constraint] = []
    for c in cs:
        # 只动**确实在讲游泳**的那个条件，别去污染水体/肯德基/高校等其他条件
        has_swim = any(
            f.get("leisure") in ("swimming_pool", "sports_centre")
            or f.get("amenity") == "swimming_pool"
            for f in c.filters)
        if not has_swim:
            out.append(c)
            continue
        fs: list[dict[str, str]] = []
        for f in c.filters:
            if f == {"leisure": "sports_centre"}:
                # 单独的体育馆太宽：改成"带游泳标签的体育场馆"
                fs.append({"leisure": "sports_centre", "sport": "swimming"})
            else:
                fs.append(f)
        if not any(f.get("leisure") == "swimming_pool" for f in fs):
            fs.insert(0, {"leisure": "swimming_pool"})
        out.append(replace(c, filters=fs))
    return out


def to_constraints(data: dict) -> tuple[list[Constraint], list[str]]:
    warnings: list[str] = []
    out: list[Constraint] = []
    for raw in (data.get("constraints") or []):
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op", "")).strip()
        if op not in OPS:
            warnings.append(f"模型给出了不支持的算子「{op}」，已忽略")
            continue
        cid = f"c{len(out) + 1}"
        label = str(raw.get("label") or "").strip()
        if op == "along":
            name = str(raw.get("feature_name") or "").strip()
            if not name:
                warnings.append("沿线条件缺少要素名，已忽略")
                continue
            out.append(Constraint(op="along", feature_name=name,
                                  tolerance=_clamp_distance(raw.get("tolerance"), 2000,
                                                            warnings, f"「{name}」沿线"),
                                  label=label or name, cid=cid))
            continue
        filters = _clean_filters(raw.get("filters"))
        if not filters:
            warnings.append(f"条件「{label or op}」没给出可识别的 OSM 标签，已忽略")
            continue
        if op == "exists":
            out.append(Constraint(op="exists", filters=filters,
                                  label=label or "目标要素", cid=cid))
        else:
            default = {"not_exists": 200.0, "inside": 120.0}.get(op, 2000.0)
            out.append(Constraint(op=op, filters=filters,
                                  max_distance=_clamp_distance(raw.get("max_distance"),
                                                               default, warnings,
                                                               f"「{label or op}」"),
                                  label=label or "目标要素", cid=cid))
    for u in (data.get("unparsed") or []):
        warnings.append(f"模型表示没听懂：「{u}」")
    if not out:
        warnings.append("模型没有给出任何可用条件")
    return out, warnings


def parse_with_llm(text: str) -> tuple[list[Constraint], list[str], dict | None]:
    """返回 (约束列表, 警告, 模型原始输出)。失败时抛异常，由上层降级。"""
    data = call_llm(text)
    cs, warns = to_constraints(data)
    cs = _tighten(cs, text)
    return cs, warns, data


if __name__ == "__main__":
    import sys
    t = " ".join(sys.argv[1:]) or "长江边上2公里内，附近3公里有化工厂，附近5公里有铁塔"
    d = call_llm(t)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    cs, ws = to_constraints(d)
    for c in cs:
        print("  ->", c.op, c.describe())
    for w in ws:
        print("  !!", w)
