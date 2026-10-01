# 批次追溯服务（batch-trace）

面向小型食品加工厂的**本地批次追溯服务**：登记原料批次、记录生产投料与产出、
把批次分装为可独立投料的子批次、按库位核算批次库存并在库位间调拨、查询批次
余量与上下游谱系，并在发现疑似污染批次时沿投料与分装关系找出并隔离全部后代
产出。

- 语言/框架：Python 3.12 + FastAPI + SQLAlchemy 2
- 关系数据库：PostgreSQL 16（Docker Compose 一键启动）；无数据库时自动回退本地 SQLite 文件库
- 计量：`NUMERIC(18,4)` 定点数，数量必须为**正数**，同一笔单据内**单位一致**
- 部署：Dockerfile 构建镜像，`docker compose up` 安装依赖、建库建表并启动

---

## 一、快速启动

### 方式 A：Docker Compose（推荐，含 PostgreSQL）

```bash
docker compose up --build -d
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8080/docs>

API 容器会等待 PostgreSQL 健康后自动**建库建表**（`app/entrypoint.py` 调用 `CREATE TABLE IF NOT EXISTS ...`），
数据持久化在 compose 卷 `trace_pgdata` 中。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 方式 B：本地直接运行（自动使用 SQLite）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.entrypoint
```

不设置 `DATABASE_URL` 时会在项目根目录创建 `trace.db`，服务同样监听 `http://0.0.0.0:8080`。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 见下 | SQLAlchemy 数据库连接串。compose 中为 `postgresql+psycopg2://trace:trace@db:5432/trace`；未设置时回退 `sqlite:///./trace.db` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `trace` / `trace` / `trace` | 未设置 `DATABASE_URL` 时用于拼装 PostgreSQL 连接串 |
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8080` | HTTP 监听端口 |
| `DB_POOL_SIZE` | `5` | PostgreSQL 连接池大小 |

compose 的端口映射：宿主机 `8080` → 容器 `8080`，可在 `docker-compose.yml` 的
`ports` 处修改宿主机端口。

---

## 三、HTTP 接口

所有写操作（`POST`）都必须携带请求头 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/raw-batches` | 登记一个原料批次及初始数量（数量进入默认库位） |
| POST | `/productions` | 提交一次生产：多批次投入 → 一个产出批次（可按库位扣减） |
| POST | `/batches/{batch_no}/splits` | 批次分装：一个父批次 → 多个可独立投料的子批次（可按库位扣减） |
| POST | `/transfers` | 库位调拨：凭唯一调拨单号在两个库位间移动某批次的数量 |
| GET | `/batches/{batch_no}/locations` | 查询批次的库位余额（各库位之和 = 全局余量） |
| POST | `/productions/{output_no}/cancellation` | 撤销录入错误的生产单：按原消耗库位返还来源投料、作废产出与投料边 |
| GET | `/productions/{output_no}` | 按产出编号查询生产原单据（含撤销状态/原因） |
| GET | `/batches/{batch_no}` | 查询批次余量与状态（全局余量，字段与含义不变） |
| GET | `/batches/{batch_no}/lineage` | 查询上下游谱系（递归，不含已撤销投料边） |
| POST | `/contamination` | 标记疑似污染批次，返回并隔离全部后代 |
| POST | `/reviews` | 质检复核确认误报：撤销疑似标记并重算隔离 |

### 业务规则

1. 数量一律为正数，使用定点数计算；同一生产单内所有批次单位必须一致。
2. 产出量 **不得超过** 投入总量（允许加工损耗，即产出 < 投入）。
3. 批次编号全局唯一、**不得复用**（原料批次与产出批次共用编号空间）。
4. 来源不存在、用量无效（非正数）、单位不一致或余量不足时，**整笔拒绝，不改变任何余量**。
5. 污染规则：标记某批次疑似污染后，其全部直接/间接产出（混料后代）**整体隔离**；
   隔离批次（含疑似批次本身）**不得再投料**，提交生产时整笔拒绝。
6. 复核规则：质检复核确认误报时，`POST /reviews` 提交**当前疑似批次编号**与
   **复核说明**（非空白），撤销其疑似标记并重算该批次及全部后代的隔离状态——
   自身或任一祖先仍疑似则继续隔离，否则解除。混料后代不会因一个来源解除就忽略
   另一疑似来源；编号不存在报 `SOURCE_NOT_FOUND`(404)，批次当前非疑似（从未标记
   或已撤销）报 `BATCH_NOT_SUSPECT`(409)，二者均不改变任何状态。批次可经历
   “标记 → 复核撤销 → 再次标记 → 再次复核”。
