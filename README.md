# 快递理赔时限判定服务

按**订单创建时生效的政策版本**判定快递理赔申请资格的 HTTP 服务。输入订单、承诺时限、时区、节假日表与按时间到达的扫描事件，输出：

- 当前是否可申请（`eligible_to_file` / `overall_status`）；
- 各理赔类型的**截止时间**（当地时间 + UTC）；
- **缺失证据**清单；
- 逐条可审计的**判定依据**（含适用政策版本、起算事件、跳节假日说明、更正记录）；
- **理赔申请受理**：按服务实际接收时间实时核验并出具不可变申请凭证（幂等受理、同类型唯一、可查询）。
- **节假日表工作日诊断**：提交日历/时区/带偏移起始时刻/工作日数/截止钟点，只读逐日解释第 N 个工作日如何得出（含放假跳过、周末补班计入），返回当地与 UTC 截止时刻。

## 判定规则

### 政策版本锁定

- 政策以 `valid_from / valid_to`（UTC，半开区间）入库；
- 下单时选取 `valid_from <= 订单创建时间 < valid_to` 的最新版本，版本号随订单持久化；
- 之后发布新政不影响既有订单（切换政策版本无需改代码，只更新数据）。

内置两个演示版本（见 `app/seed_data.py`）：

| 版本 | 生效区间（UTC） | 延迟送达 | 货物破损 | 货物丢失 |
|---|---|---|---|---|
| v1 | 2023-01-01 ～ 2025-01-01 | 7 个工作日 | 5 个自然日 | 30 个自然日 |
| v2 | 2025-01-01 起 | 10 个工作日 | 3 个工作日 | 15 个工作日 |

### 工作日与节假日

- 截止日按订单时区计算：`触发日次日起算 N 天，截止落在第 N 天的 23:59:59（当地时间）`；
- `business`（工作日）口径跳过周六日**和**节假日表中的 `holiday`；
- 节假日表中的 `workday`（调休补班日，即使是周末）**计为工作日**；
- 内置中国国务院办公厅 2024、2025 年放假/调休安排（72 个特殊日期）。

例：2025-02-11（周二）签收迟到，10 个工作日的截止为 **2025-02-25 23:59:59（Asia/Shanghai）**——其间春节假期（1/28–2/4 在起算前、无影响；本次推算仅跳过普通周末）。2025-10-01 起算则跳过国庆+中秋黄金周，截止 **2025-10-21**。

### 节假日表工作日诊断（只读）

`POST /api/v1/calendars/diagnose` 不依赖任何订单/政策，直接对**现有节假日表**做一次可审计的逐日推算，便于核对"第 N 个工作日截止"到底是怎么数出来的：

- 入参：`calendar_code`（日历代码）、`timezone`（IANA 时区）、`start_at`（**带偏移**的起始时刻）、`business_days`（**1～60**）、`cutoff`（当地截止钟点 `HH:MM` 或 `HH:MM:SS`）；
- 起始时刻先换算到 `timezone` 当地日期，**起始日当天不计入，自次日起逐日列出**，直到第 N 个工作日；
- 逐日条目包含：日期与星期、`工作/休息` 判定、日历的补班（`workday`）/放假（`holiday`）声明（无声明为 `null`）、周末默认规则、**累计工作日数**、是否跳过及跳过原因；
- **补班落在周末也计入**（如 2025-10-11 周六国庆调休上班），**放假落在工作日也跳过**（如 2025-10-01～10-08）；无声明的周六日按默认规则跳过；
- 返回第 N 个工作日在 `cutoff` 钟点的 `deadline_local` 与 `deadline_utc`，以及计数/跳过汇总。
- 全程**只读**：不修改日历、政策、订单或凭证（响应带 `read_only: true` 与说明）。

```bash
curl -s -X POST http://localhost:8000/api/v1/calendars/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "calendar_code": "CN",
    "timezone": "Asia/Shanghai",
    "start_at": "2025-09-30T10:00:00+08:00",
    "business_days": 10,
    "cutoff": "18:00"
  }'
```

响应节选（10/1–10/8 黄金周跳过，10/11 周六补班计为第 3 个工作日，截止 10/21 18:00 当地 / 10:00 UTC）：

