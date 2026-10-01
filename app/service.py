"""业务逻辑层。所有写操作都在单个事务内完成，校验失败整体回滚。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import errors
from .models import (
    DEFAULT_LOCATION_NO,
    Batch,
    BatchLocation,
    BatchReview,
    BatchSplit,
    IdempotencyKey,
    Production,
    ProductionInput,
    ProductionInputLocation,
    Transfer,
)
from .schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    ReviewIn,
    SplitIn,
    SuspectIn,
    TransferIn,
)

# ---------------------------------------------------------------- 幂等支持


def _canonical_hash(payload: dict[str, Any]) -> str:
    """对请求体做规范序列化后取 sha256；Decimal 用原始标量文本表示。"""

    def default(obj: object) -> object:
        if isinstance(obj, Decimal):
            return f"decimal:{normalize_decimal(obj)}"
        raise TypeError(f"不可序列化的类型 {type(obj)!r}")

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_decimal(value: Decimal) -> str:
    """1 与 1.00 视为同一内容；去掉多余尾零。"""
    return format(value.normalize(), "f")


def idempotent(
    session: Session,
    *,
    method: str,
    path: str,
    key: str | None,
    payload: dict[str, Any],
    action: Callable[[], tuple[int, Any]],
) -> tuple[int, Any]:
    """以幂等键执行动作。

    - 无 key：直接执行（查询类接口不需要）；
    - 同 key + 同内容（含重试）：返回首次固化的状态码与响应；
    - 同 key + 异内容：409 冲突；
    - 失败请求不落幂等记录，允许客户端修正后以同 key 重试。
    """
    if not key:
        status, body = action()
        return status, body

    request_hash = _canonical_hash(payload)
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.method == method,
        IdempotencyKey.path == path,
        IdempotencyKey.key == key,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError_conflict(key)
        return existing.status_code, json.loads(existing.response_body)

    status, body = action()

    record = IdempotencyKey(
        method=method,
        path=path,
        key=key,
        request_hash=request_hash,
        status_code=status,
        response_body=json.dumps(body, ensure_ascii=False, default=str),
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        # 并发下另一请求已抢先登记该 key
        existing = session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError_conflict(key)
            return existing.status_code, json.loads(existing.response_body)
        raise
    return status, body


def AppError_conflict(key: str) -> errors.AppError:
    return errors.AppError(
        "IDEMPOTENCY_CONFLICT",
        f"幂等键 {key!r} 已用于不同的请求内容，异内容冲突",
        409,
    )


# ---------------------------------------------------------------- 库位核算


def _get_location_row(
    session: Session, batch_no: str, location_no: str
) -> BatchLocation | None:
    return session.get(BatchLocation, (batch_no, location_no))


def _credit_location(
    session: Session, batch_no: str, location_no: str, qty: Decimal
) -> BatchLocation:
    """给某批次的某库位入账（行不存在则新建）。调用方须已持有批次行锁。"""
    row = _get_location_row(session, batch_no, location_no)
    if row is None:
        row = BatchLocation(batch_no=batch_no, location_no=location_no, qty=qty)
        session.add(row)
        session.flush()
    else:
        row.qty += qty
    return row


def _deduct_from_locations(
    session: Session,
    batch: Batch,
    amount: Decimal,
    location: str | None = None,
) -> list[tuple[str, Decimal]]:
    """从批次库位层扣减 amount，返回 [(库位编号, 实扣量)] 明细。

    - 指定库位：仅扣该库位，该库位不足即整笔拒绝（INSUFFICIENT_BALANCE）；
    - 未指定库位：按库位编号升序依次扣减（默认库位无特殊优先级），
      各库位之和不足即整笔拒绝。

    调用方必须先持有批次行锁（生产/分装/调拨/撤销均先锁批次行），
    库位层与全局层的扣减在同一事务内完成，两层余额绝不失配。
    本函数只动库位层；批次 remaining_qty 由调用方同步扣减。
    """
    if location is not None:
        row = _get_location_row(session, batch.batch_no, location)
        available = row.qty if row is not None else Decimal("0")
        if amount > available:
            raise errors.insufficient_at_location(
                batch.batch_no, location, amount, available
            )
        row.qty -= amount
        return [(location, amount)]

    rows = session.execute(
        select(BatchLocation)
        .where(BatchLocation.batch_no == batch.batch_no, BatchLocation.qty > 0)
        .order_by(BatchLocation.location_no)
    ).scalars().all()
    total_available = sum((r.qty for r in rows), Decimal("0"))
    if amount > total_available:
        raise errors.insufficient(batch.batch_no, amount, total_available)

    parts: list[tuple[str, Decimal]] = []
    remaining = amount
    for row in rows:
        if remaining <= 0:
            break
        take = min(row.qty, remaining)
        row.qty -= take
        parts.append((row.location_no, take))
        remaining -= take
    return parts


# ---------------------------------------------------------------- 批次操作


def register_raw_batch(session: Session, data: RawBatchIn) -> Batch:
    """登记一个原料批次。编号不得复用；数量归入默认库位。"""
    existing = session.get(Batch, data.batch_no)
    if existing is not None:
        raise errors.batch_exists(data.batch_no)

    batch = Batch(
        batch_no=data.batch_no,
        unit=data.unit,
        initial_qty=data.quantity,
        remaining_qty=data.quantity,
        note=data.note,
    )
    session.add(batch)
    try:
        session.flush()
    except IntegrityError:
        raise errors.batch_exists(data.batch_no) from None
    # 新登记批次的数量一律进入默认库位（全局余量与库位余量同步建立）
    _credit_location(session, batch.batch_no, DEFAULT_LOCATION_NO, data.quantity)
    return batch


def _lock_batch(session: Session, batch_no: str) -> Batch | None:
    """读取批次并加行锁（PostgreSQL FOR UPDATE；SQLite 被写事务锁覆盖）。"""
    stmt = select(Batch).where(Batch.batch_no == batch_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _cancelled_output_nos(session: Session, batch_nos: list[str]) -> set[str]:
    """返回给定编号中“已撤销生产单产出”的编号集合。

    已撤销产出的批次行仍保留（编号不得复用、原单据可查），但不得再投料。
    """
    if not batch_nos:
        return set()
    rows = session.execute(
        select(Production.output_no).where(
            Production.output_no.in_(batch_nos),
            Production.is_cancelled.is_(True),
        )
    ).all()
    return {r[0] for r in rows}


def _lock_batches_in_order(
    session: Session, batch_nos: list[str]
) -> dict[str, Batch]:
    """按编号排序后逐个锁定批次。

    全局统一的加锁顺序（编号升序）保证并发生产事务之间不会出现
    “互相持有对方等待的行锁”式死锁（PostgreSQL FOR UPDATE；
    SQLite 写事务已被 BEGIN IMMEDIATE 全局串行化，FOR UPDATE 为空操作）。
    """
    ordered = sorted(set(batch_nos))
    locked: dict[str, Batch] = {}
    for no in ordered:
        stmt = select(Batch).where(Batch.batch_no == no)
        if session.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        batch = session.execute(stmt).scalar_one_or_none()
        if batch is not None:
            locked[no] = batch
    return locked


def _acquire_qc_lock(session: Session) -> None:
    """污染标记/复核等 QC 写事务的串行化锁。

    PostgreSQL：对 batches 取 SHARE ROW EXCLUSIVE 表锁。它与生产事务的
    ROW SHARE（SELECT ... FOR UPDATE）/ ROW EXCLUSIVE（写入）冲突，也与
    其它 QC 事务互斥——使“标记/复核”与“投料”严格串行，既杜绝隔离批次被
    投料，也避免“先锁 root、再锁后代”与生产的排序行锁之间形成死锁环。
    QC 操作低频，粗粒度表锁代价可接受。
    SQLite：BEGIN IMMEDIATE 已提供全库写串行化，无需额外处理。
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE batches IN SHARE ROW EXCLUSIVE MODE"))


