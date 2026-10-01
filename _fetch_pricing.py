#!/usr/bin/env python3
"""刷新 wb_pricing.py 使用的权威定价快照。

两个来源：
  1. DeepSeek 官方文档站（人民币，含峰谷/错峰定价）——一手权威，硬编码于此，
     因为官方页面是渲染后的 HTML，抓取不稳定，且价格变动不频繁。
  2. OpenRouter 模型列表 API（美元，聚合各厂商官方价）——可编程、覆盖广，
     用于 DeepSeek 之外的模型。

输出只描述「模型 → 单价」，不含任何业务逻辑；费用计算在 wb_pricing.py
里完成。这样定价可以独立更新，不必改计算逻辑。

用法：
    python _fetch_pricing.py            # 抓取并写入 pricing/pricing.json
    python _fetch_pricing.py --dry-run  # 只打印，不写文件
    python _fetch_pricing.py --embed    # 同时把快照回写进 wb_pricing.py 的
                                        # 内嵌副本（Docker 镜像不带 pricing/
                                        # 目录，靠内嵌副本计价）

wb_pricing.py 的加载顺序：WB_PRICING_FILE → ./pricing/pricing.json → 内嵌快照。
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pricing", "pricing.json")
EMBED_TARGET = os.path.join(HERE, "wb_pricing.py")

# 展示货币与汇率：所有费用统一折算成人民币展示。
# DeepSeek 官方源本身就是人民币；OpenRouter 是美元，按此汇率折算。
USD_CNY = 7.10

# --- DeepSeek 官方定价（人民币 / 每百万 token）--------------------------------
# 来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
# 峰谷规则：高峰为北京时间周一至周五 9:00-12:00 与 14:00-18:00（不含法定节假日），
# 空闲时段价格为高峰的一半。
DEEPSEEK_OFFICIAL = {
    "deepseek-flash": {
        "display": "DeepSeek-V4.1-Flash",
        "peak": {"input_cache_hit": 0.04, "input_cache_miss": 2.0, "output": 8.0},
        "off_peak": {"input_cache_hit": 0.02, "input_cache_miss": 1.0, "output": 4.0},
    },
    "deepseek-v4-pro": {
        "display": "DeepSeek-V4-Pro-0813",
        "peak": {"input_cache_hit": 0.30, "input_cache_miss": 9.0, "output": 27.0},
        "off_peak": {"input_cache_hit": 0.15, "input_cache_miss": 4.5, "output": 13.5},
    },
}

# 中国法定节假日（北京时间，含调休的放假日）。命中即整天按空闲价计。
# 来源：国务院办公厅《关于 2026 年部分节假日安排的通知》（国办发明电〔2025〕7号）。
# 需每年人工更新；跨年未更新时只影响该年节假日的峰谷判定，不影响其余计价。
HOLIDAYS = [
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明节
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆节
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
]

# --- hub 模型 id → 定价条目 ---------------------------------------------------
# kind=deepseek：走 DeepSeek 官方峰谷表（peak / off_peak）
# kind=openrouter：走 OpenRouter 单一价（flat），按汇率折算成人民币
MODEL_MAP = {
    # 国际版
    "deepseek-v4.1-flash": {"kind": "deepseek", "ref": "deepseek-flash"},
    "deepseek-v4-flash": {"kind": "openrouter", "ref": "deepseek/deepseek-v4-flash"},
    "deepseek-v4-pro": {"kind": "deepseek", "ref": "deepseek-v4-pro"},
    "gpt-6-astra": {"kind": "openrouter", "ref": "openai/gpt-6-astra"},
    "gpt-5.6-sol": {"kind": "openrouter", "ref": "openai/gpt-5.6-sol"},
    "gpt-5.6-terra": {"kind": "openrouter", "ref": "openai/gpt-5.6-terra"},
    "gpt-5.6-luna": {"kind": "openrouter", "ref": "openai/gpt-5.6-luna"},
    "gpt-5.5": {"kind": "openrouter", "ref": "openai/gpt-5.5"},
    "gpt-5.4": {"kind": "openrouter", "ref": "openai/gpt-5.4"},
    "grok-4.7": {"kind": "openrouter", "ref": "x-ai/grok-4.7"},
    "gpt-5.3-codex": {"kind": "openrouter", "ref": "openai/gpt-5.3-codex"},
    "gemini-3.5-flash": {"kind": "openrouter", "ref": "google/gemini-3.5-flash"},
    "kimi-k3": {"kind": "openrouter", "ref": "moonshotai/kimi-k3"},
    # 国内版
    "glm-5.3": {"kind": "openrouter", "ref": "z-ai/glm-5.3"},
    "glm-5.3-flash": {"kind": "openrouter", "ref": "z-ai/glm-5.3-flash"},
    "glm-5.2": {"kind": "openrouter", "ref": "z-ai/glm-5.2"},
    "glm-5.1": {"kind": "openrouter", "ref": "z-ai/glm-5.1"},
    "glm-5.0-turbo": {"kind": "openrouter", "ref": "z-ai/glm-5-turbo"},
    "glm-4.6": {"kind": "openrouter", "ref": "z-ai/glm-4.6"},
    "glm-4.6v": {"kind": "openrouter", "ref": "z-ai/glm-4.6v"},
    "glm-5v-turbo": {"kind": "openrouter", "ref": "z-ai/glm-5v-turbo"},
    "kimi-k3": {"kind": "openrouter", "ref": "moonshotai/kimi-k3"},
    "kimi-k3-1": {"kind": "openrouter", "ref": "moonshotai/kimi-k3"},
    "kimi-k2.6": {"kind": "openrouter", "ref": "moonshotai/kimi-k2.6"},
    "kimi-k2.7": {"kind": "openrouter", "ref": "moonshotai/kimi-k2.7-code"},
    "kimi-k2.5": {"kind": "openrouter", "ref": "moonshotai/kimi-k2.5"},
    "kimi-k2-thinking": {"kind": "openrouter", "ref": "moonshotai/kimi-k2-thinking"},
    "hy3": {"kind": "openrouter", "ref": "tencent/hy3"},
    "hy3-x": {"kind": "openrouter", "ref": "tencent/hy3"},
    "hy4-preview": {"kind": "openrouter", "ref": "tencent/hy4-preview"},
    "hy4-preview-f": {"kind": "openrouter", "ref": "tencent/hy4-preview"},
    "hy4-preview-dev": {"kind": "openrouter", "ref": "tencent/hy4-preview"},
    "hy4-preview-x": {"kind": "openrouter", "ref": "tencent/hy4-preview"},
    "minimax-m2.5": {"kind": "openrouter", "ref": "minimax/minimax-m2.5"},
    "minimax-m2.7": {"kind": "openrouter", "ref": "minimax/minimax-m2.7"},
    "minimax-m3": {"kind": "openrouter", "ref": "minimax/minimax-m3"},
    "hunyuan-2.0-instruct": {"kind": "openrouter", "ref": "tencent/hunyuan-a13b-instruct"},
    "hunyuan-chat": {"kind": "openrouter", "ref": "tencent/hunyuan-a13b-instruct"},
    "deepseek-v3-2-volc": {"kind": "openrouter", "ref": "deepseek/deepseek-v3.2"},
    "deepseek-v3-1-volc": {"kind": "openrouter", "ref": "deepseek/deepseek-chat-v3.1"},
    "deepseek-v3-1": {"kind": "openrouter", "ref": "deepseek/deepseek-chat-v3.1"},
    "deepseek-v3-0324": {"kind": "openrouter", "ref": "deepseek/deepseek-chat-v3-0324"},
    "deepseek-r1-0528": {"kind": "openrouter", "ref": "deepseek/deepseek-r1-0528"},
}

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


def fetch_openrouter():
    """拉取 OpenRouter 模型列表，返回 {id: pricing}。

    网络失败（离线、代理不通、TLS 中断）时抛异常，由 build() 决定是整体失败
    还是沿用旧快照——DeepSeek 官方价与节假日表是本地硬编码的，不该因为一次
    网络抖动而丢失。
    """
    req = urllib.request.Request(
        OPENROUTER_URL, headers={"User-Agent": "WbCostDashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = {}
    for m in payload.get("data", []):
        out[m.get("id")] = m.get("pricing") or {}
    return out


def per_million(value):
    """OpenRouter 单价是「每 token 的美元」，转成「每百万 token」。"""
    try:
        return round(float(value) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def load_existing_models():
    """读回上次生成的定价快照里的 models（若存在），供离线降级使用。"""
    try:
        with open(OUT, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("models") or {}
    except Exception:
        return {}


def build():
    # OpenRouter 抓不到就沿用上次快照里的美元定价：DeepSeek 官方价与节假日表
    # 都是本地硬编码的，不该被一次网络抖动牵连。完全没有旧数据才报错退出。
    prev = {}
    try:
        or_models = fetch_openrouter()
    except Exception as exc:
        prev = load_existing_models()
        if not prev:
            raise SystemExit("openrouter fetch failed and no previous snapshot: %s" % exc)
        print("warn: openrouter fetch failed (%s); reusing previous USD prices" % exc,
              file=sys.stderr)
        or_models = None
    models = {}
    missing = []
    for wid, spec in MODEL_MAP.items():
        if spec["kind"] == "deepseek":
            src = DEEPSEEK_OFFICIAL.get(spec["ref"])
            if not src:
                missing.append(wid)
                continue
            models[wid] = {
                "display": src["display"],
                "source": "deepseek_official",
                "currency": "CNY",
                "unit": 1_000_000,
                "schedule": "deepseek_cn",
                "peak": dict(src["peak"]),
                "off_peak": dict(src["off_peak"]),
            }
        else:
            if or_models is None:
                # 离线降级：直接沿用旧快照里该模型的整条定价。
                old = prev.get(wid)
                if old:
                    models[wid] = old
                else:
                    missing.append(wid)
                continue
            p = or_models.get(spec["ref"])
            if not p:
                missing.append(wid)
                continue
            models[wid] = {
                "display": spec["ref"].split("/")[-1],
                "source": "openrouter",
                "currency": "USD",
                "unit": 1_000_000,
                "or_id": spec["ref"],
                "flat": {
                    "input_cache_hit": per_million(p.get("input_cache_read")),
                    "input_cache_miss": per_million(p.get("prompt")),
                    "output": per_million(p.get("completion")),
                },
            }
    doc = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base_currency": "CNY",
            "usd_cny": USD_CNY,
            "sources": {
                "deepseek_official": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
                "openrouter": OPENROUTER_URL,
            },
            "note": "单价均为「每百万 token」；DeepSeek 走峰谷表，其余走单一价。",
            "holidays": HOLIDAYS,
            "holidays_source": "国务院办公厅《关于 2026 年部分节假日安排的通知》（国办发明电〔2025〕7号）",
        },
        "models": models,
    }
    return doc, missing


def embed(text, count):
    """把快照回写进 wb_pricing.py 的内嵌副本。

    只替换 _JSON = r'''...''' 的内容，文件其余部分原样保留；找不到标记时
    报错退出，而不是把一个手工改过的文件悄悄写坏。
    """
    with open(EMBED_TARGET, encoding="utf-8") as fh:
        src = fh.read()
    start_marker = "_JSON = r'''\n"
    end_marker = "\n'''"
    start = src.find(start_marker)
    if start < 0:
        raise SystemExit("cannot find the inline _JSON block in %s" % EMBED_TARGET)
    start += len(start_marker)
    end = src.find(end_marker, start)
    if end < 0:
        raise SystemExit("cannot find the end of the inline _JSON block in %s"
                         % EMBED_TARGET)
    with open(EMBED_TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src[:start] + text + src[end:])
    print("embedded %d models into %s" % (count, EMBED_TARGET))


def main():
    dry = "--dry-run" in sys.argv
    want_embed = "--embed" in sys.argv
    doc, missing = build()
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if dry:
        print(text)
    else:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("wrote %s (%d models)" % (OUT, len(doc["models"])))
        if want_embed:
            embed(text, len(doc["models"]))
    if missing:
        print("no pricing for: %s" % ", ".join(missing), file=sys.stderr)


if __name__ == "__main__":
    main()
