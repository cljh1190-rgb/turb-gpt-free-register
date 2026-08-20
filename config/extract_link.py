# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 总开关：关闭时禁止手动、批量和套餐后的自动提链。
EXTRACT_LINK_ENABLED: bool = False

# 提链服务地址（BurstPro: https://upi.burstpro-ai.online ；/cdk-api 是文档页不是 API 前缀）
EXTRACT_LINK_API_BASE: str = ""

# 提链 provider：auto、linkpp、workbench、burstpro、legacy
EXTRACT_LINK_PROVIDER: str = "auto"

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 提链类型：pix / upi / kakao_pay / ideal / paypal
EXTRACT_LINK_TYPE: str = "pix"

# Link Atelier 工作台参数。代理池使用 host:port:user:password 或完整代理 URL，
# 两个池会随每个任务提交，由远端按 Checkout / Update 阶段分别轮换。
EXTRACT_LINK_CHECKOUT_PROXY_POOL: list[str] = []
EXTRACT_LINK_UPDATE_PROXY_POOL: list[str] = []
EXTRACT_LINK_WORKBENCH_COUNTRY: str = ""
EXTRACT_LINK_WORKBENCH_PAYMENT_METHOD: str = ""
EXTRACT_LINK_WORKBENCH_APPLY_UPDATE: bool = True
EXTRACT_LINK_WORKBENCH_OAICS_ONLY: bool = False
EXTRACT_LINK_WORKBENCH_WINDOW_ID: str = ""

# link-pp PayPal 0 元提链参数（本地服务默认 http://127.0.0.1:5572）。
EXTRACT_LINK_LINKPP_COUNTRY: str = "GB"
EXTRACT_LINK_LINKPP_BILLING_COUNTRY: str = "GB"
EXTRACT_LINK_LINKPP_CHECKOUT_ATTEMPTS: int = 3
EXTRACT_LINK_LINKPP_PROVIDER_ATTEMPTS: int = 5
EXTRACT_LINK_LINKPP_STRIPE_CHECKOUT: bool = True
EXTRACT_LINK_LINKPP_STRIPE_ENGINE: str = "go"
EXTRACT_LINK_LINKPP_STRIPE_PROMO_STRATEGY: str = "mixed"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

apply_env_overrides(globals(), {
    'EXTRACT_LINK_ENABLED': 'bool',
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_PROVIDER': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_CHECKOUT_PROXY_POOL': 'list_str_multiline',
    'EXTRACT_LINK_UPDATE_PROXY_POOL': 'list_str_multiline',
    'EXTRACT_LINK_WORKBENCH_COUNTRY': 'str',
    'EXTRACT_LINK_WORKBENCH_PAYMENT_METHOD': 'str',
    'EXTRACT_LINK_WORKBENCH_APPLY_UPDATE': 'bool',
    'EXTRACT_LINK_WORKBENCH_OAICS_ONLY': 'bool',
    'EXTRACT_LINK_WORKBENCH_WINDOW_ID': 'str',
    'EXTRACT_LINK_LINKPP_COUNTRY': 'str',
    'EXTRACT_LINK_LINKPP_BILLING_COUNTRY': 'str',
    'EXTRACT_LINK_LINKPP_CHECKOUT_ATTEMPTS': 'int',
    'EXTRACT_LINK_LINKPP_PROVIDER_ATTEMPTS': 'int',
    'EXTRACT_LINK_LINKPP_STRIPE_CHECKOUT': 'bool',
    'EXTRACT_LINK_LINKPP_STRIPE_ENGINE': 'str',
    'EXTRACT_LINK_LINKPP_STRIPE_PROMO_STRATEGY': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
})
