"""dizhi 命令行入口。

用法示例：
    python -m dizhi info
    python -m dizhi parse "长江边上2公里内，附近3公里有化工厂，附近5公里有铁塔"
    python -m dizhi query "长江边上2公里内，附近3公里有化工厂，附近5公里有铁塔" --top 10
"""
from __future__ import annotations

import json
import os
import sys
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dizhi import __version__, settings
from dizhi.config import QUESTION_LIBRARY
from dizhi.nlu.rule import parse as parse_text
from dizhi.ops.operators import Constraint, Scored, dedupe, search
from dizhi.ops.engine import SqlEngine
from dizhi.ops.memory import Store

# 所有路径都从 settings 读（环境变量 / .env），代码里不留硬编码。
DEFAULT_DATA = settings.DATA
DEFAULT_NATION = settings.NATION
DEFAULT_REPORT = settings.DEFAULT_REPORT
DEFAULT_TEMP = settings.ensure_temp_dir()

app = typer.Typer(add_completion=False, help="地图地点约束检索 CLI")


# ---------------------------------------------------------------- 语言理解
def understand(text: str, mode: str) -> tuple[list[Constraint], list[str], str]:
    """把中文变成约束。mode: auto | rule | llm。返回 (约束, 警告, 用的是谁)。"""
    if mode in ("auto", "llm"):
        try:
            from dizhi.nlu.llm import parse_with_llm
            cs, warns, _ = parse_with_llm(text)
            if cs:
                return cs, warns, "模型"
            warns = list(warns) + ["模型没给出可用条件，已改用规则解析"]
        except Exception as exc:  # noqa: BLE001
            if mode == "llm":
                raise
            print(f"[nlu] 模型不可用（{type(exc).__name__}: {str(exc)[:70]}），改用规则解析")
    cs, warns = parse_text(text)
    return cs, warns, "规则"


# ---------------------------------------------------------------- 小工具
def spread_points(points, res: int = 8) -> tuple[int, float]:
    """候选地点散布的真实占地：H3 网格覆盖，返回 (网格数, 约 km²)。

    为什么不用外包框：长江沿线的候选点是一条细长的带，外包框会把几万平方公里的
    空地也算进来，看起来"没缩小"，这是假象。按覆盖的网格算才反映实际搜索范围。
    """
    pts = [(float(a), float(b)) for a, b in points]
    if not pts:
        return 0, 0.0
    try:
        import h3
        cells = {h3.latlng_to_cell(la, lo, res) for la, lo in pts}
        cell_km2 = float(h3.average_hexagon_area(res, unit="km^2"))
    except Exception:  # noqa: BLE001
        cells = {(round(la, 2), round(lo, 2)) for la, lo in pts}
        cell_km2 = 123.0
    return len(cells), len(cells) * cell_km2


def spread_of(store: Store, idxs: list[int], res: int = 8) -> tuple[int, float]:
    return spread_points([(store.elements[i].clat, store.elements[i].clon) for i in idxs], res)


def km_area_of(store: Store, idxs: list[int]) -> float:
    return spread_of(store, idxs)[1]


def geo_distance_m(a, b) -> float:
    """两个经纬度点之间的近似米距（几百米尺度足够准）。"""
    import math
    dlat = (a.clat - b.clat) * 110574.0
    dlon = (a.clon - b.clon) * 111320.0 * math.cos(math.radians((a.clat + b.clat) / 2))
    return math.hypot(dlat, dlon)


def dedupe_mixed(scored, radius_m: float = 800.0):
    """把同一地点被拆成的多个要素合并成一个。

    OSM 里同一个地方常有重复登记（一个车站在 OSM 里既是 station 又是 stop，
    一个厂区被画成多个多边形）。SQL 引擎返回的行没有投影几何，所以用经纬度近似距离。
    """
    kept = []
    for s in scored:
        e = s.element
        dup = False
        for k in kept:
            f = k.element
            if e.name and f.name:
                if e.name != f.name:
                    continue
            elif not e.name and not f.name:
                if e.kind != f.kind:
                    continue
            else:
                continue
            if geo_distance_m(e, f) <= radius_m:
                dup = True
                break
        if not dup:
            kept.append(s)
    return kept