7. 幂等：同一 `Idempotency-Key` + 同一请求体重试，返回首次的状态码与响应体、不重复扣减、不重复撤销；
   同一 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
8. 并发：生产提交在单事务内对来源批次加行锁（PostgreSQL `SELECT ... FOR UPDATE`，
   按编号排序加锁避免死锁；SQLite 以 `BEGIN IMMEDIATE` 串行化写事务），并发提交**不会超用**同一批次余量。
   污染标记/复核对“自身 + 全部后代”的闭包按编号排序加行锁，与生产事务互斥，
   并发结果等价于复核、污染标记、投料三者的某一串行顺序；隔离批次绝不会被投料。
9. 生产单撤销：录入错误时，操作员可凭**产出批次编号**与**非空撤销原因**
   `POST /productions/{output_no}/cancellation` 撤销该单。**仅当产出同时满足**
   以下条件才允许撤销，任一不符整笔拒绝、**不改变任何库存**：
   - 产出编号存在且确为生产产出（原料批次不可撤销）；
   - 该产出**从未被后续生产投料**（以保留的投料记录严格判定，即便下游单据
     后来也被撤销，本产出仍视为“曾被投料”而不可撤销）；
   - 产出**当前非疑似**（仅因疑似祖先被连带隔离不阻止撤销）；
   - 产出**无质检复核记录**；
   - 该生产单**尚未撤销**（重复撤销明确拒绝）。

   撤销在**同一事务内**：按各来源的**原投料量**返还余量（即使来源批次已隔离
   也照常返还；返还不会超过其历史消耗，余量恒不超过初始量）；生产单标记为
   已撤销并记录原因/时间。产出批次行与原单据**保留可查**（编号不得复用），
   但已撤销产出**不得再投料**，其投料边**不再进入有效谱系查询**、也**不再
   参与疑似隔离传播**；产出若此前仅因疑似祖先被连带隔离，撤销边后随之解除。
   编号不存在报 `SOURCE_NOT_FOUND`(404)，原料批次报 `PRODUCTION_NOT_FOUND`(404)；
   已被投料 `OUTPUT_ALREADY_CONSUMED`(409)、当前疑似 `OUTPUT_IS_SUSPECT`(409)、
   有复核记录 `PRODUCTION_HAS_REVIEW`(409)、重复撤销
   `PRODUCTION_ALREADY_CANCELLED`(409)、撤销产出投料 `BATCH_CANCELLED`(409)。
10. 撤销并发：撤销与污染标记/复核一样先取 QC 串行化锁，再按编号升序锁定
    “产出 + 各来源”，使撤销 / 投料 / 污染标记 / 复核任意并发都等价于某一串行
    顺序，不会超用余量或误放行（例如“撤销成功且该产出又被投料”不会发生）。
11. 批次分装（split）：同一原料或产出批次可按**父批次编号**
    `POST /batches/{batch_no}/splits`，一笔提交**多个互异新编号**及各自**正数
    分装量**，服务返回**父批次新余量、子批次清单与分装关系**。
    - 子批次**继承父批次单位**，其初始/当前余量等于分装量；**分装不产生
      生产单**（子批次查不到生产原单据），但子批次与普通批次一样**可作为
      正常生产来源**投料，也可再次分装；
    - 校验：父批次须**存在**（`SOURCE_NOT_FOUND` 404）、**未隔离**
      （`BATCH_QUARANTINED` 409）且**非已撤销产出**（`BATCH_CANCELLED` 409）；
      同一笔分装内新编号必须**互异**（`DUPLICATE_SPLIT_TARGET` 422）；
      新编号全局**不得复用**（`BATCH_NO_REUSED` 409）；分装总量必须为正且
      **不得超过父批次余量**（`INSUFFICIENT_BALANCE` 409，details 中
      `requested` 为分装总量）。任一不符**整笔拒绝、不改任何库存**，
      已扣减到一半也会随事务回滚；
    - **谱系**：分装边（`kind: "SPLIT"`）与投料边（`kind: "PRODUCTION"`）
      **一起遍历**，祖先/后代、深度与边集合均同时包含两类关系；同一批次经
      多条路径（混料 + 分装形成的菱形 DAG）只出现一次，深度取最短路径；
    - **隔离传播**：祖先被标记疑似或经复核重算后，隔离沿**分装边与投料边
      两类关系**传播并重算；混料后代仍按“任一疑似来源”隔离。分装新建的
      子批次初始不隔离；在隔离父批次上提交分装会被整笔拒绝；
    - **撤销联动**：父批次若为**生产产出**，一旦被分装即**视为曾被使用**
      ——即使子批次尚未投料，原撤销入口也以 `OUTPUT_ALREADY_SPLIT`(409)
      拒绝（details 的 `split_into` 列出子批次编号），且不改变任何库存；
    - **并发**：分装与生产提交共用同一套单事务 + 按编号升序行锁
      （SQLite 为 `BEGIN IMMEDIATE` 串行化），分装 / 投料并发绝不超用；
      与污染标记 / 复核经行锁与 QC 表锁互斥，结果等价于某一串行顺序，
      绝不漏隔离；
    - 幂等沿用既有 `Idempotency-Key` 规则：同键同体重放返回首次结果、
      只扣一次，同键异体 `409 IDEMPOTENCY_CONFLICT`，失败不固化可重试。