```json
{
  "calendar_code": "CN",
  "timezone": "Asia/Shanghai",
  "start_at_local": "2025-09-30T10:00:00+08:00",
  "start_at_utc": "2025-09-30T02:00:00+00:00",
  "business_days_requested": 10,
  "cutoff_local": "18:00:00",
  "count_basis": "起始日当天不计入，自次日起逐日计数，截止落在第 N 个工作日",
  "count_start_date": "2025-10-01",
  "deadline_date": "2025-10-21",
  "deadline_local": "2025-10-21T18:00:00+08:00",
  "deadline_utc": "2025-10-21T10:00:00+00:00",
  "summary": {"business_days_counted": 10, "rest_days_seen": 11,
              "declared_holidays_seen": 8, "declared_makeup_workdays_seen": 1,
              "days_listed": 21},
  "entries": [
    {"date": "2025-10-01", "weekday": "周三", "verdict": "休息",
     "is_business_day": false,
     "declaration": {"kind": "holiday", "name": "国庆节、中秋节"},
     "weekend_default_rule": "默认规则：周一至周五为工作日",
     "business_day_count": 0, "skipped": true,
     "reason": "日历声明放假（国庆节、中秋节），跳过"},
    {"date": "2025-10-11", "weekday": "周六", "verdict": "工作",
     "is_business_day": true,
     "declaration": {"kind": "workday", "name": "国庆调休上班"},
     "weekend_default_rule": "默认规则：周六、周日为休息日",
     "business_day_count": 3, "skipped": false,
     "reason": "日历声明补班上班（国庆调休上班），虽落在周末也计为工作日，计入"}
  ],
  "read_only": true,
  "note": "只读诊断：未修改日历、政策、订单或凭证"
}
```

明确拒绝（均为统一错误体）：

| 情形 | 状态码 | 错误码 |
|---|---|---|
| `start_at` 无时区偏移（naive）/ 缺必填字段 | 422 | `INVALID_REQUEST` |
| `business_days` 不在 1～60 | 422 | `INVALID_REQUEST` |
| `cutoff` 非 `HH:MM(:SS)` 或越界（如 `24:00`、`18:60`） | 422 | `INVALID_REQUEST` |
| `timezone` 不是合法 IANA 时区 | 422 | `TIMEZONE_INVALID` |
| `calendar_code` 日历不存在 | 404 | `CALENDAR_NOT_FOUND` |

### 理赔类型与状态

三类理赔独立判定：

- `delay` 延迟送达：签收晚于承诺时限、或承诺时限已过仍无签收 → 构成；未到承诺时限无签收 → `pending`；准时签收 → `not_applicable`；订单未给承诺时限 → `indeterminate`；
- `damage` 货物破损：出现 `damage_reported`（破损申报）扫描即触发；
- `loss` 货物丢失：出现 `lost_confirmed`（丢件确认）扫描即触发。

每个类型状态：`filable`（可申请）/ `pending`（等待触发事件）/ `not_filable`（已过截止）/ `not_applicable`（不成立）/ `indeterminate`（证据不足不可判定）。顶层 `overall_status`：任一可申请即 `filable`，否则有 `pending/indeterminate` 即 `pending`，全部 `not_applicable` 为 `closed`。

### 事件流规则（重点）

1. **按到达时间顺序入库**：以 `arrived_at`（服务接收时间）为准，必须相对已接受流单调不减。倒序提交返回 `422 EVENT_ORDER_INVALID` 并标明出错下标，**整批回滚**。
2. **重放不重复计入**：`event_id` 在同一订单内唯一；重复提交进入 `duplicates`，不插入、不参与判定。
3. **时间字段必须带时区偏移**（如 `2025-02-11T10:00:00+08:00`），字段缺失/时间 naive 一律 `422 INVALID_REQUEST`。
4. **迟到更正不改写归档**：判定可 `POST .../archive` 归档（生成不可变 JSON 快照）。归档后到达的任何事件（含更正）会**开启新一代（generation）判定**，旧代快照原样保留；新结果通过 `supersedes_generation` 指向旧代。
5. **更正事件三种动作**（`is_correction` 语义由 `correction_action` 表达）：
   - `amend`：修正目标基础事件的发生时间（须带 `target_event_id`，类型须一致）；
   - `cancel`：撤销目标事件（如丢件确认被推翻）；
   - `insert`：补录一条此前漏掉的扫描（允许类型：signed / damage_reported / lost_confirmed）。
   更正只影响"物化"后的判定视图，原始事件行永不修改或删除。