def scale_constraints(cs: list[Constraint], factor: float) -> list[Constraint]:
    import copy
    out = []
    for c in cs:
        d = copy.copy(c)
        if d.max_distance:
            d.max_distance *= factor
        if d.tolerance:
            d.tolerance *= factor
        out.append(d)
    return out


def fmt_distance(d: float) -> str:
    if d == float("inf"):
        return ">60km"
    return f"{d / 1000:.2f}km" if d >= 1000 else f"{d:.0f}m"


# ---------------------------------------------------------------- info
@app.command()
def info(data: str = typer.Option(DEFAULT_DATA, "--data", help="elements.parquet 路径")):
    """查看已导入的数据概况。"""
    console = Console(record=True)
    store = Store(data)
    console.print(Panel.fit(store.summary(), title="数据概况", border_style="cyan"))
    console.print(f"[dim]parquet: {os.path.abspath(data)}[/dim]")


# ---------------------------------------------------------------- parse
@app.command()
def parse(text: str):
    """只做语言解析，不检索——用来确认"我理解到的是什么"。"""
    console = Console(record=True)
    cs, warns = parse_text(text)
    t = Table(title="解析结果", show_header=True, header_style="bold cyan")
    t.add_column("#", width=3)
    t.add_column("算子", width=11)
    t.add_column("含义")
    for i, c in enumerate(cs, 1):
        t.add_row(str(i), c.op, c.describe())
    console.print(t)
    for w in warns:
        console.print(f"[yellow]警告:[/yellow] {w}")
    if not cs:
        console.print("[red]没有解析出任何条件[/red]")


