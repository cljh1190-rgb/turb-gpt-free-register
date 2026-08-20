# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（USE_EMAIL_SERVICE=True 时邮箱由当前 Provider 自动领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时由当前邮箱 Provider 自动领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'REGISTER_EMAIL': 'str', 'REGISTER_NAME': 'str', 'REGISTER_PASSWORD': 'str'})
