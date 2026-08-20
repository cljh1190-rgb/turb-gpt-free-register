# AI 接管说明书

生成日期：2026-08-18

本文件面向后续接管本项目的 AI 编程代理。开始修改前必须先读本文件、`README.md`、`.env.example`，再检查当前工作树和运行状态。不要根据旧对话或包名猜测实际配置。

## 1. 项目定位

这是一个本地运行的 ChatGPT/OpenAI 注册、邮箱 OTP、Codex OAuth、套餐查询及账号管理工具，提供 CLI 和 Flask WebUI。

- CLI 入口：`main.py`
- WebUI 入口：`web.py`
- Flask 路由：`webui/app.py`
- 注册任务调度：`core/registration_service.py`
- 协议注册主流程：`main.py`、`core/chatgpt_auth.py`、`core/openai_auth.py`
- Codex OAuth：`core/codex_oauth.py`
- 邮箱统一接口：`core/email_provider.py`
- 通用取码邮箱：`core/generic_api_mail_client.py`
- 代理选择与健康检查：`config/proxy.py`
- JSON 持久化与账号/任务数据：`core/db.py`
- WebUI 主模板：`webui/templates/index.html`

## 2. 首次接管步骤

1. 确认 Python 版本建议为 3.11。
2. 在项目根目录创建虚拟环境并安装依赖：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. 从 `.env.example` 创建本机 `.env`，只在 `.env` 填写密钥。禁止把真实值写进源码或测试。
4. 运行测试：

   ```powershell
   python -m pytest -q
   ```

5. 本源码包生成时的基线为 `237 passed`。后续用例增加时总数可以变化，但不得忽略失败。
6. 启动 WebUI：

   ```powershell
   python web.py --open-browser
   ```

   默认地址：`http://127.0.0.1:5000/`

7. Windows 常驻运行可使用 `启动注册机.bat`，它通过 `_webui_keeper.py` 和 `_webui_supervisor.py` 托管 WebUI。

## 3. 协议模式配置

压缩包名称是“GPT协议注册”，但源码中的默认驱动可能仍是 `roxy`。纯协议模式必须在 `.env` 或 WebUI 配置中明确设置：

```dotenv
REGISTRATION_DRIVER=protocol
```

如需 Codex OAuth 同样走协议层，再按目标设置 `CODEX_OAUTH_DRIVER=protocol` 或 `same_as_registration`。不要为切换驱动直接删除浏览器实现，它们是现有回退和兼容路径。

协议链路的关键不变量：

- 从 OAuth signin、authorize、OTP、about-you 到 callback 必须复用同一会话和 cookie jar。
- 同一任务开始后不得中途更换代理出口、设备 ID、浏览器画像或时区。
- 代理预检必须发生在会触发 OTP 的请求之前。
- `invalid_state` 通常表示 state/cookie/authorize 上下文断裂，不是简单重发最后一步即可恢复。
- 403 只能在重试、响应体和代理出口信息收集完整后分类，不要把入口 IP 当作已验证出口 IP。

## 4. 邮箱与 OTP

邮箱来源由 `EMAIL_SOURCE` 控制，支持多来源按顺序兜底。当前实现包含 Outlook、Cloudflare、通用 API、GPTMail、MailNest、CloudMail 和 Throwaway。

通用 API 邮箱支持：

```text
email----取码URL
email----password----取码URL
email----password---取码URL
```

最后一种是部分供应商使用的混合分隔格式。URL 查询参数必须原样保留，密码必须传入邮箱池记录。取码端返回“暂无新邮件”或 HTTP 200 但没有 6 位验证码时，应继续轮询，不得当作验证码成功。

相关实现和测试：

- `core/generic_api_mail_client.py`
- `core/link_otp_login_service.py`
- `tests/test_otp_viewer.py`
- `tests/test_mail_link_registration.py`

## 5. 任务和运行状态

注册任务由 `core/registration_service.py` 调度，任务记录和日志由 `core/db.py` 管理。

- `pending/running/stopping` 任务不能在真实实例仍活跃时物理删除。
- 进程重启后，磁盘状态为 running/stopping 但内存无实例的任务属于僵尸任务，可回收或删除。
- 删除任务记录时应同步删除该任务对应日志，但不得顺带删除已注册账号。
- 重启 WebUI 前先确认没有活跃任务，避免中断 OTP 或 OAuth 会话。

## 6. 数据和密钥禁区

以下均为运行数据或秘密，不属于源码，禁止提交、打包、输出到日志或测试夹具：

- `.env`、`.env.*`（仅 `.env.example` 可分发）
- `accounts/`
- `codex_accounts/`
- `webcodex_pool/`
- `注册成功的邮箱.json`、`注册成功的邮箱.txt`、`注册成功的token.txt`
- `用于注册的邮箱.json/txt`
- `用于注册的API邮箱.json/txt`
- `注册任务.json`
- `注册日志/`
- `accounts_viewer.html`
- `_webui*.log`、`*.pid`、缓存、抓包和 HAR 原始数据
- 代理订阅、代理用户名/密码、邮箱 token、WebUI 授权码和第三方 API key

新增测试必须使用明显的虚构凭证，例如 `test-user`、`test-password`、`example.com`。

## 7. 修改纪律

- 工作树可能包含大量用户未提交修改。禁止 `git reset --hard`、`git checkout --` 或覆盖式回滚。
- 修改前先用 `git status --short` 和 `git diff -- <目标文件>` 识别已有变更。
- 优先复用现有模块和配置热加载机制，不要平行造一套配置系统。
- 修复协议步骤时同时补单元测试，测试网络行为时必须 mock 外部服务。
- 前端修改后同时检查桌面布局、文字溢出和 API 兼容性。
- 任何真实网络试跑都应先确认代理、邮箱和任务数量，避免并发产生不可控外部状态。

## 8. 常见故障定位顺序

### 8.1 `invalid_state`

1. 检查 authorize URL 的 state 是否与最初请求一致。
2. 检查全过程是否复用同一个 session/cookie jar。
3. 检查是否在 OTP 后重新创建 session 或切换代理。
4. 检查 redirect/callback 链是否被手工截断。
5. 状态已失效时应从 signin 重新开始，不要只重放步骤 10。

### 8.2 403 或 unusual activity

1. 记录入口代理、实际出口 IP、国家和 ASN，三者不要混淆。
2. 确认出口国家符合任务配置。
3. 检查 TLS/UA/Accept-Language/时区与设备画像是否自洽。
4. 只对明确可重试的网络错误轮换代理；会话建立后换出口必须整条流程重开。

### 8.3 OTP 不到

1. 先直接读取邮箱接口，确认 HTTP 状态和原始响应类型。
2. 检查邮箱行是否被正确解析，特别是三横线/四横线混合格式。
3. 检查是否错误解析了旧邮件、CSS 色值或页面演示数字。
4. 检查轮询超时、间隔和邮件时间戳过滤。

### 8.4 查询套餐 401

1. 检查 access token 是否存在、过期或属于另一个账号。
2. 检查套餐查询是否复用了注册时保存的设备和代理上下文。
3. 401 是鉴权失败，不应只靠无条件换 IP 掩盖。

## 9. 交付前验收

每次交付至少完成：

```powershell
python -m pytest -q
```

涉及 WebUI 时，再启动本地服务并用浏览器检查目标元素、布局和接口返回。涉及打包时，必须列出 ZIP 条目并扫描，确认没有 `.env`、账号、token、邮箱池、日志、PID、缓存、备份或代理订阅。
