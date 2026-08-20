# -*- coding: utf-8 -*-
"""随机化操作节奏，让协议流程更接近人工浏览器操作。

默认使用右偏分布（lognormal/gamma）采样停顿：多数时候偏短、偶尔偏长，
并按 kind 配置跳过延迟概率 + 偶发“走神”长停顿，破坏“每步必延迟、
uniform 均匀分布”的机器节拍。

兼容旧配置：ENABLE_HUMANIZE_DELAY / HUMANIZE_DELAY_FACTOR 仍生效；
HUMANIZE_DISTRIBUTION="uniform" 可完整恢复旧行为。
"""
import logging
import math
import random
import time

logger = logging.getLogger(__name__)


def _load_config(kind: str, minimum: float | None, maximum: float | None):
    """读取 humanize 配置，返回 (lo, hi, factor, dist, skew, skip_p, pause_p, pause_lo, pause_hi, pause_kinds)。"""
    try:
        from config import humanize as _cfg
        enabled = bool(getattr(_cfg, "ENABLE_HUMANIZE_DELAY", True))
        if minimum is None or maximum is None:
            lo, hi = getattr(_cfg, "HUMANIZE_DELAYS", {}).get(kind, (0.4, 1.2))
            minimum = lo if minimum is None else minimum
            maximum = hi if maximum is None else maximum
        factor = float(getattr(_cfg, "HUMANIZE_DELAY_FACTOR", 1.0) or 1.0)
        dist = str(getattr(_cfg, "HUMANIZE_DISTRIBUTION", "lognormal") or "lognormal").strip().lower()
        skew = float(getattr(_cfg, "HUMANIZE_SKEW", 0.45) or 0.45)
        skip_p = float((getattr(_cfg, "HUMANIZE_SKIP_PROBABILITY", {}) or {}).get(kind, 0.0) or 0.0)
        pause_p = float(getattr(_cfg, "HUMANIZE_PAUSE_PROBABILITY", 0.05) or 0.05)
        pause_lo, pause_hi = getattr(_cfg, "HUMANIZE_PAUSE_RANGE", (5.0, 15.0))
        pause_kinds = set(getattr(_cfg, "HUMANIZE_PAUSE_KINDS", {"navigate", "form", "post_auth", "challenge"}) or ())
    except Exception:
        enabled = True
        minimum = 0.4 if minimum is None else minimum
        maximum = 1.2 if maximum is None else maximum
        factor = 1.0
        dist = "lognormal"
        skew = 0.45
        skip_p = 0.0
        pause_p = 0.05
        pause_lo, pause_hi = 5.0, 15.0
        pause_kinds = {"navigate", "form", "post_auth", "challenge"}
    lo = max(0.0, float(minimum) * factor)
    hi = max(lo, float(maximum) * factor)
    return enabled, lo, hi, dist, skew, skip_p, pause_p, pause_lo, pause_hi, pause_kinds


def _sample_delay(lo: float, hi: float, dist: str, skew: float) -> float:
    """在 [lo, hi] 区间内按分布采样；uniform 保持旧行为。"""
    if hi <= lo:
        return lo
    if dist == "uniform":
        return random.uniform(lo, hi)
    if dist in ("lognormal", "log-normal"):
        # 中位数落在区间中点，sigma 制造右偏；拒绝采样约束在 [lo, hi]。
        median = (lo + hi) / 2.0
        mu = math.log(median) if median > 0 else 0.0
        sigma = max(0.05, skew)
        while True:
            value = math.exp(random.normalvariate(mu, sigma))
            if lo <= value <= hi:
                return value
    if dist == "gamma":
        # shape=2 的 gamma：均值 = shape*scale = (lo+hi)/2，右偏。
        mean = (lo + hi) / 2.0
        scale = max(mean, 0.0) / 2.0
        while True:
            value = random.gammavariate(2.0, scale)
            if lo <= value <= hi:
                return value
    return random.uniform(lo, hi)


def delay(kind: str = "api", *, minimum: float | None = None, maximum: float | None = None) -> float:
    """
    按配置随机 sleep，返回实际 sleep 秒数。

    Args:
        kind: HUMANIZE_DELAYS 的 key。
        minimum/maximum: 临时覆盖区间。
    """
    enabled, lo, hi, dist, skew, skip_p, pause_p, pause_lo, pause_hi, pause_kinds = _load_config(kind, minimum, maximum)
    if not enabled:
        return 0.0

    # 按 kind 概率跳过本步延迟，打破“每步必延迟”的固定节拍。
    if skip_p > 0 and random.random() < skip_p:
        logger.debug(f"[Humanize] delay kind={kind}, skipped")
        return 0.0

    seconds = _sample_delay(lo, hi, dist, skew)

    # 偶发“走神”长停顿。
    if pause_p > 0 and kind in pause_kinds and random.random() < pause_p:
        seconds += random.uniform(pause_lo, pause_hi)

    logger.debug(f"[Humanize] delay kind={kind}, seconds={seconds:.2f}")
    time.sleep(seconds)
    return seconds
