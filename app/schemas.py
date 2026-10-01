"""请求/响应模型。

数量统一走 ``Decimal`` 精确计算，拒绝零、负数与 NaN/Inf；响应再序列化为数字。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("数量不能为布尔值")
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # invalid literal
        raise ValueError("数量必须是十进制数字") from exc
    if not dec.is_finite():
        raise ValueError("数量必须是有限数字")
    if dec <= 0:
        raise ValueError("数量必须为正数")
    return dec


# 正数数量：入参即完成 > 0 校验
PositiveQty = Annotated[Decimal, BeforeValidator(_to_decimal)]

BatchNo = Annotated[str, Field(min_length=1, max_length=64, examples=["RM-20260930-01"])]
Unit = Annotated[str, Field(min_length=1, max_length=16, examples=["kg"])]


def _location_not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("库位编号不得为空白")
    return value


# 库位编号：非空、非纯空白；大小写敏感，按字符串升序参与扣减排序
LocationNo = Annotated[
    str,
    Field(min_length=1, max_length=64, examples=["L-01"]),
    BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v),
    AfterValidator(_location_not_blank),
]


class RawBatchIn(BaseModel):
    batch_no: BatchNo
    unit: Unit
    quantity: PositiveQty
    note: str | None = Field(default=None, max_length=255)


class ConsumptionIn(BaseModel):
    batch_no: BatchNo
    quantity: PositiveQty
    # 可选库位：指定时仅从该库位扣减；缺省时按库位编号升序依次扣减
    location: LocationNo | None = None


class ProductionIn(BaseModel):
    output_batch_no: BatchNo
    unit: Unit
    output_quantity: PositiveQty
    inputs: list[ConsumptionIn] = Field(min_length=1)


class SuspectIn(BaseModel):
    batch_no: BatchNo
    note: str | None = Field(default=None, max_length=255)


class SplitTargetIn(BaseModel):
    """一笔分装：新子批次编号（全局不得复用）与正数分装量。"""

    batch_no: BatchNo
    quantity: PositiveQty


class SplitIn(BaseModel):
    """按父批次编号提交一笔分装：多个互异新编号 + 各自正数分装量。

    可选 location：指定时仅从父批次该库位扣减分装总量；缺省时按库位
    编号升序依次扣减。子批次数量一律进入默认库位。
    """

    splits: list[SplitTargetIn] = Field(min_length=1)
    location: LocationNo | None = None


class TransferIn(BaseModel):
    """库位调拨：凭唯一调拨单号，把某批次的正数数量在两个库位间移动。"""

    transfer_no: BatchNo
    batch_no: BatchNo
    from_location: LocationNo
    to_location: LocationNo
    quantity: PositiveQty


class ReviewIn(BaseModel):
    batch_no: BatchNo
    review_note: str = Field(min_length=1, max_length=1000, examples=["复检合格：留样复测未检出农残"])

    @field_validator("review_note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("复核说明不得为空白")
        return value


class CancelIn(BaseModel):
    # 撤销原因非空、非空白；凭产出批次编号（路径）+ 撤销原因撤销生产单
    cancel_reason: str = Field(min_length=1, max_length=1000, examples=["产出编号录入错误，实际应为 MIX-1B"])

    @field_validator("cancel_reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("撤销原因不得为空白")
        return value


# ---------- 响应 ----------


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BatchOut(_ORMModel):
    batch_no: str
    unit: str
    initial_qty: float
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    is_cancelled: bool = False
    note: str | None = None


class ConsumptionOut(_ORMModel):
    batch_no: str
    quantity: float


class ProductionOut(_ORMModel):
    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]


class ReturnedInputOut(BaseModel):
    """撤销时返还给某来源批次的一笔原投料量。"""

    batch_no: str
    returned_quantity: float


class CancellationOut(BaseModel):
    """生产单撤销结果：产出编号、返还明细、撤销状态。"""

    output_batch_no: str
    status: str = "CANCELLED"
    cancel_reason: str
    cancelled_at: str
    returned_inputs: list[ReturnedInputOut]


class ProductionRecordOut(BaseModel):
    """生产原单据（含撤销信息），供按产出编号查询。"""

    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]
    is_cancelled: bool
    cancel_reason: str | None = None
    cancelled_at: str | None = None


class NodeOut(BaseModel):
    batch_no: str
    unit: str
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    is_cancelled: bool = False
    depth: int


class LineageOut(BaseModel):
    batch_no: str
    is_cancelled: bool = False
    upstream: list[NodeOut]
    downstream: list[NodeOut]
    edges: list[dict[str, str]]


class DescendantsOut(BaseModel):
    source: str
    is_suspect: bool
    affected_batch_nos: list[str]
    newly_quarantined: list[str]


class ReviewOut(BaseModel):
    reviewed_batch_no: str
    is_suspect: bool
    review_note: str
    affected_batch_nos: list[str]
    released_batch_nos: list[str]
    still_quarantined: list[str]


class SplitBatchOut(BaseModel):
    """分装产生的子批次：编号、继承的单位与分装余量。"""

    batch_no: str
    unit: str
    quantity: float
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool


class SplitRelationOut(BaseModel):
    """分装关系：父批次 -> 子批次（分装量）。"""

    parent_batch_no: str
    child_batch_no: str
    quantity: float


class SplitOut(BaseModel):
    """分装结果：父批次新余量、子批次清单与分装关系。"""

    parent_batch_no: str
    parent_remaining_qty: float
    splits: list[SplitBatchOut]
    relations: list[SplitRelationOut]


class LocationBalanceOut(BaseModel):
    """批次在单个库位上的余量。"""

    location_no: str
    qty: float


class BatchLocationsOut(BaseModel):
    """批次的库位余额清单：各库位之和恒等于全局余量 remaining_qty。"""

    batch_no: str
    remaining_qty: float
    locations: list[LocationBalanceOut]


class TransferOut(BaseModel):
    """调拨结果：调拨单信息 + 来源/目标两个库位的新余额。"""

    transfer_no: str
    batch_no: str
    from_location: str
    to_location: str
    quantity: float
    from_remaining_qty: float
    to_remaining_qty: float
