"""
极简使用统计（双后端，自动切换）
================================
统计两个数字：visits（访问次数）、analyses（核查次数）。

后端自动选择：
  - 若设置了环境变量 UPSTASH_REDIS_REST_URL 与 UPSTASH_REDIS_REST_TOKEN
    → 使用 Upstash Redis（外部存储，重启 / 重新部署都不丢，推荐线上用）。
  - 否则 → 退回本地 JSON 文件（路径由 STATS_FILE 指定，默认 ./stats.json；
    适合本地开发，但免费 PaaS 上重启会清零）。

两种后端切换【无需改代码】，只要在平台上配置环境变量即可。
依赖 httpx（项目已依赖）。统计读写失败【绝不影响主功能】（异常被吞掉、降级为 0）。
"""

import json
import os
import threading
from pathlib import Path

# 直接运行/导入本模块时也尝试加载 .env（与 app.py 中重复调用无害）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_KEYS = ("visits", "analyses")

# ---- Upstash（外部存储，可选）----
_UP_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_UP_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
_USE_UPSTASH = bool(_UP_URL and _UP_TOKEN)

# ---- 本地文件（回退）----
_FILE = Path(os.environ.get("STATS_FILE", Path(__file__).parent / "stats.json"))
_LOCK = threading.Lock()


# ===== Upstash 后端（通过 REST，命令以 JSON 数组发送）=====
def _up_cmd(*args):
    import httpx
    r = httpx.post(
        _UP_URL,
        headers={"Authorization": f"Bearer {_UP_TOKEN}"},
        json=list(args),
        timeout=3.0,
    )
    r.raise_for_status()
    return r.json().get("result")


def _up_get() -> dict:
    try:
        res = _up_cmd("MGET", *_KEYS)            # 形如 ["2", null]
        out = {}
        for k, v in zip(_KEYS, res or []):
            out[k] = int(v) if v not in (None, "") else 0
        for k in _KEYS:
            out.setdefault(k, 0)
        return out
    except Exception:
        return {k: 0 for k in _KEYS}


def _up_increment(key: str) -> dict:
    try:
        _up_cmd("INCR", key)                     # 原子自增
    except Exception:
        pass
    return _up_get()


# ===== 本地文件后端 =====
def _file_load() -> dict:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return {k: int(data.get(k, 0)) for k in _KEYS}
    except Exception:
        return {k: 0 for k in _KEYS}


def _file_save(data: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _file_increment(key: str) -> dict:
    with _LOCK:
        data = _file_load()
        data[key] = data.get(key, 0) + 1
        _file_save(data)
        return data


# ===== 对外接口 =====
def increment(key: str) -> dict:
    if key not in _KEYS:
        return get()
    return _up_increment(key) if _USE_UPSTASH else _file_increment(key)


def get() -> dict:
    return _up_get() if _USE_UPSTASH else _file_load()


def backend() -> str:
    return "upstash" if _USE_UPSTASH else "file"
