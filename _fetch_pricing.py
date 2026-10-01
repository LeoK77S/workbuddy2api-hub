#!/usr/bin/env python3
"""刷新 wb_pricing.py 使用的定价快照 —— 全部走 OpenRouter 的单一价。

唯一来源：OpenRouter 的模型列表 API（https://openrouter.ai/api/v1/models），
它给出每个模型每 token 的美元单价（prompt / completion / input_cache_read）。
那是**模型级公布价**：同一个模型在 OpenRouter 上可能由多家 provider 承接，这个
数是它默认路由那家的价，不是各家里的最低价。快照里每个条目都是 USD，展示时按
快照中的汇率折算成人民币；不再有厂商一手页，口径因此统一为「OpenRouter 价格」。

不少模型在 OpenRouter 上是按条件定价的，条件写在条目的 overrides 数组里，
共两种：

  - 按输入长度：min_prompt_tokens 达到阈值后改用更贵的价（如 gpt-6-astra
    在输入超过 272000 token 时单价翻倍）；
  - 按时段（UTC）：utc_days 加 utc_start / utc_end，如 tencent/hy3、
    tencent/hy4-preview、deepseek/deepseek-v4-pro-0813。

这些档位会被原样搬进快照的 bands 字段，由 wb_pricing 按每条请求的时间与输入
长度取档；没有条件定价的模型只有一个 flat 价。

overrides 里另有 input_cache_write / audio / input_audio_cache 这几个字段，
本地 usage 日志不含对应的 token 数（只有 prompt / completion / reasoning /
cached），计不出价，所以不取。

模型清单与匹配（新增模型无需改本脚本）：
  - 输入清单取自本项目内置的模型目录 wb_catalog.py，所以目录里新增模型后，
    重跑本脚本就会尝试为它取价；
  - 匹配顺序：人工覆盖表 OVERRIDES（处理名字对不上的少数模型）→ 自动匹配；
  - 自动匹配只认「名字归一化后与 OpenRouter 某条目完全一致、且结果唯一」的
    候选（并排除 :batch / :free 这类变体后缀）。宁可漏也不错：匹配不到的
    模型不写进快照，费用列显示 "—"，名字会在本脚本输出里列出来供人工补表。

输出只描述「模型 → 单价」，不含任何业务逻辑；费用计算在 wb_pricing.py 里完成。

用法：
    python _fetch_pricing.py               # 抓取并写入 pricing/pricing.json
    python _fetch_pricing.py --dry-run     # 只打印，不写文件
    python _fetch_pricing.py --embed       # 同时回写 wb_pricing.py 的内嵌副本
                                           # （Docker 镜像不带 pricing/ 目录）

wb_pricing.py 的加载顺序：WB_PRICING_FILE → ./pricing/pricing.json → 内嵌快照。
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pricing", "pricing.json")
EMBED_TARGET = os.path.join(HERE, "wb_pricing.py")

# 展示货币与汇率：快照里的美元单价按此折算成人民币展示。
USD_CNY = 7.10

# 人工覆盖表：hub 模型 id → OpenRouter id。只放「名字对不上、无法自动匹配」
# 的模型；能自动匹配的不要写进来，免得又变成一张需要人工维护的大表。
OVERRIDES = {
    # hub 名字比 OpenRouter 多/少一个后缀
    "hy4-preview-f": "tencent/hy4-preview",
    "hy4-preview-dev": "tencent/hy4-preview",
    "hy4-preview-x": "tencent/hy4-preview",
    "hy3-x": "tencent/hy3",
    "kimi-k3-1": "moonshotai/kimi-k3",
    "kimi-k2.7": "moonshotai/kimi-k2.7-code",
    # hub 名字与 OpenRouter 的写法不同（版本号/代号差异）
    "glm-5.0-turbo": "z-ai/glm-5-turbo",
    "deepseek-v3-1": "deepseek/deepseek-chat-v3.1",
    "deepseek-v3-1-volc": "deepseek/deepseek-chat-v3.1",
    "deepseek-v3-2-volc": "deepseek/deepseek-v3.2",
    "deepseek-v3-0324": "deepseek/deepseek-chat-v3-0324",
    # 混元系列在 OpenRouter 上是 a13b-instruct 这一个条目
    "hunyuan-2.0-instruct": "tencent/hunyuan-a13b-instruct",
    "hunyuan-chat": "tencent/hunyuan-a13b-instruct",
}

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


def fetch_openrouter():
    """拉取 OpenRouter 模型列表，返回 {id: pricing}。

    网络失败时抛异常，由 build() 决定是整体失败还是沿用旧快照里的美元价。
    """
    req = urllib.request.Request(
        OPENROUTER_URL, headers={"User-Agent": "workbuddy2api-hub-pricing/1.0"})
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


# OpenRouter 把按时段定价的档位放在条目的 overrides 数组里：utc_days 限定 UTC
# 星期几，utc_start / utc_end 是 HHMM 整数（1600 = 16:00，0 作为终点表示当天
# 结束）。同一个数组里还有 min_prompt_tokens 那类「按上下文长度分档」的项，
# 它与时段无关，这里不取——两者的定价维度不是一回事。
WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday")
WEEKDAY_SHORT = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _rates(item):
    """一条 OpenRouter 记录 → 每百万 token 的三档美元价。"""
    return {
        "input_cache_hit": per_million(item.get("input_cache_read")),
        "input_cache_miss": per_million(item.get("prompt")),
        "output": per_million(item.get("completion")),
    }


def bands_from_overrides(or_entry):
    """条件档位列表；没有条件定价的模型返回空列表。

    OpenRouter 用 overrides 表达两种条件价：

      - min_prompt_tokens：输入长度达到该阈值后改用的价；
      - utc_days / utc_start / utc_end：按时段（UTC）改用的价，
        start / end 是 HHMM 整数（1600 = 16:00），0 作为终点表示当天结束。

    两种条件在源数据里互不混用。档位按源数组的顺序保留，因为引擎取的是
    「最后一条满足条件的档」——这正是源数据的语义：长度档按阈值升序排列
    （实测 32000 → 128000），时段档则互不重叠。与基准价重复、不带任何
    条件的项不取。

    overrides 里还有 input_cache_write / audio / input_audio_cache 这几个
    字段，本地 usage 日志不含对应的 token 数（只有 prompt / completion /
    reasoning / cached），计不出价，所以这里不取。
    """
    out = []
    for item in or_entry.get("overrides") or []:
        if not isinstance(item, dict):
            continue
        band = {"flat": _rates(item)}
        threshold = item.get("min_prompt_tokens")
        if threshold is not None:
            band["min_prompt_tokens"] = int(threshold)
        elif any(str(k).startswith("utc_") for k in item):
            days = item.get("utc_days")
            picked = [WEEKDAY_SHORT[WEEKDAY_KEYS.index(str(d).lower())]
                      for d in (days if isinstance(days, list) else [])
                      if str(d).lower() in WEEKDAY_KEYS]
            end = item.get("utc_end")
            band.update({
                "days": picked or None,
                "start": int(item.get("utc_start") or 0),
                "end": int(end) if end else 2400,
            })
        else:
            continue
        out.append(band)
    return out


def hub_model_ids():
    """内置目录里的模型 id（国际版在前，保序去重）。

    这是本脚本的输入清单：目录里新增模型，重跑就有了候选。非 chat 的虚拟
    别名（default-model 等）在 OpenRouter 里匹配不到，自然落进未定价清单。
    """
    import wb_catalog
    ids, seen = [], set()
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                   getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def normalize(name):
    """名字归一化：小写、去掉所有非字母数字（- . _ 等一律忽略）。"""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def index_openrouter(or_models):
    """把 OpenRouter 列表按归一化名字建索引，排除 :batch / :free 等变体。"""
    by_norm = {}
    for mid in or_models:
        if ":" in mid:
            continue
        by_norm.setdefault(normalize(mid.split("/")[-1]), []).append(mid)
    return by_norm


def auto_match(hub_id, by_norm):
    """给一个 hub 模型找 OpenRouter 条目：名字归一化后全等且唯一。

    有多个候选（或没有）时返回 None——宁可让费用列显示 "—"，也不拿一个
    近似的名字去猜价格。要覆盖这种情况就把映射写进 OVERRIDES。
    """
    cands = by_norm.get(normalize(str(hub_id).split("/")[-1])) or []
    return cands[0] if len(cands) == 1 else None


def resolve(hub_id, or_models, by_norm):
    """(OpenRouter id, 是否来自人工覆盖)，匹配不到返回 (None, False)。"""
    ref = OVERRIDES.get(hub_id)
    if ref and ref in or_models:
        return ref, True
    auto = auto_match(hub_id, by_norm)
    return (auto, False) if auto else (None, False)


def load_existing_models():
    """读回上次生成的快照里的 models（若存在），供离线降级使用。"""
    try:
        with open(OUT, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("models") or {}
    except Exception:
        return {}


def build():
    """(doc, unpriced, overridden)。OpenRouter 抓不到时沿用上次快照的价。"""
    prev = {}
    try:
        or_models = fetch_openrouter()
    except Exception as exc:
        prev = load_existing_models()
        if not prev:
            raise SystemExit("openrouter fetch failed and no previous snapshot: %s" % exc)
        print("warn: openrouter fetch failed (%s); reusing previous prices" % exc,
              file=sys.stderr)
        or_models = None

    by_norm = index_openrouter(or_models) if or_models else {}
    models, unpriced, overridden = {}, [], []
    for hub_id in hub_model_ids():
        if or_models is None:
            old = prev.get(hub_id)
            if old:
                models[hub_id] = old
            else:
                unpriced.append(hub_id)
            continue
        ref, via_override = resolve(hub_id, or_models, by_norm)
        if not ref:
            unpriced.append(hub_id)
            continue
        if via_override:
            overridden.append(hub_id)
        p = or_models.get(ref) or {}
        entry = {
            "display": hub_id,
            "source": "openrouter",
            "currency": "USD",
            "unit": 1_000_000,
            "or_id": ref,
            "flat": _rates(p),
        }
        # A few models price by UTC time of day; the base price stays as the
        # fallback for a request whose timestamp matches no band.
        bands = bands_from_overrides(p)
        if bands:
            entry["bands"] = bands
        models[hub_id] = entry
    doc = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base_currency": "CNY",
            "usd_cny": USD_CNY,
            "sources": {"openrouter": OPENROUTER_URL},
            "note": "单价均为「每百万 token」的美元价，取自 OpenRouter 公布的模型级价"
                    "（同一模型可能由多家 provider 承接，取它默认路由那家的公布价，"
                    "不是各家最低价）；按 usd_cny 折算成人民币；按条件定价的模型"
                    "（输入长度阈值或 UTC 时段）另带 bands，按每条请求的输入长度与"
                    "时间取档。OpenRouter 的缓存写入价与音频价，本地 usage 日志没有"
                    "对应的 token 数，未计入。",
        },
        "models": models,
    }
    return doc, unpriced, overridden


def embed(text, count):
    """把快照回写进 wb_pricing.py 的内嵌副本。

    只替换 _JSON = r'''...''' 的内容，文件其余部分原样保留；找不到标记时报错
    退出，而不是把一个手工改过的文件悄悄写坏。
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
    doc, unpriced, overridden = build()
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
    print("matched via OVERRIDES: %d (%s)"
          % (len(overridden), ", ".join(overridden) or "-"))
    if unpriced:
        print("no pricing for %d model(s): %s"
              % (len(unpriced), ", ".join(unpriced)), file=sys.stderr)


if __name__ == "__main__":
    main()
