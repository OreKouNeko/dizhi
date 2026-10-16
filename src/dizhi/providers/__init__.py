"""第三方地图 API 适配层。

每个 provider 都是**独立可调用**的：不依赖本项目的检索引擎、不依赖彼此，
拿到 key 就能单独跑。想换一家（百度/腾讯/天地图）照着写一个同签名的类即可。

    from dizhi.providers import AmapClient
    c = AmapClient("你的key")
    pois = c.search_nationwide("智能建造学院")
"""
from dizhi.providers.amap import AmapClient
from dizhi.providers.tianditu import TiandituClient

__all__ = ["AmapClient", "TiandituClient"]
