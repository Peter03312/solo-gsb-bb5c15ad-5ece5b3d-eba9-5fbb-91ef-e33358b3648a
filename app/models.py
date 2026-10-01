"""ORM 模型：批次、库位余额、生产单、投料明细、调拨单、幂等记录。"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Numeric 精度：整数 18 位、小数 4 位，满足食品厂计量场景
QTY = Numeric(18, 4)

# 默认库位编号：既有批次余量与新登记批次/产出/子批次的数量都先归入该库位。
# 默认库位不参与任何特殊排序——未指定库位的扣减一律按库位编号升序进行。
DEFAULT_LOCATION_NO = "DEFAULT"


class Batch(Base):
    """批次：既可能是原料批次，也可能是生产产出批次。"""

    __tablename__ = "batches"

    batch_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    initial_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    remaining_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    # 被人工/检测标记为疑似污染的批次
    is_suspect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # 隔离中：自身疑似，或谱系上有疑似祖先；隔离批次禁止再投料
    is_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="source_batch", foreign_keys="ProductionInput.source_no"
    )
    locations: Mapped[list["BatchLocation"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("initial_qty > 0", name="ck_batch_initial_qty_positive"),
        CheckConstraint("remaining_qty >= 0", name="ck_batch_remaining_non_negative"),
        CheckConstraint("remaining_qty <= initial_qty", name="ck_batch_remaining_within"),
    )


class BatchLocation(Base):
    """批次在某个库位上的余量（两层库存核算的库位层）。

    不变式：同一批次所有库位行 qty 之和恒等于 batches.remaining_qty，
    两层余额的每一次变动都在同一事务内完成。库位行一旦建立即保留
    （扣到 0 也保留，便于审计该批次曾存放于哪些库位）。
    """

    __tablename__ = "batch_locations"

    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), primary_key=True
    )
    location_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    batch: Mapped[Batch] = relationship(back_populates="locations")

    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_batch_location_qty_non_negative"),
    )


class Production(Base):
    """一次生产：多笔投入 -> 一个产出批次。"""

    __tablename__ = "productions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    output_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    output_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 撤销（作废）：录入错误的生产单可整单撤销。撤销后行保留（原单据可查），
    # 但其投料边不再进入有效谱系/疑似传播，产出批次不得再投料。
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="production", cascade="all, delete-orphan"
    )
    output: Mapped[Batch] = relationship(foreign_keys=[output_no])

    __table_args__ = (
        CheckConstraint("output_qty > 0", name="ck_production_output_positive"),
    )


class ProductionInput(Base):
    """生产单中的一笔原料消耗。"""

    __tablename__ = "production_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    production_id: Mapped[int] = mapped_column(
        ForeignKey("productions.id", ondelete="CASCADE"), nullable=False
    )
    source_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    production: Mapped[Production] = relationship(back_populates="inputs")
    source_batch: Mapped[Batch] = relationship(
        back_populates="inputs", foreign_keys=[source_no]
    )
    location_parts: Mapped[list["ProductionInputLocation"]] = relationship(
        back_populates="input", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("production_id", "source_no", name="uq_input_prod_source"),
        CheckConstraint("qty > 0", name="ck_production_input_qty_positive"),
        Index("ix_prod_input_source", "source_no"),
    )


class ProductionInputLocation(Base):
    """一笔投料的库位扣减明细：该笔投料从哪些库位各扣了多少。

    未指定库位的投料可能按库位编号顺序跨多个库位扣减，逐笔记录后，
    生产单撤销才能“按原消耗库位”把数量返还回各自库位。各明细 qty
    之和恒等于所属 production_inputs.qty。
    """

    __tablename__ = "production_input_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    input_id: Mapped[int] = mapped_column(
        ForeignKey("production_inputs.id", ondelete="CASCADE"), nullable=False
    )
    location_no: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    input: Mapped[ProductionInput] = relationship(back_populates="location_parts")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_input_location_qty_positive"),
        Index("ix_input_location_input", "input_id"),
    )


class BatchSplit(Base):
    """批次分装关系：父批次 -> 一个可独立投料的子批次。

    分装只拆分余量、不产生生产单：父批次余量按 qty 扣减，子批次继承父批次
    单位、初始/当前余量均为 qty。子批次与普通批次一样可作为生产来源，也可
    再次分装。分装关系与投料关系一样进入谱系遍历与疑似隔离传播。
    """

    __tablename__ = "batch_splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    # 一个子批次只能由一次分装创建（批次编号全局唯一、不得复用）
    child_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    parent: Mapped[Batch] = relationship(foreign_keys=[parent_no])
    child: Mapped[Batch] = relationship(foreign_keys=[child_no])

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_batch_split_qty_positive"),
        Index("ix_batch_split_parent", "parent_no"),
    )


class BatchReview(Base):
    """质检复核记录：确认某疑似批次为误报后撤销其疑似标记。

    每个批次至多一条有效复核；被复核批次必须当时处于疑似状态。
    复核只撤销“自身疑似”，隔离状态随后按谱系重算（混料来源仍疑似的批次继续隔离）。
    """

    __tablename__ = "batch_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False, unique=True
    )
    review_note: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[Batch] = relationship(foreign_keys=[batch_no])


class Transfer(Base):
    """库位调拨单：同一批次的数量在两个库位之间移动。

    调拨只改变库位层余额（来源库位减、目标库位加），批次全局余量不变；
    调拨单号全局唯一、不得复用。隔离批次允许调拨（调拨不是投料/分装）。
    """

    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_no: Mapped[str] = mapped_column(
        String(64), ForeignKey("batches.batch_no"), nullable=False
    )
    from_location: Mapped[str] = mapped_column(String(64), nullable=False)
    to_location: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch: Mapped[Batch] = relationship(foreign_keys=[batch_no])

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfer_qty_positive"),
        CheckConstraint(
            "from_location <> to_location", name="ck_transfer_distinct_locations"
        ),
        Index("ix_transfer_batch", "batch_no"),
    )


class IdempotencyKey(Base):
    """请求幂等记录。

    同一 (method, path, key) 只允许一种请求体（sha256）；
    成功后固化状态码与响应体，重试原样返回，异内容报冲突。
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("method", "path", "key", name="uq_idem_endpoint_key"),
    )