# ---------------------------------------------------------------- query
@app.command()
def query(
    text: str = typer.Argument(..., help="中文描述，用逗号分隔多个条件"),
    data: str = typer.Option(DEFAULT_DATA, "--data",
                             help="数据集：单个 parquet 文件，或全国数据集通配符"),
    top: int = typer.Option(10, "--top", help="返回前几名"),
    report: str = typer.Option(DEFAULT_REPORT, "--report", help="报告写到哪里（UTF-8）"),
    map_out: str = typer.Option("", "--map", help="可选：输出 HTML 地图路径"),
    loose_inside: bool = typer.Option(
        False, "--loose-inside",
        help="放宽\"内部有\"判定：校园只标成一个点时用 700 米半径近似（默认严格包含）"),
    relax: bool = typer.Option(True, "--relax/--no-relax", help="结果不足时自动放宽距离"),
    merge: bool = typer.Option(True, "--merge/--no-merge",
                               help="把同一地点被拆成的多个要素合并成一个候选"),
    nlu: str = typer.Option("auto", "--nlu", help="语言解析：auto|rule|llm"),
    engine: str = typer.Option("auto", "--engine", help="检索引擎：auto|sql|mem"),
    nation: bool = typer.Option(False, "--nation", help="改用全国数据集检索"),
    ask: bool = typer.Option(False, "--ask",
                             help="候选过多时主动向你提问，逐步缩小范围"),
    bbox: str = typer.Option("", "--bbox",
                             help="限定区域 min_lon,min_lat,max_lon,max_lat（大幅提速）"),
):
    """按中文描述检索地点。"""
    console = Console(record=True)
    t_start = time.time()
    # "内部有"默认走严格包含判定（目标必须真的落在校园多边形里面）
    import dizhi.ops.engine as _engine
    _engine.INSIDE_STRICT = not loose_inside

    bbox_val = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox_val = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            console.print("[red]--bbox 格式应为 min_lon,min_lat,max_lon,max_lat[/red]")
            raise typer.Exit(code=2) from None

    if nation:
        data = DEFAULT_NATION
    if not data:
        console.print("[red]没有指定数据集。[/red]")
        console.print("  小范围数据：[cyan]--data 你的数据.parquet[/cyan]"
                      "（或用 [cyan]DIZHI_DATA[/cyan] 环境变量）")
        console.print("  全国数据  ：[cyan]--nation[/cyan]"
                      "（用 [cyan]DIZHI_NATION[/cyan] 指向分区数据集通配符）")
        console.print("  还没有数据？先下载并建库："
                      "[cyan]python -m dizhi.ingest.pbf --help[/cyan]")
        raise typer.Exit(code=2)
    use_sql = engine == "sql" or (engine == "auto" and ("*" in data or os.path.isdir(data)))
    store = None
    if not use_sql:
        if not os.path.exists(data):
            console.print(f"[red]找不到数据文件 {data}[/red]")
            console.print("[yellow]小范围数据先跑: python -m dizhi.ingest.overpass[/yellow]")
            raise typer.Exit(code=2)
        store = Store(data)

    # ---- 第 1 步：听懂
    cs, warns, who = understand(text, nlu)
    console.print(Panel(f"[bold]{text}[/bold]", title="你说的话", border_style="blue"))
    console.print(f"[bold cyan]【第 1 步】我理解到的条件[/bold cyan] [dim]（{who}解析）[/dim]")
    if not cs:
        for w in warns:
            console.print(f"  [yellow]{w}[/yellow]")
        console.print("[red]没有解析出有效条件，已停止。[/red]")
        raise typer.Exit(code=1)
    for i, c in enumerate(cs, 1):
        console.print(f"  {i}. {c.describe()}   [dim][{'硬条件' if c.hard else '软条件'}][/dim]")
    for w in warns:
        console.print(f"  [yellow]警告: {w}[/yellow]")

    # ---- 第 2 步：逐层缩小（结果不足时自动放宽）
    console.print()
    console.print(f"[bold cyan]【第 2 步】逐层缩小范围[/bold cyan] "
                  f"[dim]（{'全国 SQL 引擎' if use_sql else '内存引擎'}）[/dim]")
    factors = (1.0, 2.5, 5.0) if relax else (1.0,)
    res = None
    used_factor = 1.0
    engine_obj = None
    for f in factors:
        scaled = scale_constraints(cs, f)
        if use_sql:
            if engine_obj is None:
                engine_obj = SqlEngine(data, DEFAULT_TEMP, threads=4)
            res = engine_obj.search(scaled, bbox=bbox_val)
        else:
            res = search(store, scaled)
        used_factor = f
        if len(res.ranked) >= top:
            break

    # ---- 主动提问：候选仍然太多时，挑"信息增益最大"的条件问用户
    asked = 0
    while (ask and engine_obj is not None and getattr(res, "table", None)
           and asked < 4 and len(res.ranked) > max(top * 10, 150)):
        try:
            qs = engine_obj.suggest_questions(res.table, len(res.ranked),
                                              QUESTION_LIBRARY)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [dim]提问评估失败，跳过（{exc}）[/dim]")
            break
        if not qs or qs[0]["gain"] <= 0.05:
            break
        q = qs[0]
        console.print()
        console.print(f"[bold cyan]【主动提问 {asked + 1}】[/bold cyan]"
                      f"候选还有 [bold]{len(res.ranked)}[/bold] 个，"
                      f"问一个能砍掉最多的：这个地方**内部或旁边**有【{q['label']}】吗？")
        console.print(f"  [dim]（答 y 有 / n 没有 / 直接回车跳过；"
                      f"答完剩下的不会超过 {q['expected_left']} 个）[/dim]")
        try:
            ans = (console.input("  > ") or "").strip().lower()
        except Exception:  # noqa: BLE001
            break
        if not ans:
            break
        op = "inside" if ans[0] == "y" else "not_exists"
        cs = list(cs) + [Constraint(op=op, filters=q["filters"], max_distance=120.0,
                                    label=q["label"], cid=f"q{asked + 1}")]
        asked += 1
        for f in factors:
            res = engine_obj.search(scale_constraints(cs, f), bbox=bbox_val)
            if len(res.ranked) >= top:
                break
        console.print(f"  -> 缩小到 [bold]{len(res.ranked)}[/bold] 个")

    t = Table(show_header=True, header_style="bold")
    t.add_column("条件")
    t.add_column("目标数", justify="right")
    t.add_column("匹配写法", style="dim", max_width=18)
    t.add_column("前", justify="right")
    t.add_column("后", justify="right")
    t.add_column("网格", justify="right")
    t.add_column("km²", justify="right")
    if use_sql:
        t.add_row("[dim]起始候选池[/dim]", "-", "-", "-", f"{res.pool_size}", "-", "-")
    else:
        c0, a0 = spread_of(store, list(range(len(store.elements))))
        t.add_row("[dim]起始候选池[/dim]", "-", "-", "-", f"{res.pool_size}", f"{c0}", f"{a0:.0f}")
    for st in res.steps:
        if st.after_idxs and store is not None:
            cn, ar = spread_of(store, st.after_idxs)
            c_s, a_s = str(cn), f"{ar:.1f}"
        else:
            c_s, a_s = "-", "-"
        t.add_row(st.constraint.describe(), str(st.target_count), st.note or "-",
                  str(st.before), str(st.after), c_s, a_s)
    console.print(t)
    cells, area = spread_points([(s.element.clat, s.element.clon) for s in res.ranked])
    console.print(f"  最终候选 [bold]{len(res.ranked)}[/bold] 个，分布在 "
                  f"[bold]{cells}[/bold] 个网格里，约 [bold]{area:.1f}[/bold] km²")
    if getattr(res, "preselect", None):
        tot, kept = res.preselect
        console.print(f"  [dim]说明：满足条件的候选共有 {tot} 个。为了速度，先用 SQL 里"
                      f"顺带算出的近似距离预排，只对最靠前的 {kept} 个做了精确几何校验；"
                      f"上表是这批里通过精确校验的数量。[/dim]")
    if used_factor > 1.0:
        console.print(f"  [yellow]注意：严格条件下结果不足 {top} 个，"
                      f"已自动把距离放宽到 {used_factor:g} 倍。[/yellow]")

    # ---- 第 3 步：排序输出
    console.print()
    console.print(f"[bold cyan]【第 3 步】置信度排序，取前 {top}[/bold cyan]")
    ranked = dedupe_mixed(res.ranked) if merge else res.ranked
    if merge and len(ranked) != len(res.ranked):
        console.print(f"  [dim]（{len(res.ranked)} 个要素合并为 {len(ranked)} 个独立地点）[/dim]")
    if not ranked:
        console.print("[red]没有任何要素满足全部硬条件。[/red]")
        console.print("OSM 数据可能确实没有，或条件太严。可用 --relax 放宽、或减少条件。")
    else:
        out = Table(show_header=True, header_style="bold")
        out.add_column("#", width=3)
        out.add_column("置信度", justify="right", width=7)
        out.add_column("名称", width=26)
        out.add_column("类别", width=22)
        out.add_column("坐标", width=21)
        out.add_column("OSM", width=16)
        for i, s in enumerate(ranked[:top], 1):
            e = s.element
            name = e.name or "(无名)"
            out.add_row(str(i), f"{s.score * 100:.0f}%", name, e.kind,
                        f"{e.clat:.5f},{e.clon:.5f}", e.ref)
        console.print(out)

        console.print()
        console.print("[bold]命中详情（前 5 名）[/bold]")
        for i, s in enumerate(ranked[:5], 1):
            console.print(f"  [bold]#{i} {s.element.name or '(无名)'}[/bold] "
                          f"置信度 {s.score * 100:.0f}%")
            for desc, sc, d in s.parts:
                mark = "[green]✓[/green]" if sc > 0 else "[red]✗[/red]"
                dist = f"（{fmt_distance(d)}）" if d not in (0.0, float("inf")) else ""
                console.print(f"      {mark} {desc}{dist}  贴合度 {sc:.2f}")

    # ---- 输出文件
    console.print()
    os.makedirs(os.path.dirname(os.path.abspath(report)), exist_ok=True)
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(console.export_text(clear=False))
    console.print(f"[dim]报告已写入 {os.path.abspath(report)}[/dim]")

    if ranked:
        if map_out:
            write_map(engine_obj, ranked, map_out, top, scaled)
            console.print(f"[dim]地图已写入 {os.path.abspath(map_out)}[/dim]")
        geo = os.path.splitext(report)[0] + ".geojson"
        write_geojson(ranked, geo, top)
        console.print(f"[dim]GeoJSON 已写入 {os.path.abspath(geo)}[/dim]")

    console.print(f"[dim]耗时 {time.time() - t_start:.1f}s[/dim]")