def submit_production(session: Session, data: ProductionIn) -> Production:
    """提交一次生产：校验全部通过后才扣减余量并创建产出批次。

    校验顺序（任一失败整笔拒绝、不改变余量）：
    1. 投入明细编号不重复；
    2. 每个来源批次存在、单位一致、非已撤销产出、未隔离；
    3. 产出编号未被占用；
    4. 每笔用量为正且不超过该批次余量；
    5. 产出总量不超过投入总量。
    """
    source_nos = [item.batch_no for item in data.inputs]
    if len(set(source_nos)) != len(source_nos):
        raise errors.AppError(
            "DUPLICATE_INPUT",
            "同一生产单中来源批次不得重复",
            422,
        )

    sources: dict[str, Batch] = {}
    total_input = Decimal("0")
    # 先按编号排序一次性锁定所有来源批次，避免并发事务交叉加锁死锁
    # （PostgreSQL 行锁；SQLite 为立即写锁）
    locked = _lock_batches_in_order(session, source_nos)
    for no in source_nos:
        batch = locked.get(no)
        if batch is None:
            raise errors.batch_not_found(no)
        sources[no] = batch

    cancelled_nos = _cancelled_output_nos(session, source_nos)

    for item in data.inputs:
        batch = sources[item.batch_no]
        if batch.unit != data.unit:
            raise errors.unit_mismatch(item.batch_no, batch.unit, data.unit)
        if item.batch_no in cancelled_nos:
            raise errors.batch_cancelled(item.batch_no)
        if batch.is_quarantined:
            raise errors.batch_quarantined(item.batch_no)

    if session.get(Batch, data.output_batch_no) is not None:
        raise errors.batch_exists(data.output_batch_no)

    for item in data.inputs:
        batch = sources[item.batch_no]
        if item.quantity > batch.remaining_qty:
            raise errors.insufficient(
                item.batch_no, item.quantity, batch.remaining_qty
            )
        total_input += item.quantity

    if data.output_quantity > total_input:
        raise errors.AppError(
            "OUTPUT_EXCEEDS_INPUT",
            f"产出量 {normalize_decimal(data.output_quantity)} 超过投入总量 "
            f"{normalize_decimal(total_input)}",
            422,
            details={
                "output_quantity": normalize_decimal(data.output_quantity),
                "total_input": normalize_decimal(total_input),
            },
        )

    output = Batch(
        batch_no=data.output_batch_no,
        unit=data.unit,
        initial_qty=data.output_quantity,
        remaining_qty=data.output_quantity,
    )
    session.add(output)
    try:
        session.flush()  # 让产出批次主键可用，并尽早暴露编号冲突
    except IntegrityError:
        raise errors.batch_exists(data.output_batch_no) from None
    # 产出数量进入默认库位
    _credit_location(session, output.batch_no, DEFAULT_LOCATION_NO, data.output_quantity)

    production = Production(
        output_no=output.batch_no, output_qty=data.output_quantity
    )
    session.add(production)
    session.flush()

    for item in data.inputs:
        batch = sources[item.batch_no]
        # 库位层扣减（指定库位仅扣该库位；缺省按库位编号升序），
        # 与全局余量扣减同事务完成；明细落库供撤销时按原库位返还
        parts = _deduct_from_locations(session, batch, item.quantity, item.location)
        batch.remaining_qty -= item.quantity
        input_row = ProductionInput(
            production_id=production.id,
            source_no=batch.batch_no,
            qty=item.quantity,
        )
        session.add(input_row)
        session.flush()
        session.add_all(
            [
                ProductionInputLocation(
                    input_id=input_row.id, location_no=loc, qty=qty
                )
                for loc, qty in parts
            ]
        )

    session.flush()
    # 重新加载聚合，便于响应组装
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.id == production.id)
    ).scalar_one()