12. 批次按库位核算库存：每个批次的数量同时记录在**全局余量**
    （`batches.remaining_qty`，原查询字段与含义不变）与**库位余额**
    （`batch_locations`）两层，各库位之和恒等于全局余量。
    - **默认库位**：库位核算上线前的既有批次余量、以及此后新登记原料批次的
      数量，一律归入默认库位 `DEFAULT`；生产产出与分装子批次的数量也进入
      默认库位。默认库位不参与任何特殊排序；
    - **调拨**：`POST /transfers` 凭**唯一调拨单号**、批次编号、来源库位、
      目标库位与正数数量，把数量在两个库位间移动，返回两库位新余额；
      调拨只动库位层，批次全局余量不变。库位编号为空（`422 VALIDATION_ERROR`）
      或来源/目标相同（`422 SAME_LOCATION`）、批次不存在（`404 SOURCE_NOT_FOUND`）、
      来源库位不足（`409 INSUFFICIENT_BALANCE`，details 含 `location_no`）、
      调拨单号复用（`409 TRANSFER_NO_REUSED`）时**整单拒绝、两库位余额不变**。
      隔离批次仍不得投料或分装，但**允许调拨**；
    - **库位余额查询**：`GET /batches/{batch_no}/locations` 返回该批次全部
      库位行（按库位编号升序，含已扣到 0 的历史库位行）与全局余量，
      各库位之和恒等于全局余量；
    - **投料/分装按库位扣减**：生产投料的每笔投入与分装请求均可选带
      `location` 字段——指定时**仅扣该库位**（该库位不足即整笔拒绝，即使
      全局余量足够）；未指定时**按库位编号升序**依次扣减（可跨库位）。
      请求与响应的既有字段保持不变，`location` 为新增可选字段；
    - **撤销按原消耗库位返还**：生产提交时逐笔记录每个来源的库位扣减明细，
      撤销生产单时按原明细把数量返还回各自库位（库位核算上线前的历史投料
      没有明细，按口径返还到默认库位）；
    - **并发与一致性**：调拨 / 投料 / 分装 / 撤销都在持有批次行锁的同一
      事务内同步更新两层余额（PostgreSQL `SELECT ... FOR UPDATE`，SQLite
      `BEGIN IMMEDIATE` 串行化），并发执行绝不超用、绝不出现两层余额失配；
    - 调拨沿用既有 `Idempotency-Key` 规则：同键同体重放返回首次结果、
      只调拨一次，同键异体 `409 IDEMPOTENCY_CONFLICT`，失败不固化可重试。

### 错误响应格式

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "批次 'RM-1' 余量不足：请求 200，剩余 100", "details": { } } }
```

常见错误码：`SOURCE_NOT_FOUND`(404)、`PRODUCTION_NOT_FOUND`(404)、`BATCH_NO_REUSED`(409)、
`INSUFFICIENT_BALANCE`(409)、`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、
`DUPLICATE_INPUT`(422)、`DUPLICATE_SPLIT_TARGET`(422)、
`VALIDATION_ERROR`(422)、`BATCH_QUARANTINED`(409)、`BATCH_NOT_SUSPECT`(409)、
`OUTPUT_ALREADY_CONSUMED`(409)、`OUTPUT_ALREADY_SPLIT`(409)、`OUTPUT_IS_SUSPECT`(409)、
`PRODUCTION_HAS_REVIEW`(409)、`PRODUCTION_ALREADY_CANCELLED`(409)、`BATCH_CANCELLED`(409)、
`SAME_LOCATION`(422)、`TRANSFER_NO_REUSED`(409)、
`IDEMPOTENCY_CONFLICT`(409)。