# ---------------------------------------------------------------- 输出
def write_geojson(ranked: list[Scored], path: str, top: int) -> None:
    feats = []
    for s in ranked[:top]:
        e = s.element
        feats.append({
            "type": "Feature",
            "geometry": json.loads(json.dumps(e.geom_ll.__geo_interface__)),
            "properties": {
                "rank": len(feats) + 1, "confidence": round(s.score, 4),
                "name": e.name, "kind": e.kind, "osm": e.ref,
                "latitude": e.clat, "longitude": e.clon,
            },
        })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh,
                  ensure_ascii=False, indent=1)


def _wkb_geojson(wkb_bytes):
    """把候选的 WKB 转成 GeoJSON 几何；失败就返回 None。

    multipolygon 关系的成员里常混着线要素，几何类型会变成 GeometryCollection，
    而 **folium 对 GeometryCollection 不渲染悬浮框和弹窗**（会刷一屏警告，
    而且那几十个候选点了没反应）。这里把其中的面提取出来合成 MultiPolygon。
    """
    if not wkb_bytes:
        return None
    try:
        from shapely import wkb as _wkb
        from shapely.geometry import MultiPolygon
        g = _wkb.loads(bytes(wkb_bytes))
        if g.is_empty:
            return None
        if g.geom_type == "GeometryCollection":
            polys = []
            for x in g.geoms:
                if x.geom_type == "Polygon":
                    polys.append(x)
                elif x.geom_type == "MultiPolygon":
                    polys.extend(x.geoms)
            if not polys:
                return None
            g = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        return g.__geo_interface__
    except Exception:  # noqa: BLE001
        return None


