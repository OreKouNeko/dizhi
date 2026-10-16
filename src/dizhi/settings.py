"""集中配置：所有路径、密钥、第三方服务地址都从这里取。

原则：**代码里不留任何硬编码的路径和密钥**。
- 优先读环境变量；
- 环境变量没有时，尝试读项目根目录的 `.env`（模板见 `.env.example`）；
- 都没有就用一个安全的中性默认值（通常是空，由调用方报错提示）。

这样别人 clone 下来不会因为作者机器上的路径而失败，也不会误带作者的密钥。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器（不引入 python-dotenv 依赖）。已存在的环境变量优先。"""
    p = path or (PROJECT_ROOT / ".env")
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except OSError:
        pass


load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- 数据集
# 单个 parquet 文件（小范围试跑用），或全国分区数据集的通配符。
DATA = _env("DIZHI_DATA")
NATION = _env("DIZHI_NATION")

# DuckDB 溢写目录。默认放系统临时目录，**不要放沙箱外或只读盘**，
# 否则查询一大就报 "IO Error: Cannot open file"。
TEMP_DIR = _env("DIZHI_TMP") or str(Path(tempfile.gettempdir()) / "dizhi")
DEFAULT_REPORT = _env("DIZHI_REPORT", "report.txt")
DEFAULT_MAP = _env("DIZHI_MAP")

# ---------------------------------------------------------------- OSM 数据
# 本地 PBF 文件，以及 Geofabrik 下载地址前缀（用于 fetch 子命令）。
OSM_PBF = _env("DIZHI_PBF")
GEOFABRIK_BASE = _env("DIZHI_GEOFABRIK", "https://download.geofabrik.de")

# ---------------------------------------------------------------- LLM 解析
LLM_API_KEY = _env("DIZHI_LLM_API_KEY")
LLM_BASE_URL = _env("DIZHI_LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = _env("DIZHI_LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = _env_int("DIZHI_LLM_TIMEOUT", 60)

# ---------------------------------------------------------------- 第三方地图 API
# 高德 Web 服务（关键字搜索等）。个人 key 免费，需在控制台申请并选"Web服务"。
AMAP_KEY = _env("AMAP_KEY")
AMAP_BASE = _env("AMAP_BASE", "https://restapi.amap.com/v3")
# 天地图。注意：**服务器端 key 强制要求 Referer**，否则返回 403「权限类型错误」。
TIANDITU_TK = _env("TIANDITU_TK")
TIANDITU_BASE = _env("TIANDITU_BASE", "https://api.tianditu.gov.cn")
TIANDITU_REFERER = _env("TIANDITU_REFERER", "http://lbs.tianditu.gov.cn/")

# ---------------------------------------------------------------- 运行参数
THREADS = _env_int("DIZHI_THREADS", 4)
MEMORY_LIMIT = _env("DIZHI_MEMORY", "2GB")


def ensure_temp_dir() -> str:
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    return TEMP_DIR