---

## 四、HTTP 调用样例

### 1. 登记原料批次

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: raw-rm-a-001' \
  -d '{"batch_no":"RM-A","unit":"kg","quantity":"100","note":"高筋面粉"}'
```

```json
{"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":100.0,
 "is_suspect":false,"is_quarantined":false,"note":"高筋面粉"}
```

### 2. 提交一次混料生产（多批次投入 → 一个产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{
        "output_batch_no": "MIX-1",
        "unit": "kg",
        "output_quantity": 90,
        "inputs": [
          {"batch_no": "RM-A", "quantity": 40},
          {"batch_no": "RM-B", "quantity": 55}
        ]
      }'
```

### 3. 安全重试（同一 key + 同一内容）

```bash
# 与上一条完全相同的请求：返回原结果，余量只扣一次
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{ ...同上... }'

# 同一 key 换不同内容：
# 409 {"error":{"code":"IDEMPOTENCY_CONFLICT", ...}}
```

### 4. 查询批次余量

```bash
curl http://localhost:8080/batches/RM-A
# {"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":60.0, ...}
```

### 5. 查询上下游谱系

```bash
curl http://localhost:8080/batches/RM-A/lineage
```

```json
{
  "batch_no": "RM-A",
  "upstream": [],
  "downstream": [
    {"batch_no": "MIX-1", "depth": 1, "remaining_qty": 40.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false},
    {"batch_no": "FG-1",  "depth": 2, "remaining_qty": 50.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false}
  ],
  "edges": [
    {"from": "RM-A", "to": "MIX-1", "kind": "PRODUCTION"},
    {"from": "MIX-1", "to": "FG-1", "kind": "PRODUCTION"}
  ]
}
```

`depth` 表示与查询批次相隔的关系层数；`edges` 给出当前谱系子图中的有向边，
`kind` 为 `PRODUCTION`（投料边：来源 → 产出）或 `SPLIT`（分装边：父批次 →
子批次），两类关系在谱系与隔离传播中等价遍历。同一批次经多条路径到达时
（混料/分装形成的菱形 DAG）只出现一次，`depth` 取最短路径。

### 6. 标记疑似污染并找出全部后代

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cont-20260930-01' \
  -d '{"batch_no":"RM-A","note":"抽检农残超标"}'
```

```json
{
  "source": "RM-A",
  "is_suspect": true,
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "newly_quarantined": ["MIX-1", "FG-1"]
}
```

`MIX-1`（混料时混入 RM-A）与 `FG-1`（间接后代）被**整体隔离**；与 RM-A 无关的批次不受影响。
此后再用隔离批次投料会被拒绝：

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-fg2-001' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 {"error":{"code":"BATCH_QUARANTINED","message":"批次 'MIX-1' 已隔离，隔离批次不得再投料"}}
```

### 7. 质检复核：确认误报，撤销疑似标记并重算隔离

沿用上例：`RM-A` 已被标记疑似，混料后代 `MIX-1 = RM-A + RM-B`、`MIX-1 -> FG-1`
均被隔离。质检复检确认 RM-A 为误报：

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: review-rm-a-20260930-01' \
  -d '{"batch_no":"RM-A","review_note":"留样复测未检出农残，确认误报"}'
```

```json
{
  "reviewed_batch_no": "RM-A",
  "is_suspect": false,
  "review_note": "留样复测未检出农残，确认误报",
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "released_batch_nos": ["FG-1", "MIX-1", "RM-A"],
  "still_quarantined": []
}
```

- `affected_batch_nos`：本次重算涉及的全部后代；
- `released_batch_nos`：本次**解除隔离**的批次（复核批次自身 + 不再有任何疑似
  祖先的后代）；`still_quarantined`：重算范围内仍须隔离的批次。
- 此后 `GET /batches/{no}` 与 `/batches/{no}/lineage` 同步反映 `is_suspect=false`、
  `is_quarantined=false`；解除隔离的批次可重新投料。

混料来源不会被“连带误放”：若 RM-B 当时也处于疑似状态，撤销 RM-A 只会放行
RM-A 自身，`MIX-1`/`FG-1` 因仍有疑似祖先 RM-B 而继续隔离：

```json
{ "released_batch_nos": ["RM-A"], "still_quarantined": ["FG-1", "MIX-1"] }
```

非法复核明确报错且不改变状态：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-x' \
  -d '{"batch_no":"GHOST","review_note":"x"}'
# 404 SOURCE_NOT_FOUND

# 已撤销 / 从未疑似
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-a2' \
  -d '{"batch_no":"RM-A","review_note":"再次撤销"}'
# 409 BATCH_NOT_SUSPECT
```

