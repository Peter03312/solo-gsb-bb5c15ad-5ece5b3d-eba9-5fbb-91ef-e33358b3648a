"""库位并发测试：调拨 / 投料 / 分装 / 撤销并发时不得超用，两层余额不得失配。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch, BatchLocation
from app.schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    TransferIn,
)


def _qty(value: str) -> Decimal:
    return Decimal(value)


def _location_map(session, batch_no: str) -> dict[str, Decimal]:
    rows = (
        session.query(BatchLocation)
        .filter(BatchLocation.batch_no == batch_no)
        .all()
    )
    return {r.location_no: r.qty for r in rows}


def _assert_two_level_consistent(session, batch_no: str) -> None:
    batch = session.get(Batch, batch_no)
    total = sum(_location_map(session, batch_no).values(), Decimal("0"))
    assert total == batch.remaining_qty, (batch_no, total, batch.remaining_qty)


def test_concurrent_transfers_never_overuse(engine):
    # DEFAULT 库位 100，4 个线程各调拨 30 到 L-01：3 成功、1 拒绝
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_location(
                    s,
                    TransferIn(
                        transfer_no=f"TR-{idx}",
                        batch_no="RM-1",
                        from_location="DEFAULT",
                        to_location="L-01",
                        quantity=_qty("30"),
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:  # 锁错误等也必须暴露，不能吞掉
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert results.count("ok") == 3, results
    assert results.count("INSUFFICIENT_BALANCE") == 1, results
    assert not [r for r in results if r.startswith("ERROR")], results
    with session_scope() as s:
        locs = _location_map(s, "RM-1")
        assert locs == {"DEFAULT": Decimal("10.0000"), "L-01": Decimal("90.0000")}
        # 调拨不动全局余量
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("100.0000")
        _assert_two_level_consistent(s, "RM-1")


def test_concurrent_transfer_and_production_same_location_never_overuse(engine):
    # DEFAULT 库位 100：调拨 60（DEFAULT->L-01）与指定 DEFAULT 投料 60 并发，
    # 恰好一个成功、另一个 INSUFFICIENT_BALANCE，两层余额始终一致
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_location(
                    s,
                    TransferIn(
                        transfer_no="TR-1",
                        batch_no="RM-1",
                        from_location="DEFAULT",
                        to_location="L-01",
                        quantity=_qty("60"),
                    ),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=_qty("60"),
                        inputs=[{"batch_no": "RM-1", "quantity": _qty("60"), "location": "DEFAULT"}],  # type: ignore[list-item]
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [transfer_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["transfer"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["produce"] in ("ok", "INSUFFICIENT_BALANCE"), results
    assert results["transfer"] != results["produce"], results  # 一成一拒

    with session_scope() as s:
        locs = _location_map(s, "RM-1")
        if results["transfer"] == "ok":
            # 调拨先：DEFAULT 40 / L-01 60，投料因 DEFAULT 不足被拒
            assert locs == {"DEFAULT": Decimal("40.0000"), "L-01": Decimal("60.0000")}
            assert s.get(Batch, "FG-1") is None
            assert s.get(Batch, "RM-1").remaining_qty == Decimal("100.0000")
        else:
            # 投料先：DEFAULT 40，FG-1 产出 60 进默认库位，调拨被拒
            assert locs == {"DEFAULT": Decimal("40.0000")}
            assert s.get(Batch, "FG-1").remaining_qty == Decimal("60.0000")
            assert s.get(Batch, "RM-1").remaining_qty == Decimal("40.0000")
            _assert_two_level_consistent(s, "FG-1")
        _assert_two_level_consistent(s, "RM-1")


def test_concurrent_productions_across_locations_keep_two_levels_matched(engine):
    # DEFAULT 60 + A-01 40（A-01 字典序在前）：6 线程各未指定库位投料 15，
    # 6 个全部成功，最终 DEFAULT 45、A-01 0，两层之和恒等
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.transfer_location(
            s,
            TransferIn(
                transfer_no="TR-SEED",
                batch_no="RM-1",
                from_location="DEFAULT",
                to_location="A-01",
                quantity=_qty("40"),
            ),
        )

    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no=f"FG-{idx}",
                        unit="kg",
                        output_quantity=_qty("15"),
                        inputs=[{"batch_no": "RM-1", "quantity": _qty("15")}],  # type: ignore[list-item]
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert results == ["ok"] * 6, results
    with session_scope() as s:
        # 先扣 A-01(40)，再扣 DEFAULT(50)
        locs = _location_map(s, "RM-1")
        assert locs == {"A-01": Decimal("0.0000"), "DEFAULT": Decimal("10.0000")}
        _assert_two_level_consistent(s, "RM-1")
        for idx in range(6):
            _assert_two_level_consistent(s, f"FG-{idx}")


def test_cancel_concurrent_with_transfer_equivalent_to_serial(engine):
    """撤销（按原库位返还 DEFAULT 100）与从 DEFAULT 调出 50 并发：

    结局 A：撤销先 -> DEFAULT 回到 100，调拨成功，最终 DEFAULT 50；
    结局 B：调拨先 -> DEFAULT 为 0 不足，调拨被拒，撤销成功，DEFAULT 100。
    两种结局都必须自洽：来源全局余量恒为 100，两层余额恒一致。
    """
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=_qty("100"),
                inputs=[{"batch_no": "RM-1", "quantity": _qty("100")}],  # type: ignore[list-item]
            ),
        )

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def cancel_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_production(
                    s, "MIX-1", CancelIn(cancel_reason="录入错误")
                )
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_location(
                    s,
                    TransferIn(
                        transfer_no="TR-1",
                        batch_no="RM-1",
                        from_location="DEFAULT",
                        to_location="L-01",
                        quantity=_qty("50"),
                    ),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [cancel_worker, transfer_worker]))

    assert results["cancel"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["transfer"] in ("ok", "INSUFFICIENT_BALANCE"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        # 撤销只发生一次：全局余量恒为 100
        assert rm1.remaining_qty == Decimal("100.0000")
        locs = _location_map(s, "RM-1")
        if results["transfer"] == "ok":
            assert locs == {"DEFAULT": Decimal("50.0000"), "L-01": Decimal("50.0000")}
        else:
            assert locs == {"DEFAULT": Decimal("100.0000")}
        _assert_two_level_consistent(s, "RM-1")
