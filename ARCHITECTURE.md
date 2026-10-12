# PayPilot 架构复审（v4，v5 修订）

> 背景：v2/v3 两轮"单路由打补丁"后，Codex 复验仍在组合路径上发现绕过
> （v3 报告 P1-1/P1-2/P2）。本轮不再给单个路由加条件，而是先定架构、
> 再按架构实现。本文是 v4 的唯一事实来源；实现与测试都必须能回指到
> 下面的每一条。
>
> v5 修订（Codex v4 复验）：§3 的"原子"说明在 v4 是**错误**的——
> Python 3.12 的 sqlite3 默认 autocommit，`with connection:` 不会把
> SELECT→判定→UPDATE 包进同一个事务（SELECT 后 `in_transaction` 为
> false），单进程两线程即可复现降级。本节按真实机制重写。
> §5 重写：服务端操作键是**唯一**权威，模型工具键彻底移除。

## 1. 可信到账证据：什么才算"钱到了"

**只有同时满足以下三条，才算到账（paid）：**

1. 远端订单 JSON 中 `purchase_units[0].payments.captures[0].status == "COMPLETED"`；
2. 该 capture 的金额（分）**等于**本地订单的 `amount_cents`；
3. 该 capture 的币种**等于**本地订单的 `currency`。

**明确不算到账的：**

- 订单顶层 `status == "COMPLETED"` ——它只说明订单生命周期走完，
  不证明钱已划转（子 capture 可能是 PENDING / 金额不对 / 币种不对 /
  根本没有 capture 对象）。v2/v3 的核心教训：**顶层状态字符串永远
  不能作为到账证据**。
- HTTP 200 本身 ——`/capture` 对 PENDING/失败的 capture 同样返回 200。

实现位置：`paypal_client.PayPalClient.parse_capture_result()`（纯函数，
无网络）；语义封装：`payments.verify_payment()`（见 §2）。

## 2. 四条写入路径共用同一个到账验证函数

任何能把"外部支付世界"写进本地订单状态的入口，必须经过同一个函数，
**不允许各路径自己定义"什么算付过"**。四条路径：

| # | 入口 | 触发时机 |
|---|---|---|
| 1 | `GET /return` | 买家在 PayPal 批准后被重定向回来 |
| 2 | `GET /api/order/<id>/status` | 买家聊天轮询 |
| 3 | `Agent.tool_check_order` | 聊天里问"我的订单怎么样了" |
| 4 | `POST /webhook/paypal` | PayPal 事件（先经 PayPal API 核实真状态） |

共享函数（`payments.py`）：

```python
verify_payment(remote_order_json, expected_cents, expected_currency)
    -> (local_status, paid: bool, reason: str)
```

- 内部调用 `parse_capture_result` 做三条到账判定；
- 到账 → `("CAPTURED", True, "")`；
- capture 为 PENDING → `("APPROVED", False, reason)`（买家已批准，
  PayPal 处理中；即使订单层状态已是 COMPLETED 也一样——缺的是钱的证据）；
- 未到账但订单层 `APPROVED` → `("APPROVED", False, reason)`；
- 未到账且 `VOIDED` → `("CANCELLED", False, reason)`；
- 其他未到账 → `("CREATED", False, reason)`；
- 四条路径拿到 `(local_status, …)` 后，**唯一**允许的写入是
  `Store.apply_payment_verdict(order_id, local_status)`（见 §3）。

webhook 的额外规则：事件体本身不可信。只处理
`PAYMENT.CAPTURE.COMPLETED` 事件，从 `resource.links[rel=up]` 提取订单 id，
再用 `get_order` 拉全量订单 JSON，走同样的 `verify_payment`。
伪造事件永远翻不动状态（已有测试锁定）。

## 3. 合法状态转换表 + 终态保护

本地订单状态机（`store.py`）：

```
CREATED ──► APPROVED ──► CAPTURED   (终态)
   │            │
   │            ▼
   └─────► CANCELLED   (终态)
   └─────► CAPTURED    (直接到账也允许)
```

合法转换表：

```python
TRANSITIONS = {
    "CREATED":   {"APPROVED", "CAPTURED", "CANCELLED"},
    "APPROVED":  {"CAPTURED", "CANCELLED"},
    "CAPTURED":  set(),   # 终态：钱已到，永不离开
    "CANCELLED": set(),   # 终态
}
```