# ---------------------------------------------------------------- 批次分装


def split_batch(session: Session, parent_no: str, data: SplitIn) -> dict[str, Any]:
    """把同一批次分装为多个可独立投料的子批次。

    一笔分装包含多个互异的新编号及各自正数分装量。全部校验通过后才在同一
    事务内扣减父批次余量、创建子批次（继承单位，初始/当前余量等于分装量）
    并写分装关系；分装不产生生产单，任一不符整笔拒绝、不改库存。

    校验顺序：
    1. 同一笔分装中新编号互不相同；
    2. 父批次存在（SOURCE_NOT_FOUND 404）、未隔离（BATCH_QUARANTINED 409）、
       非已撤销产出（BATCH_CANCELLED 409）；
    3. 每个新编号未被占用（BATCH_NO_REUSED 409，编号全局不得复用）；
    4. 分装总量为正（入参已逐项保证 > 0）且不超过父批次余量
       （INSUFFICIENT_BALANCE 409）。

    并发：与生产提交相同，按编号升序持锁（此处只锁父批次），子批次行由
    唯一约束兜底并发抢号；SQLite 由 BEGIN IMMEDIATE 全库串行。分装 / 投料
    并发不会超用；与污染标记/复核的关系同生产事务（经行锁与 QC 表锁互斥），
    隔离沿分装边与投料边一并传播，不会漏隔离。
    """
    child_nos = [item.batch_no for item in data.splits]
    if len(set(child_nos)) != len(child_nos):
        dup = next(no for no in child_nos if child_nos.count(no) > 1)
        raise errors.duplicate_split_target(dup)

    parent = _lock_batch(session, parent_no)
    if parent is None:
        raise errors.batch_not_found(parent_no)
    if _is_cancelled_output(session, parent_no):
        raise errors.batch_cancelled(parent_no)
    if parent.is_quarantined:
        raise errors.batch_quarantined(parent_no)

    # 新编号不得复用：编号空间全局唯一（原料/产出/子批次共用）
    existing_nos = {
        r[0]
        for r in session.execute(
            select(Batch.batch_no).where(Batch.batch_no.in_(child_nos))
        ).all()
    }
    if existing_nos:
        raise errors.batch_exists(sorted(existing_nos)[0])

    total = Decimal("0")
    for item in data.splits:
        total += item.quantity
    if total > parent.remaining_qty:
        raise errors.insufficient(parent_no, total, parent.remaining_qty)

    # 库位层扣减分装总量（指定库位仅扣该库位；缺省按库位编号升序），
    # 与全局余量扣减同一事务完成；任一不符整笔回滚
    _deduct_from_locations(session, parent, total, data.location)

    created: list[Batch] = []
    relations: list[BatchSplit] = []
    for item in data.splits:
        child = Batch(
            batch_no=item.batch_no,
            unit=parent.unit,
            initial_qty=item.quantity,
            remaining_qty=item.quantity,
        )
        session.add(child)
        try:
            session.flush()  # 尽早暴露并发下的编号冲突
        except IntegrityError:
            raise errors.batch_exists(item.batch_no) from None
        # 子批次数量一律进入默认库位
        _credit_location(session, child.batch_no, DEFAULT_LOCATION_NO, item.quantity)
        created.append(child)
        relations.append(
            BatchSplit(parent_no=parent_no, child_no=item.batch_no, qty=item.quantity)
        )

    for item in data.splits:
        parent.remaining_qty -= item.quantity
    session.add_all(relations)
    session.flush()

    return {
        "parent_batch_no": parent_no,
        "parent_remaining_qty": float(parent.remaining_qty),
        "splits": [
            {
                "batch_no": child.batch_no,
                "unit": child.unit,
                "quantity": float(child.initial_qty),
                "remaining_qty": float(child.remaining_qty),
                "is_suspect": child.is_suspect,
                "is_quarantined": child.is_quarantined,
            }
            for child in created
        ],
        "relations": [
            {
                "parent_batch_no": parent_no,
                "child_batch_no": child.batch_no,
                "quantity": float(child.initial_qty),
            }
            for child in created
        ],
    }


