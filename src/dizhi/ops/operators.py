"""四类约束算子 + 检索主流程（选择性排序、逐层缩小、打分）。

算子语义：
    exists      候选要素自身的标签满足条件（"我要找一个寺庙"）
    near        候选要素附近 max_distance 米内存在目标要素（"附近有化工厂"）
    not_exists  候选要素附近 max_distance 米内不存在目标要素（"不要挨着居民区"）
    along       候选要素位于某个命名线状要素（长江等）的 tolerance 米缓冲区内

主流程 = 硬约束逐层求交（按稀有度排序，越稀有的条件越先算）+ 软约束打分排序。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from dizhi.ops.memory import Element, Store


@dataclass
class Constraint:
    op: str                                   # exists | near | not_exists | along
    label: str = ""                           # 中文描述（展示给用户）
    filters: list[dict[str, str]] | None = None
    max_distance: float | None = None
    feature_name: str | None = None
    tolerance: float | None = None
    hard: bool = True
    weight: float = 1.0
    cid: str = ""

    def describe(self) -> str:
        d = self.max_distance or self.tolerance
        dist = f"{d / 1000:g} 公里" if d and d >= 1000 else (f"{d:g} 米" if d else "")
        if self.op == "along":
            return f"位于「{self.feature_name}」沿线 {dist}内"
        if self.op == "near":
            return f"{dist}内有{self.label or '目标要素'}"
        if self.op == "inside":
            return f"内部有{self.label or '目标要素'}"
        if self.op == "not_exists":
            return f"{dist}内没有{self.label or '目标要素'}"
        if self.op == "exists":
            return f"本身就是{self.label or '目标要素'}"
        return self.label or self.op


@dataclass
class Step:
    constraint: Constraint
    before: int
    after: int
    target_count: int = 0
    after_idxs: list[int] = field(default_factory=list)
    note: str = ""


@dataclass
class Scored:
    element: Element
    score: float
    parts: list[tuple[str, float, float]] = field(default_factory=list)


# ---------------------------------------------------------------- 打分函数
def proximity(d: float, limit: float) -> float:
    """距离贴合度：0 米 -> 1.0，等于上限 -> 0.5，越远越接近 0，永不为 0。"""
    if limit <= 0:
        return 1.0
    if math.isinf(d):
        return 0.0
    x = d / limit
    return 1.0 / (1.0 + x * x)


def data_confidence(el: Element) -> float:
    """数据可信度：标签越全越可信；无名要素打折（对应方案 v0.2 第 3 节）。"""
    n = len(el.tags)
    conf = 0.6 + 0.4 * min(1.0, n / 8.0)
    if not el.named:
        conf *= 0.7
    return round(conf, 3)


# ---------------------------------------------------------------- 算子求值
class Evaluator:
    """把一个约束编译成可复用的求值器（空间索引只建一次）。"""

    def __init__(self, store: Store, c: Constraint, pool_layer=None):
        self.store = store
        self.c = c
        self.target_idxs: list[int] = []
        self.matched_filter: dict[str, str] | None = None
        self._tree = None
        self._union = None

        if c.op in ("near", "exists"):
            # 肯定式条件：优先用同义词表里最具体的写法（先试 industrial=chemical，
            # 再退回 man_made=works，最后才是泛泛的 landuse=industrial），
            # 否则"化工厂"会把所有工业用地都算进来，约束几乎不起筛选作用。
            self.target_idxs, self.matched_filter = self._select_specific()
        elif c.op == "not_exists":
            # 否定式条件反过来：要排除就按最宽的写法排除，不能只排最严的那种。
            self.target_idxs = store.select(c.filters)
        if c.op in ("near", "not_exists"):
            self._tree = store.tree(self.target_idxs) if self.target_idxs else None

        if c.op == "along":
            self.target_idxs = self._resolve_feature()

    def _select_specific(self) -> tuple[list[int], dict[str, str] | None]:
        filters = self.c.filters or []
        if not filters:
            return self.store.select(None), None
        for f in filters:
            idxs = self.store.select([f])
            if idxs:
                return idxs, f
        return [], filters[0]

    def note(self) -> str:
        if self.matched_filter:
            return "、".join(f"{k}={v}" for k, v in self.matched_filter.items())
        return ""

    def _resolve_feature(self) -> list[int]:
        """找到"长江"这类命名线状要素的真实几何。

        要点：同名要素可能有很多（一座名为"长江"的公交站、一块水面……），
        做"沿线"约束必须优先用真正的线要素（河道的 relation / way），
        只有当没有任何线要素时才退回其他类型，否则缓冲区会锚在错误的位置上。
        """
        name = self.c.feature_name or ""
        if not name:
            return []
        exact = [e.idx for e in self.store.elements if (e.name or "") == name]
        if not exact:
            exact = [e.idx for e in self.store.elements if name in (e.name or "")]
        lines = [i for i in exact if self.store.elements[i].layer == "line"]
        idxs = lines or exact
        self._union = self.store.union_geometry(idxs)
        return idxs

    # -------------------------------------------------- 单独求值（用于选择性估计）
    def surviving_count(self, idxs: list[int]) -> int:
        return len(self.evaluate(idxs, want_scores=False)[0])

    def evaluate(self, idxs: list[int], want_scores: bool = True
                 ) -> tuple[list[int], dict[int, float]]:
        c = self.c
        dists: dict[int, float] = {}

        if c.op == "exists":
            kept = [i for i in idxs if self.store.match(self.store.elements[i].tags, c.filters)]
            return kept, dists

        if c.op == "along":
            if self._union is None:
                return [], dists
            tol = float(c.tolerance or 1000)
            kept = []
            for i in idxs:
                g = self.store.elements[i].geom_m
                if want_scores:
                    d = g.distance(self._union)
                    if d <= tol:
                        dists[i] = d
                        kept.append(i)
                elif g.distance(self._union) <= tol:
                    kept.append(i)
            return kept, dists

        if not self.target_idxs or self._tree is None:
            # 目标要素一个都没有
            return ([] if c.op == "near" else list(idxs)), dists

        limit = float(c.max_distance or 0)
        tree = self._tree
        geoms = [self.store.elements[i].geom_m for i in self.target_idxs]
        kept = []
        for i in idxs:
            g = self.store.elements[i].geom_m
            hits = tree.query(g, predicate="dwithin", distance=limit)
            inside = len(hits) > 0
            if c.op == "near" and inside:
                if want_scores:
                    dists[i] = min(g.distance(geoms[int(h)]) for h in hits)
                kept.append(i)
            elif c.op == "not_exists" and not inside:
                if want_scores:
                    dists[i] = self._nearest_beyond(g, tree, geoms, limit)
                kept.append(i)
        return kept, dists

    @staticmethod
    def _nearest_beyond(g, tree, geoms, limit: float) -> float:
        r = limit * 4 + 1000
        hits = tree.query(g, predicate="dwithin", distance=r)
        if len(hits) == 0:
            return r
        return min(g.distance(geoms[int(h)]) for h in hits)


# ---------------------------------------------------------------- 主流程
class SearchResult:
    def __init__(self):
        self.steps: list[Step] = []
        self.ranked: list[Scored] = []
        self.pool_size: int = 0
        # SQL 引擎会把当前候选集所在的临时表名放进来，供"主动提问"继续筛选用
        self.table: str | None = None
        # 精确判定前的预排序：(候选总数, 实际精确校验数)。用于向用户说明
        # "最终候选数"是精确校验过的数量，不是全部满足条件的数量。
        self.preselect: tuple[int, int] | None = None


def search(store: Store, constraints: list[Constraint],
           pool: list[int] | None = None,
           order_by_selectivity: bool = True) -> SearchResult:
    res = SearchResult()
    if pool is None:
        pool = default_pool(store)
    res.pool_size = len(pool)

    hard = [c for c in constraints if c.hard]
    evals = {id(c): Evaluator(store, c) for c in constraints}

    if order_by_selectivity and len(hard) > 1:
        hard = sorted(hard, key=lambda c: evals[id(c)].surviving_count(pool))

    current = list(pool)
    for c in hard:
        before = len(current)
        kept, _ = evals[id(c)].evaluate(current, want_scores=False)
        ev = evals[id(c)]
        res.steps.append(Step(constraint=c, before=before, after=len(kept),
                              target_count=len(ev.target_idxs),
                              after_idxs=list(kept), note=ev.note()))
        current = kept
        if not current:
            break

    # 打分：所有约束（含软约束）一起算
    scored: list[Scored] = []
    for i in current:
        total, weight_sum = 0.0, 0.0
        parts: list[tuple[str, float, float]] = []
        for c in constraints:
            ev = evals[id(c)]
            kept, dists = ev.evaluate([i], want_scores=True)
            passed = i in kept
            if not passed:
                s, d = 0.0, float("inf")
            elif c.op == "near":
                d = dists[i]
                s = proximity(d, float(c.max_distance or 1))
            elif c.op == "along":
                d = dists[i]
                s = proximity(d, float(c.tolerance or 1))
            elif c.op == "not_exists":
                d = dists.get(i, 0.0)
                lim = float(c.max_distance or 1)
                s = min(1.0, d / (2 * lim))
            else:
                d, s = 0.0, 1.0
            w = c.weight
            total += w * s
            weight_sum += w
            parts.append((c.describe(), s, d))

        base = total / weight_sum if weight_sum else 0.0
        conf = base * data_confidence(store.elements[i])
        scored.append(Scored(element=store.elements[i], score=conf, parts=parts))

    scored.sort(key=lambda s: s.score, reverse=True)
    res.ranked = scored
    return res


def default_pool(store: Store, layers=("poi", "area")) -> list[int]:
    """候选池：默认"有名字的"点/面要素（对应方案 v0.2 第 2 节）。"""
    return [e.idx for e in store.elements if e.named and e.layer in layers]


def dedupe(store: Store, scored: list["Scored"], radius_m: float = 800.0) -> list["Scored"]:
    """把指向同一个真实地点、且彼此很近的要素合并成一个候选。

    OSM 里同一个地方常被拆成多个要素（同一条路切成很多段、一个厂区分成几个
    多边形、一栋楼有多个节点）。不去重的话 Top10 会被同一地点的碎片占满，
    看起来像"只找到一个地方"。
    """
    kept: list[Scored] = []
    for s in scored:
        e = s.element
        dup = False
        for k in kept:
            f = k.element
            if e.named and f.named:
                # 同名且挨得近 -> 同一个地方（比如同一个车站在 OSM 里既是
                # railway=station 又是 railway=stop，属于重复登记）
                if e.name != f.name:
                    continue
            elif not e.named and not f.named:
                if e.kind != f.kind:
                    continue
            else:
                continue
            if e.geom_m.distance(f.geom_m) <= radius_m:
                dup = True
                break
        if not dup:
            kept.append(s)
    return kept
