# -*- coding: utf-8 -*-
"""注册完成后的官方 Plus 结账页交接配置。"""
from config.env_loader import apply_env_overrides


# True 时，账号保存成功后自动打开可见浏览器，并进入 ChatGPT 官方 Plus 页面。
# 这里只负责页面交接；卡资料与最终订阅确认始终在官方页面内由持卡人完成。
ENABLE_BILLING_HANDOFF: bool = False

# 只允许 chatgpt.com 的 HTTPS 地址；运行时会再次校验。
BILLING_HANDOFF_URL: str = "https://chatgpt.com/#pricing"

# 给账号数据落盘与浏览器资源释放预留的等待时间。
BILLING_HANDOFF_DELAY_SECONDS: int = 2


apply_env_overrides(globals(), {
    "ENABLE_BILLING_HANDOFF": "bool",
    "BILLING_HANDOFF_URL": "str",
    "BILLING_HANDOFF_DELAY_SECONDS": "int",
})