# ---------------------------------------------------------------- 库位调拨


def transfer_location(session: Session, data: TransferIn) -> dict[str, Any]:
    """库位调拨：把某批次的正数数量从来源库位移动到目标库位。

    只动库位层余额（来源减、目标加），批次全局余量不变。任一不符
    整笔拒绝、两库位余额不变：
    1. 来源/目标库位编号不得相同（SAME_LOCATION 422；空编号由入参校验拦截）；
    2. 批次须存在（SOURCE_NOT_FOUND 404）；
    3. 调拨单号全局唯一、不得复用（TRANSFER_NO_REUSED 409）；
    4. 来源库位余额须充足（INSUFFICIENT_BALANCE 409）。

    隔离批次允许调拨（调拨不是投料/分装，不改变可用性语义）。

    并发：先锁批次行（与生产/分装/撤销同一把行锁，SQLite 由
    BEGIN IMMEDIATE 全库串行），库位扣减/入账与调拨单在同一事务
    落库；调拨只持有一把批次行锁，不会与其它事务形成死锁环。
    """
    if data.from_location == data.to_location:
        raise errors.same_location(data.from_location)

    batch = _lock_batch(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    existing = session.execute(
        select(Transfer.id).where(Transfer.transfer_no == data.transfer_no)
    ).scalar_one_or_none()
    if existing is not None:
        raise errors.transfer_no_reused(data.transfer_no)

    # 来源库位扣减（不足即整笔拒绝），目标库位入账
    _deduct_from_locations(session, batch, data.quantity, data.from_location)
    to_row = _credit_location(session, data.batch_no, data.to_location, data.quantity)

    transfer = Transfer(
        transfer_no=data.transfer_no,
        batch_no=data.batch_no,
        from_location=data.from_location,
        to_location=data.to_location,
        qty=data.quantity,
    )
    session.add(transfer)
    try:
        session.flush()  # 兜底并发下的调拨单号竞争
    except IntegrityError:
        raise errors.transfer_no_reused(data.transfer_no) from None

    from_row = _get_location_row(session, data.batch_no, data.from_location)
    return {
        "transfer_no": data.transfer_no,
        "batch_no": data.batch_no,
        "from_location": data.from_location,
        "to_location": data.to_location,
        "quantity": float(data.quantity),
        "from_remaining_qty": float(from_row.qty if from_row is not None else 0),
        "to_remaining_qty": float(to_row.qty),
    }


def get_batch_locations(session: Session, batch_no: str) -> dict[str, Any]:
    """查询批次的库位余额清单（按库位编号升序）。

    各库位之和恒等于批次全局余量 remaining_qty；已扣到 0 的库位行
    保留在清单中（便于审计该批次曾存放的库位），不影响求和口径。
    """
    batch = session.get(Batch, batch_no)
    if batch is None:
        raise errors.batch_not_found(batch_no)
    rows = session.execute(
        select(BatchLocation)
        .where(BatchLocation.batch_no == batch_no)
        .order_by(BatchLocation.location_no)
    ).scalars().all()
    return {
        "batch_no": batch_no,
        "remaining_qty": float(batch.remaining_qty),
        "locations": [
            {"location_no": row.location_no, "qty": float(row.qty)} for row in rows
        ],
    }


# ---------------------------------------------------------------- 生产单撤销


def _is_cancelled_output(session: Session, batch_no: str) -> bool:
    """该批次是否为一张已撤销生产单的产出（原料批次恒为 False）。"""
    return batch_no in _cancelled_output_nos(session, [batch_no])


def _load_production_for_output(
    session: Session, output_no: str
) -> Production | None:
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.output_no == output_no)
    ).scalar_one_or_none()


