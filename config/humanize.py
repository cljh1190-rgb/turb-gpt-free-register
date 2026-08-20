# -*- coding: utf-8 -*-
"""
人工操作节奏配置。

协议请求本身很快；真实浏览器人工操作通常会有页面加载、阅读、输入、切换邮箱
等停顿。这里集中配置轻量随机延迟，避免全流程固定节拍。
"""
from config.env_loader import apply_env_overrides

# 总开关。关闭后 delay() 直接返回。
ENABLE_HUMANIZE_DELAY = True

# 延迟倍率；批量跑得太慢时可调小到 0.5。
HUMANIZE_DELAY_FACTOR = 1.0

# 每类动作的随机停顿区间（秒）。
HUMANIZE_DELAYS = {
    # 普通 API 间隔：看起来像页面 JS 发完一个请求后处理状态。
    "api": (0.45, 1.35),
    # 页面跳转 / 重定向后等页面稳定。
    "navigate": (1.2, 3.2),
    # Sentinel / Turnstile / PoW 相关，给 SDK 运行和 UI 等待留时间。
    "challenge": (0.8, 2.4),
    # 邮箱验证码到达后，模拟用户切回页面和输入。
    "otp_input": (2.5, 8.0),
    # 填写姓名生日等表单。
    "form": (1.8, 5.0),
    # 注册完成后进入应用、拉 session。
    "post_auth": (1.5, 4.0),
    # 并发任务错峰。
    "job_stagger": (0.4, 1.8),
}

# 延迟采样分布：
#   uniform   —— 均匀分布（旧行为）
#   lognormal —— 对数正态：多数停顿偏短、偶发偏长，更接近真人节奏
#   gamma     —— 伽马分布（shape=2），同样右偏
HUMANIZE_DISTRIBUTION = "lognormal"

# lognormal/gamma 的右偏程度；越大越“多数很快、偶尔很慢”。
HUMANIZE_SKEW = 0.45

# 按 kind 配置“跳过延迟”的概率（0-1）。真人不会每一步之间都固定停顿，
# 让部分 API 间隔直接为 0，破坏机器节拍。关键的输入/等待类保持 0。
HUMANIZE_SKIP_PROBABILITY = {
    "api": 0.15,
    "navigate": 0.05,
    "challenge": 0.10,
    "otp_input": 0.0,
    "form": 0.0,
    "post_auth": 0.0,
    "job_stagger": 0.0,
}

# 偶发“走神”长停顿：命中后在本应停顿之外额外加一段长等待。
# 只作用于以下 kind，避免拖慢纯等待类（otp_input 本身已足够长）。
HUMANIZE_PAUSE_PROBABILITY = 0.05
HUMANIZE_PAUSE_RANGE = (5.0, 15.0)
HUMANIZE_PAUSE_KINDS = {"navigate", "form", "post_auth", "challenge"}

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_HUMANIZE_DELAY': 'bool',
    'HUMANIZE_DELAY_FACTOR': 'float',
    'HUMANIZE_DISTRIBUTION': 'str',
    'HUMANIZE_SKEW': 'float',
    'HUMANIZE_PAUSE_PROBABILITY': 'float',
})