### 8. 整笔拒绝示例（余量不足时不产生副作用）

```bash
# RM-A 当前余量 60，却请求消耗 200
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-bad-001' \
  -d '{"output_batch_no":"MIX-X","unit":"kg","output_quantity":200,
       "inputs":[{"batch_no":"RM-A","quantity":200}]}'
# HTTP/1.1 409 ... INSUFFICIENT_BALANCE；MIX-X 未创建，RM-A 余量仍为 60
```

### 9. 撤销录入错误的生产单（返还来源投料、作废产出）

接上例，一次混料生产 `MIX-1 = 40 RM-A + 20 RM-B`（产出 55）录入有误，
操作员凭产出编号与非空撤销原因撤销：

```bash
curl -X POST http://localhost:8080/productions/MIX-1/cancellation \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cancel-mix1-20260930-01' \
  -d '{"cancel_reason":"产出编号录入错误，实际应为 MIX-1B"}'
```

```json
{
  "output_batch_no": "MIX-1",
  "status": "CANCELLED",
  "cancel_reason": "产出编号录入错误，实际应为 MIX-1B",
  "cancelled_at": "2026-09-30T06:20:31.123456+00:00",
  "returned_inputs": [
    {"batch_no": "RM-A", "returned_quantity": 40.0},
    {"batch_no": "RM-B", "returned_quantity": 20.0}
  ]
}
```

撤销结果含**产出编号、返还明细（各来源原投料量）与撤销状态**；同一事务内
`RM-A` 余量回到 100、`RM-B` 回到 50（即使某来源当时已隔离也照常返还）。

撤销后保留产出编号、原单据与撤销原因供查询：

```bash
# 批次查询带 is_cancelled=true（编号仍被占用，不得复用）
curl http://localhost:8080/batches/MIX-1
# {"batch_no":"MIX-1", ..., "remaining_qty":55.0, "is_cancelled":true}

# 原单据（含撤销状态/原因/时间）
curl http://localhost:8080/productions/MIX-1
# {"output_batch_no":"MIX-1", ..., "is_cancelled":true,
#  "cancel_reason":"产出编号录入错误，实际应为 MIX-1B", "cancelled_at":"..."}

# 谱系不再包含该投料边
curl http://localhost:8080/batches/RM-A/lineage
# {"batch_no":"RM-A","is_cancelled":false,"upstream":[],"downstream":[],"edges":[]}
```

已撤销产出不得再投料（编号也不得复用）；撤销的投料边不再进入疑似传播：

```bash
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-after-cancel' \
  -d '{"output_batch_no":"FG-9","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 BATCH_CANCELLED：批次 'MIX-1' 为已撤销生产单的产出，已撤销产出不得再投料
```

撤销沿用现有写请求的幂等约定：同 `Idempotency-Key` + 同请求体重放返回首次
结果且只返还一次；同键异体返回 `409 IDEMPOTENCY_CONFLICT`。

条件不符明确拒绝且不改库存（示例）：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/productions/GHOST/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: c-x' \
  -d '{"cancel_reason":"x"}'
# 404 SOURCE_NOT_FOUND

# 产出编号实为原料批次（无生产单）
# 404 PRODUCTION_NOT_FOUND

# 产出已被后续生产投料（即便下游单据后来撤销，也仍按“曾被投料”拒绝）
# 409 OUTPUT_ALREADY_CONSUMED，details.consumed_by 列出投料它的下游产出

# 产出当前疑似（须先复核；但复核会留下记录而同样不可撤销）
# 409 OUTPUT_IS_SUSPECT / 409 PRODUCTION_HAS_REVIEW

# 重复撤销
# 409 PRODUCTION_ALREADY_CANCELLED

# 撤销原因为空或纯空白
# 422 VALIDATION_ERROR
```

### 10. 分装：把同一批次拆成可独立投料的子批次

把原料批次 `RM-1`（100 kg）一次性分装为两个子批次 `RM-1-A`（30 kg）与
`RM-1-B`（20 kg）：

```bash
curl -X POST http://localhost:8080/batches/RM-1/splits \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: split-rm1-20261001-01' \
  -d '{"splits":[
        {"batch_no":"RM-1-A","quantity":"30"},
        {"batch_no":"RM-1-B","quantity":"20"}
      ]}'