def get_production_record(session: Session, output_no: str) -> Production:
    """按产出编号查询生产原单据（含撤销原因/时间）。

    编号不存在 -> SOURCE_NOT_FOUND(404)；编号存在但不是生产产出
    （原料批次）-> PRODUCTION_NOT_FOUND(404)。
    """
    if session.get(Batch, output_no) is None:
        raise errors.batch_not_found(output_no)
    production = _load_production_for_output(session, output_no)
    if production is None:
        raise errors.production_not_found(output_no)
    return production


def cancel_production(session: Session, output_no: str, data: CancelIn) -> dict[str, Any]:
    """撤销一张录入错误的生产单：返还各来源原投料量、作废产出与投料边。

    仅当产出同时满足以下全部条件时允许撤销，任一不符整笔拒绝、不改变库存：
    1. 产出编号存在且确为生产产出（原料批次不可撤销）；
    2. 该生产单尚未撤销（重复撤销明确拒绝）；
    3. 产出从未被后续生产投料（严格按保留的投料记录判定，与下游是否已撤销无关）；
    3b. 产出从未被分装（一旦分装即视为曾被使用，即使子批次尚未投料）；
    4. 产出当前非疑似（自身 is_suspect 为假；仅因疑似祖先被连带隔离不阻止撤销）；
    5. 产出无质检复核记录。

    通过后在同一事务内：
    - 各来源批次余量按“原投料量”返还（来源即使已隔离也照常返还，返还不会
      超过其历史累计消耗，余量恒不超过初始量）；
    - 生产单标记 is_cancelled、记录撤销原因与时间；产出批次保留、编号不得复用，
      但其投料边不再进入有效谱系/疑似传播，且不得再投料；
    - 边失效后按“自身疑似或任一（有效）祖先疑似”重算产出隔离位：撤销后产出
      已无有效祖先，自身又非疑似，因此解除其连带隔离（不触碰任何其它批次）。

    并发：与污染标记/复核一致先取 QC 串行化锁（PG 为 batches 表
    SHARE ROW EXCLUSIVE，SQLite 为 BEGIN IMMEDIATE 全库写锁），再按编号
    升序对“产出 + 各来源”统一加行锁，使撤销 / 投料 / 污染标记 / 复核的
    任意并发都等价于某一串行顺序，不会超用余量或误放行。
    """
    _acquire_qc_lock(session)

    # 产出行（FOR UPDATE）；不存在直接 404
    output_batch = _lock_batch(session, output_no)
    if output_batch is None:
        raise errors.batch_not_found(output_no)

    production = _load_production_for_output(session, output_no)
    if production is None:
        # 编号存在但是原料批次：没有可撤销的生产单
        raise errors.production_not_found(output_no)

    # 重复撤销优先、明确拒绝（不改库存）
    if production.is_cancelled:
        raise errors.production_already_cancelled(output_no)

    # 从未被后续生产投料：按保留的原始投料记录严格判定
    # （下游单据即便已撤销，其投料行仍保留，因此这里不排除 is_cancelled）
    consumer_rows = session.execute(
        select(Production.output_no)
        .join(ProductionInput, ProductionInput.production_id == Production.id)
        .where(ProductionInput.source_no == output_no)
    ).all()
    consumer_nos = sorted({row[0] for row in consumer_rows})
    if consumer_nos:
        raise errors.output_already_consumed(output_no, consumer_nos)

    # 产出一旦被分装（即使子批次尚未投料）即视为曾被使用，拒绝撤销：
    # 分装边已进入谱系/隔离传播，撤销生产单会令来源返还与父子关系自相矛盾。
    split_rows = session.execute(
        select(BatchSplit.child_no).where(BatchSplit.parent_no == output_no)
    ).all()
    child_nos = sorted({row[0] for row in split_rows})
    if child_nos:
        raise errors.output_already_split(output_no, child_nos)

    # 产出当前非疑似（仅因疑似祖先被隔离不阻止撤销）
    if output_batch.is_suspect:
        raise errors.output_is_suspect(output_no)

    # 产出无复核记录
    review = session.execute(
        select(BatchReview).where(BatchReview.batch_no == output_no)
    ).scalar_one_or_none()
    if review is not None:
        raise errors.production_has_review(output_no)

    # 统一按编号升序锁定来源批次（产出已锁），与并发投料/撤销保持一致加锁顺序
    source_nos = sorted({pi.source_no for pi in production.inputs})
    locked_sources = _lock_batches_in_order(session, source_nos)

    cancelled_at = datetime.now(timezone.utc)
    returned: list[dict[str, Any]] = []
    for pi in sorted(production.inputs, key=lambda x: x.id):
        source = locked_sources.get(pi.source_no)
        if source is None:
            # 理论不可达：来源批次必然存在（生产创建时外键约束）
            raise errors.batch_not_found(pi.source_no)
        # 即使来源批次已隔离，也在本事务内返还其原投料量
        source.remaining_qty += pi.qty
        # 库位层按“原消耗库位”逐笔返还；库位核算上线前的历史投料没有
        # 库位明细，按“现有余量归默认库位”的口径返还到默认库位
        parts = [(p.location_no, p.qty) for p in pi.location_parts]
        if not parts:
            parts = [(DEFAULT_LOCATION_NO, pi.qty)]
        for loc, qty in parts:
            _credit_location(session, pi.source_no, loc, qty)
        returned.append(
            {"batch_no": pi.source_no, "returned_quantity": pi.qty}
        )

    # 作废生产单：保留行与原单据，仅置撤销标记/原因/时间
    production.is_cancelled = True
    production.cancel_reason = data.cancel_reason
    production.cancelled_at = cancelled_at

    # 投料边失效后重算产出隔离位：自身非疑似且已无有效祖先 -> 解除连带隔离。
    # 显式 flush 让后续原始 SQL 谓词读到本事务刚撤销的边。
    session.flush()
    should_quarantine = bool(
        session.execute(
            text(_ANCESTOR_SUSPECT_SQL), {"root": output_no}
        ).scalar_one()
    )
    output_batch.is_quarantined = should_quarantine
    session.flush()

    return {
        "output_batch_no": output_no,
        "status": "CANCELLED",
        "cancel_reason": data.cancel_reason,
        "cancelled_at": cancelled_at.isoformat(),
        "returned_inputs": [
            {
                "batch_no": item["batch_no"],
                "returned_quantity": float(item["returned_quantity"]),
            }
            for item in returned
        ],
    }