def write_map(engine, ranked: list[Scored], path: str, top: int,
              constraints: list | None = None) -> None:
    """把**全部候选**画到本地地图上，供人工尾筛。

    跟只画前十的旧版不同，这里做三件事：
      1. 画全部候选（不只是 top），面状校园画**多边形轮廓**——校园形状本身
         就是很强的辨识特征，拿它和记忆中的地图/截图比对最直接；
      2. 把命中的**水体/肯德基/泳池**等目标要素也标出来，让人一眼看出
         "判定是靠哪个具体地物成立的"，而不是只给一个置信度数字；
      3. 左侧列出全部候选清单，点条目可定位到地图上对应位置。
    """
    import folium
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="folium")
    if not ranked:
        return
    c = (sum(s.element.clon for s in ranked) / len(ranked),
         sum(s.element.clat for s in ranked) / len(ranked))
    # 底图优先级：
    #   1) 天地图影像 —— CGCS2000 与 WGS-84 基本重合（**没有 GCJ-02 偏移**），
    #      国内直连速度快。需要设置 TIANDITU_TK。
    #      注意服务器端 key 强制要求 Referer，浏览器直接开本地 HTML 会是白图，
    #      用 tools/serve_map.py 起本地代理即可（详见 providers/tianditu.py）。
    #   2) 没有天地图 key 时退回 Esri 影像（无偏移但国内慢）+ 高德（有偏移但必通）。
    tk = settings.TIANDITU_TK
    if tk:
        _TDT = ("https://t{{s}}.tianditu.gov.cn/{layer}_w/wmts?SERVICE=WMTS"
                "&REQUEST=GetTile&VERSION=1.0.0&LAYER={lyr}&STYLE=default"
                "&TILEMATRIXSET=w&FORMAT=tiles&TILECOL={{x}}&TILEROW={{y}}"
                "&TILEMATRIX={{z}}&tk=" + tk)
        m = folium.Map(location=[c[1], c[0]], zoom_start=6,
                       tiles=_TDT.format(layer="img", lyr="img"),
                       attr="天地图影像", subdomains="01234567",
                       name="① 天地图影像(无偏移·国内快)")
        for layer, label in (("cia", "② 天地图影像注记"),
                             ("vec", "③ 天地图矢量"),
                             ("cva", "④ 天地图矢量注记")):
            folium.TileLayer(tiles=_TDT.format(layer=layer, lyr=layer),
                             attr="天地图", subdomains="01234567",
                             name=label, show=(layer == "cia")).add_to(m)
    else:
        m = folium.Map(location=[c[1], c[0]], zoom_start=6,
                       tiles="https://server.arcgisonline.com/ArcGIS/rest/"
                             "services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                       attr="Esri World Imagery",
                       name="① Esri 卫星影像(无偏移·国内可能慢)")
    folium.TileLayer(
        tiles="https://webst0{s}.is.autonavi.com/appmaptile"
              "?style=6&x={x}&y={y}&z={z}",
        attr="高德卫星(GCJ-02，有偏移)", subdomains="1234",
        name="⑤ 高德卫星影像(有偏移)", show=False).add_to(m)

    # ---- 候选要素 ----
    feats = []
    for i, s in enumerate(ranked, 1):
        e = s.element
        geo = _wkb_geojson(e.wkb) or {"type": "Point",
                                      "coordinates": [e.clon, e.clat]}
        detail = " / ".join(
            f"{desc} {sc * 100:.0f}%" + (f"({d:.0f}m)" if d != float("inf") else "")
            for desc, sc, d in s.parts)
        feats.append({
            "type": "Feature",
            "geometry": geo,
            "properties": {
                "rank": i,
                "name": e.name or "(无名)",
                "kind": e.kind or "",
                "score": round(s.score * 100),
                "ref": f"{e.osm_type}/{e.osm_id}",
                "lon": round(e.clon, 6),
                "lat": round(e.clat, 6),
                "detail": detail,
            },
        })

    def _style(f):
        sc = f["properties"]["score"]
        col = "#c0392b" if sc >= 90 else ("#e67e22" if sc >= 70 else "#7f8c8d")
        gt = f["geometry"]["type"]
        if gt == "Point":
            return {"color": col, "weight": 1}
        return {"color": col, "weight": 2, "fillColor": col, "fillOpacity": 0.12}

    gj = folium.GeoJson(
        {"type": "FeatureCollection", "features": feats},
        name=f"候选高校（{len(feats)} 个）",
        style_function=_style,
        marker=folium.CircleMarker(radius=5),
        tooltip=folium.GeoJsonTooltip(
            fields=["rank", "name", "score"], aliases=["#", "名称", "置信度"],
            localize=True, sticky=True),
        popup=folium.GeoJsonPopup(
            fields=["rank", "name", "kind", "lon", "lat", "score", "ref", "detail"],
            aliases=["序号", "名称", "类别", "经度", "纬度", "置信度", "OSM", "命中详情"],
            localize=True, max_width=420),
    ).add_to(m)

    # ---- 命中的目标要素（水体 / 肯德基 / 泳池 …）----
    tgt_layers = []
    if engine is not None and constraints:
        try:
            from dizhi.ops.engine import deg_guard
            con = engine.con
            ids = [(s.element.osm_type, s.element.osm_id) for s in ranked]
            con.execute("CREATE OR REPLACE TEMP TABLE _cand(osm_type VARCHAR, "
                        "osm_id BIGINT)")
            con.executemany("INSERT INTO _cand VALUES (?,?)", ids)
            palette = ["#2980b9", "#c0392b", "#16a085", "#8e44ad",
                       "#d35400", "#2c3e50"]
            ci = 0
            for con_c in constraints:
                if con_c.op not in ("near", "along", "inside"):
                    continue
                lim = float(con_c.max_distance or (2000.0 if con_c.op == "near"
                                                   else 120.0))
                if con_c.op == "inside":
                    lim = max(lim, 700.0)   # 点状校园放宽到 700 米
                g = deg_guard(lim * 1.2, 45.0)
                tw = engine.target_where(con_c, "t")
                rows = con.execute(f"""
                    SELECT u.osm_type || '/' || u.osm_id AS parent,
                           t.name, t.kind, t.clon, t.clat
                    FROM _cand c
                    JOIN el u ON u.osm_type = c.osm_type AND u.osm_id = c.osm_id
                    JOIN el t ON ({tw})
                      AND t.maxx >= u.minx - {g} AND t.minx <= u.maxx + {g}
                      AND t.maxy >= u.miny - {g} AND t.miny <= u.maxy + {g}
                """).fetchall()
                if not rows:
                    continue
                by_parent: dict[str, list] = {}
                for parent, nm, kd, lo, la in rows:
                    by_parent.setdefault(parent, []).append((nm, kd, lo, la))
                tfeats = []
                for parent, items in by_parent.items():
                    for nm, kd, lo, la in items:
                        tfeats.append({
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [lo, la]},
                            "properties": {"parent": parent,
                                           "name": nm or "(无名)", "kind": kd or ""},
                        })
                col = palette[ci % len(palette)]
                ci += 1
                tgt_layers.append(folium.GeoJson(
                    {"type": "FeatureCollection", "features": tfeats},
                    name=f"命中的{con_c.describe()}"
                         f"（{len(tfeats)} 个）",
                    style_function=lambda _f, _c=col: {
                        "color": _c, "weight": 1, "fillColor": _c,
                        "fillOpacity": 0.75},
                    marker=folium.CircleMarker(radius=4),
                    tooltip=folium.GeoJsonTooltip(
                        fields=["name", "kind"], aliases=["名称", "类别"],
                        localize=True, sticky=True),
                ).add_to(m))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 目标要素图层生成失败，仅输出候选：{exc}")

    folium.LayerControl(collapsed=False).add_to(m)

    # ---- 左侧候选清单，点条目定位 ----
    rows_html = "".join(
        f'<div class="row" data-rank="{f["properties"]["rank"]}">'
        f'<b>#{f["properties"]["rank"]}</b> {f["properties"]["name"]}'
        f'<span class="sc">{f["properties"]["score"]}%</span></div>'
        for f in feats)
    panel = folium.Element(f"""
    <div id="panel">
      <div class="hd">候选 {len(feats)} 个 · 点击定位</div>
      {rows_html}
    </div>
    <style>
      #panel {{ position:absolute; top:10px; left:10px; z-index:9999; width:270px;
                max-height:88vh; overflow:auto; background:rgba(255,255,255,.94);
                border:1px solid #bbb; border-radius:6px; font:12px/1.5 sans-serif;
                box-shadow:0 2px 8px rgba(0,0,0,.25); }}
      #panel .hd {{ position:sticky; top:0; background:#2c3e50; color:#fff;
                    padding:6px 8px; font-weight:700; }}
      #panel .row {{ padding:4px 8px; cursor:pointer; border-bottom:1px solid #eee; }}
      #panel .row:hover {{ background:#eaf2fb; }}
      #panel .sc {{ float:right; color:#c0392b; font-weight:700; }}
    </style>
    <script>
      window.addEventListener('load', function () {{
        // folium 把地图对象命名为 map_xxxx 挂在 window 上，这里找出来
        var map = null;
        for (var k in window) {{
          try {{
            var v = window[k];
            if (v && v._container && typeof v.eachLayer === 'function'
                && typeof v.fitBounds === 'function') {{ map = v; break; }}
          }} catch (e) {{}}
        }}
        if (!map) return;
        // 递归收集带 rank 属性的要素图层（GeoJson 是分组，要素在它下面）
        var byRank = {{}};
        function walk(layer) {{
          if (layer.feature && layer.feature.properties
              && layer.feature.properties.rank !== undefined) {{
            byRank[layer.feature.properties.rank] = layer;
          }}
          if (typeof layer.eachLayer === 'function') {{
            layer.eachLayer(walk);
          }}
        }}
        map.eachLayer(walk);
        document.querySelectorAll('#panel .row').forEach(function (el) {{
          el.onclick = function () {{
            var l = byRank[el.getAttribute('data-rank')];
            if (!l) return;
            var b = (typeof l.getBounds === 'function') ? l.getBounds() : null;
            if (b && b.isValid()) {{ map.fitBounds(b, {{maxZoom: 17}}); }}
            else if (typeof l.getLatLng === 'function') {{
              map.setView(l.getLatLng(), 17);
            }}
            if (typeof l.openPopup === 'function') {{ l.openPopup(); }}
          }};
        }});
      }});
    </script>
    """)
    m.get_root().html.add_child(panel)

    m.save(path)



@app.command()
def version():
    """显示版本。"""
    Console().print(f"dizhi {__version__}  python {sys.version.split()[0]}")


if __name__ == "__main__":
    app()
