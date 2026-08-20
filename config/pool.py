# -*- coding: utf-8 -*-
"""号池（account pool）服务配置。"""
from config.env_loader import apply_env_overrides


# 是否启用号池服务。False 时 acquire/switch 直接返回不可用，巡检线程不执行。
POOL_ENABLED: bool = True

# 额度剩余百分比阈值：primary_remaining_percent <= 该值即视为额度耗尽不可分配。
POOL_QUOTA_THRESHOLD_PERCENT: float = 20.0

# 没有额度数据的账号默认可用（True=按“未知但可用”分配；False=按不可用处理）。
POOL_ALLOW_UNKNOWN_QUOTA: bool = True

# 主动巡检间隔（秒）；0 = 关闭巡检线程。
POOL_PROBE_INTERVAL_SECONDS: int = 600

# 巡检时，账号“最近一次成功查额度”超过该时长视为过期，会重新入队额度检查（秒）。
POOL_PROBE_STALE_SECONDS: int = 12 * 3600

# 号池分配策略：round_robin=轮询（默认，避免同一账号被连续分配） / random=随机。
POOL_ACQUIRE_STRATEGY: str = "round_robin"


apply_env_overrides(globals(), {
    "POOL_ENABLED": "bool",
    "POOL_QUOTA_THRESHOLD_PERCENT": "float",
    "POOL_ALLOW_UNKNOWN_QUOTA": "bool",
    "POOL_PROBE_INTERVAL_SECONDS": "int",
    "POOL_PROBE_STALE_SECONDS": "int",
    "POOL_ACQUIRE_STRATEGY": "str",
})