# ---------------------------------------------------------------- 谱系与污染

# 边方向：parent -> child（父批次指向子批次），谱系由两类边共同构成：
# - PRODUCTION：source -> output（生产投料边）
# - SPLIT：父批次 -> 子批次（分装边，子批次可独立投料）
# 已撤销生产单的投料边不再进入有效谱系查询，也不参与疑似隔离传播：
# 撤销后产出作废、来源投料已在同一事务返还，该笔单据视为从未发生。
# 分装边不撤销（撤销只针对生产单据），始终参与谱系遍历与隔离传播。
_EDGE_CTE = """
SELECT pi.source_no AS parent_no, p.output_no AS child_no, 'PRODUCTION' AS kind
FROM production_inputs pi
JOIN productions p ON p.id = pi.production_id
WHERE p.is_cancelled = FALSE
UNION ALL
SELECT bs.parent_no, bs.child_no, 'SPLIT'
FROM batch_splits bs
"""


def _descendant_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部后代（直接/间接使用该批次的产出/子批次），带层数。

    谱系是允许汇聚的 DAG（混料、分装都可能使同一后代经多条路径到达），
    递归按 (batch_no, depth) 去重，最终每个批次只保留最小深度，并按
    (depth, batch_no) 稳定排序。
    """
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.child_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.parent_no = w.batch_no
    )
    SELECT batch_no, MIN(depth) AS depth
    FROM walk
    WHERE depth > 0
    GROUP BY batch_no
    ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _ancestor_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部祖先（直接/间接被其消耗的原料/父批次），带层数。

    同一祖先可能经投料边与分装边多条路径到达，按批次取最小深度去重。
    """
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.parent_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.child_no = w.batch_no
    )
    SELECT batch_no, MIN(depth) AS depth
    FROM walk
    WHERE depth > 0
    GROUP BY batch_no
    ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _load_batches_map(session: Session, nos: list[str]) -> dict[str, Batch]:
    if not nos:
        return {}
    rows = session.execute(select(Batch).where(Batch.batch_no.in_(nos))).scalars().all()
    return {b.batch_no: b for b in rows}