```

```json
{
  "parent_batch_no": "RM-1",
  "parent_remaining_qty": 40.0,
  "splits": [
    {"batch_no":"RM-1-A","unit":"kg","quantity":30.0,"remaining_qty":30.0,
     "is_suspect":false,"is_quarantined":false},
    {"batch_no":"RM-1-B","unit":"kg","quantity":20.0,"remaining_qty":20.0,
     "is_suspect":false,"is_quarantined":false}
  ],
  "relations": [
    {"parent_batch_no":"RM-1","child_batch_no":"RM-1-A","quantity":30.0},
    {"parent_batch_no":"RM-1","child_batch_no":"RM-1-B","quantity":20.0}
  ]
}
```

子批次继承单位、**分装不产生生产单**，但可作为正常生产来源投料，也可再次分装：

```bash
# 子批次作为正常来源混料投料
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-mix2-001' \
  -d '{"output_batch_no":"MIX-2","unit":"kg","output_quantity":25,
       "inputs":[{"batch_no":"RM-1-A","quantity":20},{"batch_no":"RM-1-B","quantity":5}]}'

# 子批次继续分装
curl -X POST http://localhost:8080/batches/RM-1-A/splits \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: split-rm1a-001' \
  -d '{"splits":[{"batch_no":"RM-1-A-1","quantity":"10"}]}'
```

分装边与投料边一起进入谱系（`kind` 区分两类），疑似隔离沿两类边传播：

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cont-rm1-01' \
  -d '{"batch_no":"RM-1","note":"抽检农残超标"}'
# affected_batch_nos 同时包含分装后代（RM-1-A/RM-1-B/RM-1-A-1）与
# 投料后代（MIX-2）；混料后代仍按“任一疑似来源”整体隔离
```

任一不符整笔拒绝、不改库存（示例）：

```bash
# 父批次不存在：404 SOURCE_NOT_FOUND
# 同一笔分装新编号重复：422 DUPLICATE_SPLIT_TARGET
# 新编号已被占用（原料/产出/子批次共用编号空间）：409 BATCH_NO_REUSED
# 分装总量超过父余量（如余量 40 却分装 30+20）：
#   409 INSUFFICIENT_BALANCE，details.requested 为分装总量 50
# 父批次已隔离（含疑似）：409 BATCH_QUARANTINED
# 父批次为已撤销产出：409 BATCH_CANCELLED
# 分装量为 0/负数或 splits 为空：422 VALIDATION_ERROR
```

父批次若是生产产出，分装后视为曾被使用——即使子批次尚未投料，撤销入口也拒绝：

```bash
# MIX-1 是生产产出，曾分装出 MIX-1-A（即便 MIX-1-A 从未投料）
curl -i -X POST http://localhost:8080/productions/MIX-1/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cancel-mix1-01' \
  -d '{"cancel_reason":"录入错误"}'
# HTTP/1.1 409
# {"error":{"code":"OUTPUT_ALREADY_SPLIT",
#   "details":{"batch_no":"MIX-1","split_into":["MIX-1-A"]}, ...}}
# 来源余量不返还、生产单不撤销
```

### 11. 库位核算与调拨：默认库位、库位间移动、按库位扣减

新登记批次的数量进入默认库位 `DEFAULT`；可随时查询批次的库位余额，
各库位之和恒等于全局余量：

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: raw-rm1-01' \
  -d '{"batch_no":"RM-1","unit":"kg","quantity":"100"}'

curl http://localhost:8080/batches/RM-1/locations
# {"batch_no":"RM-1","remaining_qty":100.0,
#  "locations":[{"location_no":"DEFAULT","qty":100.0}]}
```

凭唯一调拨单号把 30 kg 从默认库位调到 `L-01`，响应返回两库位新余额；
批次全局余量不变：

```bash
curl -X POST http://localhost:8080/transfers \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: tr-20261001-01' \
  -d '{"transfer_no":"TR-20261001-01","batch_no":"RM-1",
       "from_location":"DEFAULT","to_location":"L-01","quantity":"30"}'