6. **无写入预演**：`POST .../events/preview` 在当前最新代事件视图上按同一套规则模拟整批处理，返回逐项校验结果、首个失败位置、模拟后的各理赔状态/截止时间/缺失证据与判定依据差异；任一事件违反约束即整批预演失败（错误码与正式摄取一致），全程不写事件、不开新代、不改归档快照。已归档订单的预演明确按**新一代**（`gen_no+1`）结果计算。

### 理赔申请与凭证（本轮新增）

客户按**订单 + 理赔类型**提交申请（`POST /api/v1/orders/{id}/claims`），核验与受理规则：

1. **以服务实际接收时间核验**：以请求中的 `received_at`（带时区偏移；缺省取服务当前 UTC）为判定基准，核验该订单**当前代**的有效扫描事件、下单时锁定的政策版本与当地截止时刻。接收时刻**之后**到达的事件/更正不构成本次依据。
2. **已归档也实时重算**：即使当前代已归档，也按接收时间重新核验，**不把较早归档快照当成实时资格**；快照后的迟到事件若已开启新代，按新代实时结果判定。受理凭证会标注 `archived_generation_rechecked` 并在依据中说明。
3. **只有 `filable` 才受理**：受理返回 201 与不可变凭证——申请编号 `claim_no`、接收/受理时间、判定代号 `decision_code=FILABLE`、适用政策（版本+窗口快照）、锚点/截止时间（当地+UTC）、该类型当时判定依据与冻结的整单判定。其他状态**不生成凭证**，返回可区分的业务拒绝：

   | 接收时状态 | 错误码 | 含义 |
   |---|---|---|
   | `pending` | `CLAIM_PENDING` (422) | 触发事件尚未出现 |
   | `not_filable` | `CLAIM_DEADLINE_PASSED` (422) | 已过当地截止时刻 |
   | `not_applicable` | `CLAIM_NOT_APPLICABLE` (422) | 理赔事由不成立 |
   | `indeterminate` | `CLAIM_INDETERMINATE` (422) | 证据不足不可判定 |

   拒绝体 `error.details` 带 `status` / `decision_code`（状态大写）/ 截止时间 / 缺失证据 / 当时依据 / `voucher_generated=false`；拒绝**不消耗**幂等键，状态转为可申请后同键可再提交。
4. **幂等键**（必填，请求体 `idempotency_key` 或请求头 `Idempotency-Key`，两处不一致返回 422）：
   - 同订单**同键同类型**重试 => 返回**原凭证**（HTTP 200，`replayed=true`），不重新受理、不改变任何时间与依据；
   - 同订单**同键换类型** => 409 `CLAIM_CONFLICT`（details 指向既有凭证）；
   - 同订单**同类型至多一张凭证**：不同键再来 => 409 `CLAIM_ALREADY_ACCEPTED`；不同类型各可一张；
   - 并发放心重试：进程内写锁串行化 + 数据库 `UNIQUE(order_id, claim_type)`、`UNIQUE(order_id, idempotency_key)` 双保险，并发不同键也不会双重受理。
5. **凭证一经受理不可变**：之后的更正或新代只影响"当前判定"，凭证冻结的政策、截止时间与判定依据永不改变。`GET /api/v1/claims/{claim_no}` 可查凭证及其所依据的判定代号；`GET /api/v1/orders/{id}/claims` 列订单全部凭证。

## 目录结构