def _lock_descendant_closure(
    session: Session, root_no: str
) -> tuple[Batch | None, list[str], dict[str, Batch]]:
    """在固定点谱系上加载 root 及其全部后代。

    调用方必须先经 ``_acquire_qc_lock`` 与并发生产事务串行化，因此此处读到的
    谱系边与批次状态在本事务内稳定（PG 上表锁排除了并发投料，SQLite 上
    BEGIN IMMEDIATE 全库串行），不存在边被并发追加的问题。

    返回 (root 批次[可能不存在], 后代编号[depth 序], 批次映射)。
    """
    root = _lock_batch(session, root_no)
    if root is None:
        return None, [], {}

    rows = _descendant_rows(session, root_no)
    descendant_nos = [r["batch_no"] for r in rows]
    targets = _load_batches_map(session, descendant_nos)
    return root, descendant_nos, {root_no: root, **targets}


# 判断给定批次是否应隔离（自身疑似或任一祖先疑似）：
# 祖先部分以目标批次为根沿 source -> output 反向递归（严格祖先），
# 再并上目标自身；谱系边以同事务内可见数据为准，谱系为无环 DAG。
_ANCESTOR_SUSPECT_SQL = f"""
WITH RECURSIVE anc(batch_no) AS (
    SELECT e.parent_no
    FROM ({_EDGE_CTE}) e
    WHERE e.child_no = :root
    UNION
    SELECT e.parent_no FROM anc a
    JOIN ({_EDGE_CTE}) e ON e.child_no = a.batch_no
)
SELECT EXISTS(
    SELECT 1 FROM batches s
    WHERE s.is_suspect AND (
        s.batch_no = :root OR s.batch_no IN (SELECT batch_no FROM anc)
    )
)
"""


def _suspect_closure_map(
    session: Session, batch_nos: list[str]
) -> dict[str, bool]:
    """对每个目标批次求：自身疑似，或任一祖先疑似（是否应隔离）。"""
    return {
        no: bool(session.execute(text(_ANCESTOR_SUSPECT_SQL), {"root": no}).scalar_one())
        for no in batch_nos
    }


def lineage(session: Session, batch_no: str) -> dict[str, Any]:
    root = session.get(Batch, batch_no)
    if root is None:
        raise errors.batch_not_found(batch_no)

    up_rows = _ancestor_rows(session, batch_no)
    down_rows = _descendant_rows(session, batch_no)

    up_map = _load_batches_map(session, [r["batch_no"] for r in up_rows])
    down_map = _load_batches_map(session, [r["batch_no"] for r in down_rows])

    def node(r: dict[str, Any], b: Batch) -> dict[str, Any]:
        return {
            "batch_no": b.batch_no,
            "unit": b.unit,
            "remaining_qty": b.remaining_qty,
            "is_suspect": b.is_suspect,
            "is_quarantined": b.is_quarantined,
            "is_cancelled": _is_cancelled_output(session, b.batch_no),
            "depth": r["depth"],
        }

    upstream = [node(r, up_map[r["batch_no"]]) for r in up_rows if r["batch_no"] in up_map]
    downstream = [
        node(r, down_map[r["batch_no"]]) for r in down_rows if r["batch_no"] in down_map
    ]

    involved = set(up_map) | set(down_map) | {batch_no}
    edges = session.execute(
        select(ProductionInput.source_no, Production.output_no)
        .join(Production, Production.id == ProductionInput.production_id)
        .where(
            ProductionInput.source_no.in_(involved),
            Production.output_no.in_(involved),
            # 撤销的投料边不再进入有效谱系查询
            Production.is_cancelled.is_(False),
        )
    ).all()
    edge_list = [
        {"from": s, "to": o, "kind": "PRODUCTION"} for s, o in edges
    ]
    # 分装边与投料边一起进入谱系
    split_edges = session.execute(
        select(BatchSplit.parent_no, BatchSplit.child_no).where(
            BatchSplit.parent_no.in_(involved),
            BatchSplit.child_no.in_(involved),
        )
    ).all()
    edge_list.extend(
        {"from": p, "to": c, "kind": "SPLIT"} for p, c in split_edges
    )
    return {
        "batch_no": batch_no,
        "is_cancelled": _is_cancelled_output(session, batch_no),
        "upstream": upstream,
        "downstream": downstream,
        "edges": edge_list,
    }