规则：

- 所有支付驱动的写入走 `Store.apply_payment_verdict(order_id, new_status)`。
- `new == old` → 空操作；`new ∈ TRANSITIONS[old]` → 写入
  （`CAPTURED` 首次写入时记录 `captured_ts`，之后不再覆盖）；
  其他一律**原子拒绝**并记日志（返回 `(old, old, reason)`）。
- "原子"指两层，缺一不可（v5，Codex v4 复验后的真实机制）：
  1. **写串行化**：`apply_payment_verdict` 在 SELECT 之前先执行
     `BEGIN IMMEDIATE`，拿到 RESERVED 锁。所有走共享写入入口的
     writer 被串行化——后来者阻塞到前者提交，不可能插空。
     （为什么需要显式 BEGIN：Python 3.12 的 sqlite3 默认
     `isolation_level=""` 即 autocommit；`with connection:` 只负责
     commit/rollback，**不会**把 SELECT→判定→UPDATE 包进一个事务。
     Codex v4 用 `in_transaction == False` 实测证明了 v4 文档的说法
     是错的。）
  2. **比较交换（CAS）**：UPDATE 带条件
     `WHERE paypal_order_id=? AND status=<SELECT 读到的旧值>`。
     若有不走共享入口的 writer（遗留直写、未来代码、手写 SQL）在
     SELECT 与 UPDATE 之间改了行，`rowcount == 0` → 立即重读、
     按转换表重新判定：迟到的旧请求一律拒绝，终态永不降级。
  多 worker 部署需要真正的分布式 CAS——本项目明确为单进程 demo，
  不假装解决了分布式问题（见 README）。
- 典型被拒绝的场景：本地已 `CAPTURED`、收入已计，
  迟到的 `APPROVED` 查询到达 → 拒绝，收入纹丝不动。
  v3 的"收入 53.95 → 0"正是缺了这一层。

`Store.stats()` 的收入口径：**只认 `is_paid(status)`**
（`status == "CAPTURED"`）。状态字符串统一只是表象，
到账证据（§1）+ 终态保护（本节）才是实质。

## 4. public buyer / private merchant 边界

### 4.1 公共买家页面永不下发商户凭据

- `GET /`（买家聊天页）的 HTML 里**不得出现任何商户 token**
  （v3 把 `window.MERCHANT_TOKEN` 直接塞进首页，是本次重构要删掉
  的根本错误：鉴权检查本身是对的，但把开门的钥匙发给了每个访客）。
- 商户仪表盘搬到独立路由 `GET /merchant`（`merchant.html` +
  `static/merchant.js`），与买家入口物理区分。
- `/merchant` 页面只放一个**token 输入框**：操作员从服务端控制台
  复制 `MERCHANT_API_TOKEN` 粘贴进去；JS 把它存在**内存变量**里，
  所有 `/api/merchant/*` 请求带 `Authorization: Bearer <token>`；
  **token 永不写入 HTML、永不进 localStorage/cookie**。
- token 缺失/错误 → API 返回 401，页面显示"token 无效"，
  不泄露任何订单数据。
- `MERCHANT_API_TOKEN` 未设置时，启动自动生成随机值并打印到控制台
  （demo 级保护：防随手爬虫，不冒充生产鉴权；README 如实说明）。

### 4.2 买家订单状态接口的访问能力决策（明确写下来）

`GET /api/order/<id>/status` 的设计决策：

1. **订单 id 本身就是访问能力（capability）**。
   PayPal 订单 id 是高熵不可猜的；拿到 id 的人被视为持有"签过名的链接"，
   这是刻意的设计（买家在多个设备/标签页查单不需要先登录）。
2. **会话边界是第二层**：接口接受可选 `session_id` 参数。
   一旦提供，就必须与订单的创建会话一致，否则 403（不返回任何数据）。
   第一方聊天 JS 永远带上自己的 `session_id`。
   不提供 `session_id` 的裸 id 访问 = 能力持有模型（by design）。
3. **approval_url 只属于买家会话**：跨会话的裸 id 查询返回状态，
   这是能力模型的含义；商户 token 的 401 检查**不是**这条接口的保护，
   两者不要混为一谈（Codex v3 报告"其他范围"段的要求）。

生产部署建议（README）：买家登录体系 + 服务端会话绑定；
本 demo 不实现，不宣称。