```

```json
{"transfer_no":"TR-20261001-01","batch_no":"RM-1",
 "from_location":"DEFAULT","to_location":"L-01","quantity":30.0,
 "from_remaining_qty":70.0,"to_remaining_qty":30.0}
```

```bash
curl http://localhost:8080/batches/RM-1/locations
# {"batch_no":"RM-1","remaining_qty":100.0,
#  "locations":[{"location_no":"DEFAULT","qty":70.0},
#              {"location_no":"L-01","qty":30.0}]}
curl http://localhost:8080/batches/RM-1        # 全局余量仍为 100.0
```

生产投料与分装可选带 `location`：指定时仅扣该库位，未指定时按库位编号
升序依次扣减（可跨库位）；产出与分装子批次进入默认库位：

```bash
# 指定库位投料：仅扣 L-01
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-fg1-01' \
  -d '{"output_batch_no":"FG-1","unit":"kg","output_quantity":25,
       "inputs":[{"batch_no":"RM-1","quantity":25,"location":"L-01"}]}'
# RM-1 库位变为 DEFAULT 70 / L-01 5；FG-1 的 25 进入 DEFAULT

# 未指定库位投料：按库位编号升序扣（DEFAULT 先于 L-01）
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-fg2-01' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":75,
       "inputs":[{"batch_no":"RM-1","quantity":75}]}'
# 先扣 DEFAULT 70、再扣 L-01 5

# 分装同样可选 location（仅从该库位扣减分装总量）
curl -X POST http://localhost:8080/batches/RM-1/splits \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: split-rm1-loc-01' \
  -d '{"location":"L-01","splits":[{"batch_no":"RM-1-C","quantity":"5"}]}'
```

撤销生产单按**原消耗库位**返还（上例 FG-1 撤销后 25 kg 回到 L-01 而非
DEFAULT）；隔离批次不得投料/分装，但允许调拨：

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cont-rm1-02' \
  -d '{"batch_no":"RM-1","note":"待复检"}'
# 隔离后调拨仍允许（例如移入隔离库位 L-Q）
curl -X POST http://localhost:8080/transfers \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: tr-20261001-02' \
  -d '{"transfer_no":"TR-20261001-02","batch_no":"RM-1",
       "from_location":"DEFAULT","to_location":"L-Q","quantity":"10"}'
# 201；但投料/分装仍被 409 BATCH_QUARANTINED 拒绝
```

任一不符整笔拒绝、两库位余额不变（示例）：

```bash
# 来源/目标库位相同：422 SAME_LOCATION
# 库位编号为空或纯空白：422 VALIDATION_ERROR
# 批次不存在：404 SOURCE_NOT_FOUND
# 来源库位不足（含来源库位从未存放过该批次）：409 INSUFFICIENT_BALANCE
#   details 含 batch_no / location_no / requested / remaining
# 调拨单号已被占用：409 TRANSFER_NO_REUSED
# 数量为 0/负数：422 VALIDATION_ERROR
```

调拨沿用既有幂等约定：同 `Idempotency-Key` + 同请求体重放返回首次结果、
只调拨一次；同键异体 `409 IDEMPOTENCY_CONFLICT`；失败请求不固化、可修正后
以同键重试。

---

## 五、项目结构

```
app/
  config.py       # 环境变量配置
  db.py           # 引擎/会话、SQLite 写锁与 PG 行锁支撑、建表与增量补列、
                  # 既有批次余量回填默认库位
  models.py       # batches / batch_locations(库位余额) / productions(含撤销字段) /
                  # production_inputs / production_input_locations(投料库位明细) /
                  # batch_splits(分装关系) / transfers(调拨单) / batch_reviews /
                  # idempotency_keys
  schemas.py      # Pydantic 请求响应模型（正数、定点数、非空撤销原因、分装校验、
                  # 库位编号与调拨单校验）
  errors.py       # 业务错误码
  service.py      # 核心逻辑：登记、库位扣减/入账、调拨、分装、生产事务、撤销返还、
                  # 幂等、递归谱系（投料边+分装边）、污染传播、复核重算
  routes.py       # HTTP 路由
  main.py         # FastAPI 应用与异常处理
  entrypoint.py   # 容器入口：等待数据库 → 建表 → 启动
tests/            # 端到端 + 并发测试（110 个用例，含库位/调拨/库位并发）
Dockerfile
docker-compose.yml
```

## 六、并发与一致性说明

- 每次生产提交是一个数据库事务：先按编号升序逐个锁定全部来源批次
  （全局统一加锁顺序，杜绝生产事务间死锁），再依次校验单位、隔离状态、
  余量与产出总量，全部通过后才扣减并写入产出；任一校验失败立即回滚。
