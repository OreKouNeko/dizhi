"""不依赖 pip 的 wheel 下载与安装器（P0 依赖安装用）。

为什么需要它：
    本机沙箱会给 pip 自建的临时目录加上限制性文件权限，导致 pip 在
    清理/移动 `<wheel>.whl.metadata` 时抛 PermissionError，`pip install`
    和 `pip download` 都无法完成。这里直接走 PyPI 的 JSON 接口下载 wheel，
    再用 zipfile 解包到项目内的 libs/，全程不经过 pip 的临时目录。

用法:
    python tools/fetch_deps.py install      # 下载并解包全部依赖
    python tools/fetch_deps.py resolve      # 只解析依赖树，不下载
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBS = os.path.join(ROOT, "libs")
WHEELHOUSE = os.path.join(ROOT, "wheelhouse")
REQ_FILE = os.path.join(ROOT, "requirements.txt")

TOP_LEVEL = [
    "osmium", "duckdb", "shapely", "pyproj", "h3", "pyroaring",
    "typer", "rich", "requests", "folium", "numpy",
]

UA = {"User-Agent": "dizhi-deps/0.1"}
PYTAG = f"cp{sys.version_info.major}{sys.version_info.minor}"

# ---------------------------------------------------------------- marker 求值
try:
    from packaging.markers import Marker  # type: ignore

    def marker_ok(expr: str) -> bool:
        try:
            return bool(Marker(expr).evaluate())
        except Exception:  # noqa: BLE001
            return False
except Exception:  # noqa: BLE001

    def marker_ok(expr: str) -> bool:
        # 极简回退：只处理本项目会遇到的少数表达式
        e = expr.replace(" ", "")
        m = re.match(r"python_version([<>=!]+)(\d+\.\d+)", e)
        if m:
            op, ver = m.group(1), m.group(2)
            cur = f"{sys.version_info.major}.{sys.version_info.minor}"
            return {"<": cur < ver, ">": cur > ver, "<=": cur <= ver,
                    ">=": cur >= ver, "==": cur == ver, "!=": cur != ver}.get(op, True)
        if 'sys_platform=="win32"' in e or "sys_platform=='win32'" in e:
            return sys.platform == "win32"
        if 'platform_system=="windows"' in e.lower():
            return os.name == "nt"
        return True


# ---------------------------------------------------------------- PyPI 访问
def pypi(name: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/json"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def score(filename: str) -> int:
    """给 wheel 文件名打分，挑最适合本机（Windows / cp312）的那个。"""
    fn = filename.lower()
    if not fn.endswith(".whl"):
        return -1
    s = 0
    if f"{PYTAG}-{PYTAG}-win_amd64" in fn:
        s = 100
    elif f"{PYTAG}-abi3-win_amd64" in fn:
        s = 90
    elif "py3-none-any" in fn:
        s = 70
    elif "py2.py3-none-any" in fn:
        s = 68
    elif "win_amd64" in fn and PYTAG in fn:
        s = 80
    elif "win_amd64" in fn:
        s = 40
    elif "any.whl" in fn:
        s = 30
    return s


def pick(data: dict) -> dict | None:
    files = [f for f in data["urls"] if f["filename"].endswith(".whl")]
    if not files:
        return None
    scored = [(score(f["filename"]), f) for f in files]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return None
    return max(scored, key=lambda x: x[0])[1]


def download(url: str, dest: str) -> None:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)


# ---------------------------------------------------------------- 依赖解析
def requires_of(wheel_path: str) -> list[str]:
    with zipfile.ZipFile(wheel_path) as zf:
        meta = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not meta:
            return []
        text = zf.read(meta[0]).decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        spec = line.split(":", 1)[1].strip()
        name_part, _, marker = spec.partition(";")
        marker = marker.strip()
        if "extra ==" in marker or "extra==" in marker:
            continue  # 可选依赖，跳过
        if marker and not marker_ok(marker):
            continue
        name = re.split(r"[<>=!~\[; ]", name_part.strip())[0].strip()
        if name:
            out.append(name)
    return out


def install(names: list[str]) -> int:
    os.makedirs(LIBS, exist_ok=True)
    seen: dict[str, str] = {}
    queue = list(names)
    while queue:
        name = queue.pop(0).lower()
        if name in seen:
            continue
        try:
            data = pypi(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
            return 1
        f = pick(data)
        if not f:
            print(f"  [FAIL] {name}: no suitable wheel (source-only package?)")
            return 1
        dest = os.path.join(WHEELHOUSE, f["filename"])
        try:
            download(f["url"], dest)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] download {name}: {type(exc).__name__}: {exc}")
            return 1
        seen[name] = f["filename"]
        print(f"  [get ] {f['filename']}")
        for dep in requires_of(dest):
            if dep.lower() not in seen:
                queue.append(dep)

    print(f"\n[unpack] {len(seen)} wheels -> {LIBS}")
    for name, fn in seen.items():
        path = os.path.join(WHEELHOUSE, fn)
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                target = member
                if ".data/" in member:
                    head, _, tail = member.partition(".data/")
                    sub, _, rest = tail.partition("/")
                    if sub not in ("purelib", "platlib") or not rest:
                        continue  # 跳过 scripts / headers
                    target = rest
                out = os.path.join(LIBS, target.replace("/", os.sep))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with zf.open(member) as src, open(out, "wb") as dst:
                    dst.write(src.read())
    print(f"[done ] {len(seen)} packages installed")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "install"
    names = TOP_LEVEL
    if os.path.exists(REQ_FILE):
        parsed = []
        for line in open(REQ_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed.append(re.split(r"[<>=!~\[; ]", line)[0].strip())
        names = [n for n in parsed if n] or TOP_LEVEL
    print(f"[mode] {mode}")
    print(f"[deps] {', '.join(names)}")
    if mode == "resolve":
        for n in names:
            d = pypi(n)
            f = pick(d)
            print(f"  {n} {d['info']['version']} -> {f['filename'] if f else 'NO WHEEL'}")
        return 0
    return install(names)


if __name__ == "__main__":
    sys.exit(main())