## 5. 真实请求重试标识：服务端操作键是唯一权威（v5 重写）

用户要能表达"这次是**同一操作**的重试"，而不是"一次新的购买"。
v4 的错误：`_execute` 里"LLM 工具参数里的同名字段优先"——模型每次
调用都可能给出不同的 key，同一 HTTP 重试建出两张订单
（Codex v4 复验 P1-2）。v5 的规则：

**服务端认可的操作键是唯一权威，整轮固定；模型永远接触不到它。**

```
浏览器/HTTP 客户端
  │  POST /api/chat {…, "idempotency_key": "<uuid>"}   (可选)
  ▼
Agent.run_turn(session_id, message, idempotency_key=<server_key>)
  │  本轮固定；LLM 工具 schema 里**没有** idempotency_key 参数
  ▼
Agent._execute → key = _server_call_key(server_key, items)
  │  纯服务端推导：sha256(规范化 items) 取 12 位 → f"{server_key}#{digest}"
  │  同 key + 同 items → 同 PayPal-Request-Id（真重试）
  │  同 key + 不同 items → 不同 key（一轮内第二次购买）
  │  模型在 args 里塞的 idempotency_key 一律忽略
  ▼
tool_create_paypal_order(…, idempotency_key=key)
  │  无 server_key → 为本次购买意图生成全新 uuid（新购买）
  ▼
PayPalClient.create_order(…, idempotency_key=key)
   → HTTP 头 PayPal-Request-Id: <key>
```

- 新购买：key 缺省 → 每次意图全新 uuid，同会话买两次同商品
  生成两个订单（v2 的 UNIQUE 冲突根因在此终结）。
- 重试：调用方复用**同一个** HTTP key（网络超时后点"重试"）→
  同 items 推导出同 key → PayPal 返回同一订单 id →
  本地 `UNIQUE(paypal_order_id)` 冲突被吸收，返回已存订单，
  不报错、不建重复单。模型在同一轮里"换个 key"的把戏不再可能，
  因为它根本看不到这个参数。
- 一轮内两次**不同**购买：items 不同 → 派生 key 不同 → 两个独立订单。
  这是"同一操作内工具序号/子操作的稳定映射"（Codex v4 报告原文要求）。
- 前端 `send()` 每次消息生成一个 key；fetch 网络失败时提供
  "重试"按钮，**复用同一个 key**重发——这才是用户可感知的重试语义。
- 测试必须从**外部调用路径**进入（HTTP POST /api/chat 带 key 调两次，
  且第二次让模型在工具参数里塞一个不同的 hallucinated key），
  断言：只建一张订单，发给 PayPalClient 的 PayPal-Request-Id 稳定不变。

## 6. 测试策略：组合测试是及格线

Codex 明确要求：只测单路由不算过。v4 的组合矩阵（`tests/test_all.py`
`PaymentCombinationTest`）：

- **路径 × 变体**：4 条路径（/return、轮询、Agent 查单、webhook）
  × 5 种远端变体（子 capture PENDING / 只 capture $1.00 / EUR 53.95 /
  缺 capture 对象 / 顶层 COMPLETED 无 capture）= 20 格，
  每格断言：**本地 ≠ CAPTURED，收入 = 0**。
- **序列测试**：return(正确) → 继续轮询 → 仍 CAPTURED；
  return(正确) → 聊天查询 → 仍 CAPTURED；
  已到账后 webhook 到达 → 仍 CAPTURED；
  return(pending) → 远端转正后轮询 → 转为 CAPTURED（恢复路径不断）；
  迟到 APPROVED（本地已 CAPTURED）→ 原子拒绝，收入不变。
- dotenv 隔离：**独立子进程**复现报告场景（cwd 里躺着带虚拟凭据的
  `.env`，单跑无凭据 webhook 测试必须过）——不依赖导入顺序。
- 全部测试禁止外连（FakePayPal / stub），无真实 PayPal/LLM 调用。

## 7. 不做什么（诚实边界）

- 单进程 SQLite 事务 ≠ 分布式锁；多 worker/公网生产部署需要真正的
  CAS 与买家登录体系，本文不假装覆盖。
- Node XSS 套件是最小 DOM 环境测试，不等于完整浏览器渗透测试。
- 浏览器组合路径的人工复验仍由 Codex 按其流程执行；本仓库只保证
  自动化组合矩阵全绿。