```
app/
  config.py        # 环境变量配置（数据目录、默认截止钟点）
  constants.py     # 事件/理赔类型与中文标签
  db.py            # SQLite 连接、建表（含不可变 claim_vouchers）、串行化写事务
  engine.py        # 纯函数核心：政策选取/工作日推算/节假日诊断/事件物化/判定
  errors.py        # 业务错误码
  schemas.py       # Pydantic 入参模型与时区/钟点校验
  seed_data.py     # 政策版本 + CN 2024/2025 节假日种子
  services.py      # 订单、事件摄取、归档/开代、判定装配、理赔申请受理/查询、日历诊断
  main.py          # FastAPI 路由
init_db.py         # 初始化入口（建表 + 种子，幂等）
examples/call_api.py  # 端到端调用示例（仅标准库，含理赔申请与日历诊断全链路）
tests/             # pytest 引擎单测 + API 端到端测试（89 个用例）
Dockerfile, docker-compose.yml
```

## 快速启动

### 方式一：Docker Compose（推荐，自动初始化）

```bash
docker compose up --build
# 服务：http://localhost:8000  交互文档：http://localhost:8000/docs
```

数据持久化在命名卷 `claim-data`（容器内 `/data`，SQLite 单文件）。

### 方式二：本地 Python（3.11+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python init_db.py                       # 建表 + 写入政策与节假日（幂等）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

重新初始化（清空重来）：停止服务后删除数据目录（默认 `./data`），再执行 `python init_db.py`。

### 运行调用示例

```bash
pip install -r requirements.txt          # 示例只用标准库，装不装均可
python examples/call_api.py              # BASE_URL 可改目标地址
```

### 运行测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## HTTP API

所有请求/响应均为 JSON。错误统一形如：

```json
{"error": {"code": "EVENT_ORDER_INVALID", "message": "...", "details": {...}}}
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/orders` | 创建订单（按创建时间锁定政策版本） |
| GET | `/api/v1/orders/{id}` | 查询订单 |
| POST | `/api/v1/orders/{id}/events` | 提交单个扫描事件 |
| POST | `/api/v1/orders/{id}/events/batch` | 批量提交（顺序错误整批回滚） |
| POST | `/api/v1/orders/{id}/events/preview` | 无写入预演：模拟整批摄取，返回校验结果与判定差异 |
| GET | `/api/v1/orders/{id}/events` | 查看已接受事件流 |
| GET | `/api/v1/orders/{id}/determination?as_of=...` | 当前判定（最新代已归档则返回快照） |
| POST | `/api/v1/orders/{id}/determination/archive?as_of=...` | 归档当前代判定 |
| GET | `/api/v1/orders/{id}/determinations` | 全部"代"的判定历史与快照 |
| POST | `/api/v1/orders/{id}/claims` | **提交理赔申请**：按接收时间实时核验，filable 则出不可变凭证（幂等） |
| GET | `/api/v1/orders/{id}/claims` | 该订单全部已受理凭证 |
| GET | `/api/v1/claims/{claim_no}` | 按申请编号查凭证及其判定代号/冻结依据 |
| GET | `/api/v1/policies` | 政策版本列表 |
| GET | `/api/v1/calendars/{code}` | 节假日表 |
| POST | `/api/v1/calendars/diagnose` | **节假日表工作日诊断**（只读）：逐日列出工作/休息判定、补班/放假声明、默认规则、累计工作日数与跳过原因，返回第 N 个工作日的当地/UTC 截止时刻 |

`as_of` 为可选判定基准时刻（带偏移的 ISO8601），缺省取服务当前 UTC 时间——便于回溯测试。

### curl 示例

**1) 创建订单**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "order_id": "O-1001",
    "created_at": "2025-02-05T09:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "calendar_code": "CN",
    "promise_by": "2025-02-10T18:00:00+08:00",
    "weight_kg": 2.5
  }'
```

**2) 提交签收扫描（晚于承诺时限）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-1",
    "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00"
  }'
```

**3) 查询判定**

```bash
curl -s "http://localhost:8000/api/v1/orders/O-1001/determination?as_of=2025-02-20T12:00:00%2B08:00"
```

响应节选：