def mark_suspect(session: Session, data: SuspectIn) -> dict[str, Any]:
    """标记批次疑似污染，并隔离其全部后代产出批次。

    混料规则：一个产出若混有受污染来源（直接或间接），该产出整体隔离；
    隔离批次不得再投料（提交生产时拦截）。

    并发：先取 QC 串行化锁（PG 表锁 / SQLite 全库写锁），再读取后代闭包
    并写状态，与生产、复核事务整体等价于某一串行顺序。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    previously_quarantined = {no for no, b in locked.items() if b.is_quarantined}

    batch.is_suspect = True
    batch.is_quarantined = True
    if data.note:
        batch.note = data.note

    for no in descendant_nos:
        target = locked[no]
        target.is_quarantined = True

    session.flush()
    newly = sorted(set(descendant_nos) - previously_quarantined)
    return {
        "source": data.batch_no,
        "is_suspect": True,
        "affected_batch_nos": descendant_nos,
        "newly_quarantined": newly,
    }


def review_suspect(session: Session, data: ReviewIn) -> dict[str, Any]:
    """质检复核确认误报：撤销批次疑似标记，并重算自身及全部后代的隔离状态。

    规则：
    - 只接受当前仍疑似的批次；编号不存在 -> SOURCE_NOT_FOUND(404)，
      已撤销或从未疑似 -> BATCH_NOT_SUSPECT(409)，均不改变任何状态；
    - 重算范围为该批次及其全部后代：自身或任一祖先仍疑似则继续隔离，否则解除；
    - 混料后代若还有另一条疑似来源（祖先），不得因本来源撤销而解除隔离。

    并发：先取 QC 串行化锁，再按与 mark_suspect 相同的固定点闭包读取，
    与生产事务的来源行锁互斥，保证复核 / 污染标记 / 投料并发时等价于
    某一串行顺序；隔离批次在任一串行结局下都不会被投料。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)
    if not batch.is_suspect:
        raise errors.batch_not_suspect(data.batch_no)

    targets = [data.batch_no, *descendant_nos]

    # 撤销自身疑似标记并固化复核说明（同一批次可能经历 标记→撤销→再标记→再撤销，
    # 复用已有复核行更新说明，避免唯一约束冲突）
    batch.is_suspect = False
    review = session.execute(
        select(BatchReview).where(BatchReview.batch_no == data.batch_no)
    ).scalar_one_or_none()
    if review is None:
        session.add(
            BatchReview(batch_no=data.batch_no, review_note=data.review_note)
        )
    else:
        review.review_note = data.review_note
        review.reviewed_at = func.now()

    # 重算：自身疑似或任一祖先疑似 -> 隔离；否则解除。
    # 注意：session.execute(text()) 不会触发 ORM autoflush，必须显式 flush，
    # 让下面的原始 SQL 谓词读到本事务刚撤销的 is_suspect 状态。
    session.flush()
    should_quarantine = _suspect_closure_map(session, targets)

    released: list[str] = []
    still: list[str] = []
    for no in targets:
        target = locked[no]
        quarantined = should_quarantine.get(no, False)
        if target.is_quarantined and not quarantined:
            released.append(no)
        target.is_quarantined = quarantined
        if quarantined:
            still.append(no)

    session.flush()
    released.sort()
    # still 中的第一个即复核批次自身（撤销后通常已解除；仍疑似祖先时会保留）
    still.sort()
    return {
        "reviewed_batch_no": data.batch_no,
        "is_suspect": False,
        "review_note": data.review_note,
        "affected_batch_nos": descendant_nos,
        "released_batch_nos": released,
        "still_quarantined": still,
    }
