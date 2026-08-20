# 号池项目（webcodex_pool）

本项目把「ChatGPT 号池」暴露给 AI 客户端（ChatGPT / Grok 等），通过 [webcodex](https://github.com/yyjeqhc/webcodex) 运行时接入。

- 号池后端：注册机 WebUI（Flask），HTTP API 位于 `http://127.0.0.1:5000/api/pool/*`，用根目录 `.env` 的 `WEBUI_AUTH_CODE` 鉴权。
- 本目录提供命令行客户端 `pool_cli.py`，封装号池 API，供 AI 通过 webcodex 的 `commands_run` 直接调用。

> 安全红线：`access_token` 是敏感凭证。不要在对话/MCP 日志里明文打印完整 token。需要完整值时用 `--raw`，直接由脚本使用/保存，用完即弃。

---

## 一、号池是什么

号池是一批可用的 ChatGPT 账号（含 access_token、套餐、额度），由后端统一管理。AI 干活的典型流程：

1. `summary` 看池状态 → 2. `acquire --raw` 拿一个账号 → 3. 用 token 干活 → 4. 账号 401 / 额度耗尽时 `switch` 切号。

## 二、脚本用法（pool_cli.py）

所有命令都会自动读取项目根 `.env` 的 `WEBUI_AUTH_CODE` 鉴权。WebUI 地址默认 `http://127.0.0.1:5000`，可用环境变量 `POOL_API_BASE` 覆盖。

```powershell
# 查看号池统计（可用/耗尽/禁用/额度未知/不可用原因）
python pool_cli.py summary

# 列出池内账号（可筛选）
python pool_cli.py list
python pool_cli.py list --status available
python pool_cli.py list --status disabled

# 分配一个可用账号（脱敏打印，不显示完整 token）
python pool_cli.py acquire
python pool_cli.py acquire --email user@example.com
python pool_cli.py acquire --tags plus,us

# 分配账号并输出完整 JSON（含完整 access_token，供脚本直接使用）
python pool_cli.py acquire --raw

# 无感切号：标记旧账号额度耗尽并分配新账号
python pool_cli.py switch --email old@example.com --reason quota_exhausted
python pool_cli.py switch --reason auth_failed   # 不指定邮箱则自动选最近分配账号
python pool_cli.py switch --email old@example.com --reason quota_exhausted --raw

# 手动触发额度巡检（对过期账号入队查额度）
python pool_cli.py probe

# 禁用 / 重新启用池内账号
python pool_cli.py disable 12 --reason "滥用"
python pool_cli.py enable 12
```

示例输出：

```
== 号池统计 ==
  功能启用   : 是
  账号总数   : 25
  入池数     : 25
  可用       : 23
  ...
```

```
== 分配账号 ==
  account_id : 6
  email      : beastly_satrap_5e@icloud.com
  plan_type  : free
  user_id    : user-8MjEm0Q5chYl6DPOjHZcD3iI
  access_token: eyJhbGciOiJS…sPhKY0Dw  (完整值用 --raw 获取，请勿在对话中明文打印)
```

---

## 三、webcodex 部署状态

| 项 | 值 |
|---|---|
| webcodex 版本 | 0.3.7（npm 全局安装 `@yyjeqhc/webcodex`） |
| 部署模式 | 本机 `webcodex setup` + `webcodex run`（本地 Server + Runner） |
| 项目根 | `E:\GPT注册\turb-gpt-free-register\webcodex_pool`（独立 git 仓库） |
| MCP 地址 | `http://127.0.0.1:34376/mcp` |
| OpenAPI（GPT Actions） | `http://127.0.0.1:34376/openapi.json` |
| Console | `http://127.0.0.1:34376/console` |
| Runtime status | `http://127.0.0.1:34376/api/runtime/status` |
| 凭证 | Connector key，位于 `%LOCALAPPDATA%\.local\state\webcodex\projects\personal\webcodex_pool-*` 目录下 `credentials\connector-key`（`webcodex_` 开头）；用 Bearer 发送。 |
| Agent | 本机 Agent 在线（WebSocket 连接，`wc_agent_` 前缀，仅供 Agent 传输用，不能用于项目/运行时 API） |

### 运维

- **启动**：`webcodex run --root "E:\GPT注册\turb-gpt-free-register\webcodex_pool"`（前台运行，Ctrl-C 停止）。
- **状态**：`webcodex doctor --root "E:\GPT注册\turb-gpt-free-register\webcodex_pool"`。
- **日志**：`%LOCALAPPDATA%\.local\state\webcodex\projects\personal\webcodex_pool-*\logs\server.log`（agent.log 为 Runner 日志）。
- **停止**：终止前台 `webcodex run` 进程；或 `webcodex agent stop --config <agent.toml>`。
- **注册新项目**：`webcodex setup --root <项目目录>`（项目需是 git 仓库且有初始提交）。
- 本机模式仅监听 `127.0.0.1`，只能本机访问；若需远程（ChatGPT/Grok 云端）连接，见下方「四」的远程说明或改用 `webcodex share`。

---

## 四、用户在 ChatGPT / Grok 聊天窗口连接 webcodex

webcodex 同时提供 MCP 与 GPT Actions 两种接入，任选其一。

### 方式 A：MCP（推荐，能力最全）

1. **本机浏览器打开 MCP 控制台**：`http://127.0.0.1:34376/console`（确认运行时在线）。
2. 在 ChatGPT / Grok 的 **Connectors / 自定义 MCP** 设置中：
   - MCP 服务器地址填：`http://127.0.0.1:34376/mcp`
   - 认证方式选 Bearer，凭证填 Connector key（`webcodex_` 开头，见运维节；只读本地文件）。
3. 连接成功后，AI 客户端会看到 webcodex 提供的工具（`commands_run` / `files_*` / `edits_apply` 等 14 个），即可在聊天窗口直接驱动本机号池项目。

> 注意：本机 `webcodex run` 只监听回环地址，**ChatGPT/Grok 的云端服务器无法直连 127.0.0.1**。若要远程接入，用 `webcodex share`（Cloudflare Quick Tunnel 临时 HTTPS），或自建托管 Server。

### 方式 B：GPT Actions（OpenAPI）

1. 打开 `http://127.0.0.1:34376/openapi.json` 保存为本地 JSON 文件。
2. 在 ChatGPT 的 Actions 设置里导入该 JSON，Authorization 选 Bearer 并填 Connector key。
3. 远程连接同样受本机监听限制，适合局域网/本机客户端测试。

---

## 五、AI 如何调用号池干活（提示词示例）

给 AI 的提示词示例：

```
查看号池状态（运行 python pool_cli.py summary）；
然后用一个可用账号做 XX 任务：
  1. python pool_cli.py acquire --raw 拿到完整 access_token，保存到本机文件，不要打印出来；
  2. 用该账号调用 ChatGPT 完成 XX；
  3. 若账号报 401 或额度不足，运行 python pool_cli.py switch --email <旧邮箱> --reason quota_exhausted 切号并重试；
  4. 结束时确认池状态正常。
```

AI 侧的实操要点（建议写进项目级规则）：

- 永远先 `python pool_cli.py summary` 看池状态再干活。
- 拿账号用 `python pool_cli.py acquire --raw`，把完整 JSON 写入临时文件，**不要在对话里回显 access_token**。
- 账号不可用（401 / quota_limit_reached）时：`python pool_cli.py switch --email <旧邮箱> --reason <原因>` 自动切到下一个可用账号。
- 切号会把旧账号标记为「耗尽」，属预期行为；如需保留该账号可 `python pool_cli.py enable <id>` 恢复。

---

## 六、安全提醒

- `access_token` 是 ChatGPT 账号的有效凭证，等同于密码：**MCP/对话中勿明文打印**，用完即弃。
- Connector key（`webcodex_` 前缀）能完全驱动本机项目，等同本机权限：不要外传、不要贴进公共对话，服务器日志里也会被 webcodex 自行脱敏。
- 号池 API 凭 `WEBUI_AUTH_CODE` 保护，仅监听 127.0.0.1；不要把它暴露到公网。
- 本目录是独立 git 仓库（webcodex 运行要求），初始提交仅包含 `pool_cli.py`、`README.md`、`.gitignore`，不含任何 token。