```json
{
  "order_id": "O-1001",
  "policy_version": "v2",
  "overall_status": "filable",
  "eligible_to_file": true,
  "earliest_deadline_local": "2025-02-25T23:59:59+08:00",
  "missing_evidence": [],
  "claims": [
    {
      "claim_type": "delay",
      "status": "filable",
      "deadline_local": "2025-02-25T23:59:59+08:00",
      "missing_evidence": [],
      "basis": [
        "实际签收 2025-02-11 10:00:00 CST 晚于承诺时限 2025-02-10 18:00:00 CST，构成延迟送达",
        "时限 10 个工作日（已跳过当地节假日），截止 2025-02-25 23:59:59 CST",
        "当前时间在申请时限内，可申请"
      ]
    }
  ],
  "basis": ["适用政策版本：v2（订单于 2025-02-05T09:00:00+08:00 创建）", "..."]
}
```

**4) 归档，再提交迟到更正（产生新一代，旧结果不变）**

```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/O-1001/determination/archive?as_of=2025-02-20T12:00:00%2B08:00"

curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-CORR-1",
    "event_type": "signed",
    "event_time": "2025-02-09T09:00:00+08:00",
    "arrived_at": "2025-02-21T08:00:00+08:00",
    "correction_action": "amend",
    "target_event_id": "EV-1",
    "note": "网点补传准确签收时间"
  }'

curl -s http://localhost:8000/api/v1/orders/O-1001/determinations
```

**5) 无写入预演：正式提交前模拟更正事件的影响（不落库、不开新代）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "as_of": "2025-02-21T12:00:00+08:00",
    "events": [{
      "event_id": "EV-CORR-2",
      "event_type": "signed",
      "event_time": "2025-02-09T09:00:00+08:00",
      "arrived_at": "2025-02-21T08:00:00+08:00",
      "correction_action": "amend",
      "target_event_id": "EV-1"
    }]
  }'
```

响应节选（此时第 1 代已归档，预演明确按第 2 代计算；归档快照不变）：

```json
{
  "dry_run": true,
  "valid": true,
  "first_failure_index": null,
  "items": [{"index": 0, "event_id": "EV-CORR-2", "valid": true, "checks": [
    {"check": "shape", "passed": true}, {"check": "duplicate", "passed": true},
    {"check": "arrival_order", "passed": true}, {"check": "correction_target", "passed": true}
  ]}],
  "simulation": {
    "current_generation_archived": true,
    "would_reopen": true,
    "generation_no": 2,
    "supersedes_generation": 1,
    "determination": {"overall_status": "pending", "claims": [
      {"claim_type": "delay", "status": "not_applicable", "...": "..."}
    ]}
  },
  "diff": {
    "overall_status": {"before": "filable", "after": "pending", "changed": true},
    "claims": [{"claim_type": "delay", "status_before": "filable",
                "status_after": "not_applicable", "changed": true, "...": "..."}],
    "basis_added": ["本次判定已纳入迟到更正事件：", "..."],
    "basis_removed": []
  },
  "no_writes": "本次为无写入预演：未写入任何事件、未开启新代、归档快照未受影响"
}
```

预演失败（如到达顺序违规）返回与正式摄取相同的错误码，`error.details` 内含逐项校验结果与 `first_failure_index`，且同样零写入。

### 理赔申请示例

**1) 受理（201，仅当接收时该类型 filable）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/claims \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: IDEMP-20250220-001' \
  -d '{
    "claim_type": "delay",
    "idempotency_key": "IDEMP-20250220-001",
    "received_at": "2025-02-20T12:00:00+08:00"
  }'
```

响应节选：

```json
{
  "claim_no": "CLM-20250220120000-9F2A1C77",
  "order_id": "O-1001",
  "claim_type": "delay",
  "claim_name": "延迟送达",
  "idempotency_key": "IDEMP-20250220-001",
  "received_at_utc": "2025-02-20T04:00:00+00:00",
  "accepted_at_utc": "2025-02-20T04:00:00.182344+00:00",
  "decision_code": "FILABLE",
  "status": "accepted",
  "replayed": false,
  "immutable": true,
  "policy_version": "v2",
  "policy": {"version": "v2", "window": {"days": 10, "day_basis": "business", "cutoff": "23:59:59"}},
  "deadline_local": "2025-02-25T23:59:59+08:00",
  "deadline_utc": "2025-02-25T15:59:59+00:00",
  "missing_evidence": [],
  "basis": ["实际签收 ... 构成延迟送达", "时限 10 个工作日（已跳过当地节假日），截止 2025-02-25 23:59:59 CST", "当前时间在申请时限内，可申请"],
  "generation_no": 1,
  "archived_generation_rechecked": false,
  "claim": { "...该类型完整判定快照（冻结）..." },
  "determination": { "...整单完整判定快照（冻结）..." }
}
```