- PostgreSQL 依赖 `SELECT ... FOR UPDATE` 行锁串行化同一批次的扣减；
  SQLite 写事务以 `BEGIN IMMEDIATE` 开始并设置 `busy_timeout`，写操作全局串行。
- 污染标记与复核是低频 QC 操作：事务一开始先取 QC 串行化锁
  （PostgreSQL 为 `LOCK TABLE batches IN SHARE ROW EXCLUSIVE`，与生产事务的
  行锁/写入互斥；SQLite 由 `BEGIN IMMEDIATE` 全库写锁覆盖），随后读取
  “自身 + 全部后代”闭包并写入 `is_suspect` / `is_quarantined`。因此
  - 复核 / 污染标记 / 投料任意并发，结果都等价于三者的某一串行顺序；
  - 隔离批次不可能被投料（拿不到与 QC 事务的串行化位置，校验到的
    隔离标志与最终谱系一致）；
  - 复核重算隔离状态时以“自身疑似或任一祖先疑似”为谓词，混料后代只要还有
    一个疑似来源就继续隔离。
- 生产单撤销与污染标记/复核同属低频维护事务：先取同一把 QC 串行化锁，再按
  编号升序锁定“产出 + 各来源”，随后做“曾被投料 / 当前疑似 / 有无复核 /
  是否已撤销”全部校验，通过后在同一事务内返还各来源原投料量、置撤销标记、
  令投料边失效并重算产出隔离位。因此撤销 / 投料 / 污染标记 / 复核任意并发，
  结果都等价于某一串行顺序：不会“既撤销又投料”、不会超用余量，也不会误放行；
  两个相同撤销并发时恰好一个成功、另一个以 `PRODUCTION_ALREADY_CANCELLED` 拒绝，
  来源余量只返还一次。
- 幂等记录与业务数据在同一事务内落库，唯一约束 `(method, path, key)` 兜底并发首请求竞争。
- 分装与生产提交同属余量扣减事务：单事务内按编号升序对父批次加行锁
  （SQLite 为 `BEGIN IMMEDIATE` 全库写锁），扣减、建子批次、写分装关系原子
  完成；并发分装 / 并发分装与投料绝不超用，与 QC 事务（标记/复核/撤销）
  经行锁与表锁互斥，结果等价于某一串行顺序，绝不漏隔离。生产产出一旦存在
  分装边，撤销校验即以 `OUTPUT_ALREADY_SPLIT` 拒绝，杜绝“撤销成功且分装仍
  在谱系中传播”的矛盾结局。
- 库位层与全局层余额的每一次变动都在**持有批次行锁的同一事务**内完成：
  调拨、投料、分装、撤销返还都先锁批次行（PostgreSQL `SELECT ... FOR
  UPDATE`，按编号升序；SQLite 由 `BEGIN IMMEDIATE` 全库串行），再同步更新
  `batch_locations` 与 `batches.remaining_qty`，因此两层余额绝不失配、并发
  绝不超用。调拨只持有一把批次行锁（只移动单个批次的库位数量），不会与
  其它事务形成死锁环；生产提交逐笔记录投料的库位扣减明细
  （`production_input_locations`），撤销据此按原库位返还。

> 注：新表 `batch_locations`、`production_input_locations`、`transfers`、
> `batch_splits`、`batch_reviews` 与 `productions` 表的撤销字段
> （`is_cancelled` / `cancel_reason` / `cancelled_at`）随启动/入口的
> `CREATE TABLE IF NOT EXISTS` 与幂等 `ALTER TABLE ... ADD COLUMN` 自动
> 创建/补齐；启动时还会把**没有任何库位行的既有批次**余量一次性回填到默认
> 库位 `DEFAULT`（幂等，余量为 0 的批次无需库位行），既有数据库升级无需
> 手工迁移；现有登记、生产、污染标记、复核、批次/谱系查询、撤销、分装接口
> 的请求、响应字段与启动方式均保持兼容——谱系响应的 `edges` 仅**新增**
> `kind` 字段（既有投料边为 `"PRODUCTION"`），批次/谱系响应仍**新增**
> `is_cancelled` 字段，生产投料与分装请求仅**新增**可选 `location` 字段，
> 未指定库位时行为与既有口径一致（从全部可用数量中扣减），未调拨过的批次
> 全部数量都在默认库位。
