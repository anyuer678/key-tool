# key-tool —— API Key 生成器（多租户网关）

> **ARCHIVED** · 本仓已归档，不再维护。  
> 个人 API 密钥管理请使用 **[keyvault](https://github.com/anyuer678/keyvault)**。  
> **安全警告**：历史版本在未设置 `KEYTOOL_ADMIN_TOKEN` 时管理接口可能放行；未设置 `KEYTOOL_SECRET_KEY` 时服务商密钥可能明文落库。**禁止**将归档版本部署到任何可访问网络。  
> 状态标签：`archived` · 仅作作品集/历史参考。

> 多维度限额密钥签发 / 消费服务：项目 / 共享额度池 / 余额计费 / 模型绑定 / 限流，转发任意 OpenAI 兼容上游（DeepSeek / OpenAI / Ollama / Kimi / 智谱 / Qwen…），支持 OpenAI SDK 直连与流式。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-65%20passed-brightgreen)](tests/)
[![API](https://img.shields.io/badge/API-OpenAI%20compatible-4B8BBE)](https://github.com/anyuer678)

给自用 / 小团队签发带限额的 API Key：**次数、token、有效期、余额**四个维度可组合，任一触顶即失效；`calls=1` 即一次性 key。签发的 key 可直接给 OpenAI SDK / 任意客户端用，消费自动按实际用量结算退差。

## 功能特性

| 能力 | 说明 |
|---|---|
| 项目体系 | key 归属项目；项目级**共享额度池** + key 独立上限，同事务双层扣减，任一触顶整体回滚 |
| 余额计费 | 项目余额 + 定价表（元/1M tokens），预扣防超支、按实际 usage 结算退差、5xx 全额退 |
| OpenAI 兼容 | `/v1/chat/completions`：Bearer 鉴权 + messages 透传 + 标准响应/SSE，OpenAI SDK 直连 |
| 预设服务商 | 首次启动内置 DeepSeek / OpenAI / Ollama / Kimi / 智谱 / Qwen 六家，界面填 api_key 即用（支持 `${ENV}` 引用） |
| 模型绑定 | 签发可绑 provider/model（预设模型下拉），冲突 403 |
| 扣减策略 | **预扣 + 退差**（token 与金额平行）；并发安全由原子条件 UPDATE + 幂等抢占保证 |
| 安全 | `KEYTOOL_ADMIN_TOKEN` + HttpOnly 签名会话 cookie；api_key Fernet 加密；登录/签发/消费分层限流 |
| 审计报表 | 全量 `usage_logs`（含金额）；`/stats/usage` 按天/项目聚合 |
| 三种界面 | Web 面板（tab + 模板 + 批量）/ OpenAI SDK / CLI |

## 快速开始

```powershell
pip install -r requirements.txt
# 可选：管理鉴权 + 服务商密钥加密（推荐生产）
$env:KEYTOOL_ADMIN_TOKEN = "你的管理密码"
$env:KEYTOOL_SECRET_KEY = (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uvicorn app.main:app --reload --port 8000     # 或 ./start.ps1 / docker compose up -d
```

打开 http://127.0.0.1:8000/ 使用 Web 面板。

> **服务商开箱即用**：首次启动自动内置六家预设，只需在「服务商」页给要用的**填 api_key**（支持 `${ENV_VAR}` 引用——设置了 `DEEPSEEK_API_KEY` 等环境变量就免填）。

### 三种使用方式

**① Web 面板**：签发（含模板）/ Key 管理（日志、重签、批量吊销）/ 项目 / 服务商 / 定价 / 报表。

**② OpenAI SDK 直连**（任意语言/客户端）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-你签发的key")
r = client.chat.completions.create(model="deepseek-chat", messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

**③ CLI**：

```powershell
python keytool.py issue --calls 100 --tokens 1000000 --project proj1   # 签发
python keytool.py chat sk-xxx "你好" --stream                          # 对话
python keytool.py list / status <id> / revoke <id> / rotate <id>       # 管理
python keytool.py project --name demo --balance 10                     # 建项目
```

## 接口速查

管理接口受 `KEYTOOL_ADMIN_TOKEN` 保护（页面登录走 HttpOnly cookie；脚本用 `X-Admin-Token` / `Bearer`）；消费接口用 `Authorization: Bearer <key>` 或 `X-Api-Key`。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/admin/login` `/logout` · `GET /admin/me` | 公开 | 管理会话（12h，登录限流） |
| GET | `/providers` | 公开 | 服务商列表（预设模型 + needs_key） |
| POST | `/providers` · `/providers/{id}/key` · `/providers/presets` | 管理 | 服务商 CRUD / 填 key / 一键预设 |
| POST | `/projects` · `/projects/{id}/recharge` | 管理 | 项目创建 / 充值（次数/token/余额） |
| GET/POST | `/pricing` | 管理 | 定价表（元/1M tokens） |
| POST | `/keys` · `GET /keys` | 管理 | 签发（多维度限额） / 列表 |
| GET | `/keys/{id}` · `/keys/{id}/logs` | 公开/管理 | 查余量 / 审计日志 |
| POST | `/keys/{id}/revoke` `/rotate` `/recharge` | 管理 | 吊销 / 重签（保留剩余额度与有效期）/ 充值 |
| GET | `/stats/usage` | 管理 | 用量报表（按天/项目聚合） |
| POST | `/v1/chat/completions` | `Bearer <key>` | **OpenAI 兼容**消费（含流式） |
| POST | `/v1/chat` | `X-Api-Key` | 简化消费（prompt/max_tokens） |

## 环境变量

| 变量 | 用途 |
|---|---|
| `KEYTOOL_ADMIN_TOKEN` | 管理接口鉴权（未设则本机免鉴权） |
| `KEYTOOL_SECRET_KEY` | Fernet key，服务商 api_key 加密（未设则明文+告警） |
| `KEYTOOL_FORCE_SECURE_COOKIE` | 置 1 时会话 cookie 强制 `Secure` 标志 |
| `REDIS_URL` | 多实例共享限流（不设则进程内限流） |
| `KEYTOOL_URL` / `KEYTOOL_ADMIN_TOKEN` | CLI 的默认服务地址 / 管理令牌 |

## 安全模型与边界

- 密钥只存 SHA-256 哈希；明文 key 仅在签发响应出现一次；api_key 用 Fernet 加密存储（支持 `${ENV}` 引用不落盘）。
- 消费**预扣 + 退差**：按 `max_tokens` 预扣（token + 金额），成功按 usage 结算；上游 4xx/5xx/网络错误 → 502 且全额退款；500 未捕获异常自动退预扣。
- 结算时实际用量超预扣量（上游不守 max_tokens）→ 按预扣封顶并记 `warn` 审计，余额不为负。
- 限流：消费默认 20 rps/key（签发可配 `rps_limit`）、管理登录 5 rps/IP、签发 10 rps/IP；`X-Forwarded-For` 仅在受信反代后启用。
- SQLite 单写者模型（WAL），并发写串行；上游转发用共享 HTTP 客户端，单 worker 实测约 70 req/s，远超本机工具所需。
- **本项目仅供学习交流与演示用途**，不构成任何形式的商业服务或技术承诺。软件按「现状」提供，不作任何明示或暗示的保证。将本项目部署于生产、对外提供服务或接入真实业务，均属使用者自主决策；由此产生的服务中断、数据损坏或泄露、业务损失、合规风险及第三方纠纷，**开发者均不承担任何责任**。

## 项目结构

```
app/
  main.py            路由：签发/管理/项目/定价/报表/消费（自定义 + OpenAI 兼容）
  db.py              SQLite schema（projects/api_keys/usage_logs/pricing）+ 迁移
  keys.py            签发/吊销/充值/重签/列表/日志 + 项目 CRUD
  quota.py           双层原子扣减：预扣/退差/懒重置/余额计费/审计
  middleware.py      key 校验 + 按 key 限流（内存/Redis 自动切换）
  security.py        管理令牌 + HttpOnly 签名会话 cookie + 登录/签发限流
  providers.py / provider_store.py   服务商注册表（DB + Fernet 加密 + 内置预设）
  redis_limiter.py   Redis 滑动窗口限流（多实例共享）
  stats.py           用量聚合统计
  static/            Web 面板（tab + 模板 + 批量）
keytool.py           CLI
tests/               65 个测试（含真实 uvicorn 集成）
Dockerfile / docker-compose.yml
```

## 测试

```powershell
python -m pytest tests -q
```

## License

[GPL-3.0](LICENSE) — Copyright (C) 2026 anyuer678