**2) 同键同类型重试**（网络超时/客户端重试）：返回 **200** 同一凭证，`replayed=true`，接收时间/受理时间/依据均为首次值。同键换类型：**409 CLAIM_CONFLICT**；同类型换键：**409 CLAIM_ALREADY_ACCEPTED**。

**3) 不予受理**：如在 2/26（截止后）提交，返回 **422 CLAIM_DEADLINE_PASSED**，不生成凭证：

```json
{"error": {"code": "CLAIM_DEADLINE_PASSED", "message": "延迟送达理赔申请不予受理：已过当地申请截止时刻，不再受理",
  "details": {"claim_type": "delay", "status": "not_filable", "decision_code": "NOT_FILABLE",
              "deadline_local": "2025-02-25T23:59:59+08:00", "voucher_generated": false, "basis": ["..."]}}}
```

**4) 查询凭证**：`GET /api/v1/claims/CLM-...`（含判定代号与冻结依据）；`GET /api/v1/orders/O-1001/claims` 列全部凭证。

> 说明：申请受理不会归档或推进任何"代"；当前代归档与否都按接收时间现算。凭证是只追加（insert-only）数据，无更新/删除接口。

## 设计取舍与扩展

- **存储**：SQLite + 进程内互斥锁，事件"顺序校验 + 插入"在单事务内完成；水平扩展或多副本时把 `app/db.py` 换成 Postgres（事件表加 `(order_id, arrived_at)` 约束即可）。
- **新增政策版本**：向 `policies` 表插入一行 JSON（时限天数、工作日/自然日、截止钟点均可配），无需发版；新增节假日在 `calendars` 表 upsert。
- **新增理赔类型/扫描事件**：在 `constants.py` 登记标签、政策 JSON 增规则、`engine.py` 增加一个 resolver，接口与代际机制无需改动。
- 截止钟点默认 23:59:59，可被政策窗口级 `cutoff` 或环境变量 `CLAIM_DEFAULT_CUTOFF` 覆盖。

### v1.2.0 变动（节假日表工作日诊断）

- 新增只读诊断接口 `POST /api/v1/calendars/diagnose`：提交日历代码、时区、带偏移起始时刻、1～60 个工作日与当地截止钟点，系统读取现有日历，自起始日次日起逐日输出日期/星期、工作/休息判定、补班或放假声明、周末默认规则、累计工作日数与跳过原因，直到第 N 个工作日，并返回当地与 UTC 截止时刻。
- 补班（`workday`）落在周末照常计入、放假（`holiday`）落在工作日照常跳过；无声明日期按周一至周五工作、周六日休息的默认规则。
- 全程只读，**不修改日历、政策、订单或凭证**；时间无偏移、日历/时区不存在、天数越界、钟点非法均以统一错误体明确拒绝（422/404）。
- 核心逻辑为无状态纯函数 `engine.diagnose_business_days`（日历数据由服务层只读注入）；新增 21 个测试用例（共 89 个），Compose 启动方式（`docker compose up --build` 自动初始化）与既有接口保持不变。

### v1.1.0 变动（理赔申请）

- 新增理赔申请能力：`POST /orders/{id}/claims`（幂等受理）、`GET /orders/{id}/claims`、`GET /claims/{claim_no}`；新增只追加表 `claim_vouchers`（建表语句带 `IF NOT EXISTS`，老数据目录重启即自动升级，历史数据不受影响）。
- 核验以**服务实际接收时间**为准实时重算，即使当前代已归档也不采用归档快照；接收时刻之后到达的事件不计入依据。
- 受理即冻结政策全文、该类型判定与整单判定；后续更正/新代不改凭证。
- 既有事件摄取、无写入预演、归档、判定接口及统一错误体 `{"error":{...}}` 约定**完全保持不变**；测试由 41 个增至 68 个。
