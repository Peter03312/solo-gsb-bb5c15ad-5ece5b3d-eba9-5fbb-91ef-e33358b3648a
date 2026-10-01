"""库位核算：默认库位、调拨、库位余额查询、按库位投料/分装、撤销按原库位返还。"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty), "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    """inputs: [(batch_no, qty)] 或 [(batch_no, qty, location)]"""
    from decimal import Decimal

    total = sum((Decimal(str(item[1])) for item in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [
                {"batch_no": item[0], "quantity": str(item[1]), **(
                    {"location": item[2]} if len(item) > 2 else {}
                )}
                for item in inputs
            ],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _split(client, parent, targets, key, location=None):
    body = {"splits": [{"batch_no": no, "quantity": str(q)} for no, q in targets]}
    if location:
        body["location"] = location
    return client.post(
        f"/batches/{parent}/splits",
        json=body,
        headers={"Idempotency-Key": key},
    )


def _transfer(client, transfer_no, batch_no, from_loc, to_loc, qty, key=None):
    return client.post(
        "/transfers",
        json={
            "transfer_no": transfer_no,
            "batch_no": batch_no,
            "from_location": from_loc,
            "to_location": to_loc,
            "quantity": str(qty),
        },
        headers={"Idempotency-Key": key or f"key-{transfer_no}"},
    )


def _locations(client, batch_no):
    r = client.get(f"/batches/{batch_no}/locations")
    assert r.status_code == 200, r.text
    return r.json()


def _loc_map(client, batch_no):
    body = _locations(client, batch_no)
    return {row["location_no"]: row["qty"] for row in body["locations"]}


def _assert_two_level_consistent(client, batch_no):
    """库位余额之和必须等于全局余量。"""
    body = _locations(client, batch_no)
    total = sum(row["qty"] for row in body["locations"])
    assert abs(total - body["remaining_qty"]) < 1e-9, body
    d = client.get(f"/batches/{batch_no}").json()
    assert body["remaining_qty"] == d["remaining_qty"]


# --------------------------------------------------------------- 默认库位


def test_new_batch_goes_to_default_location(client):
    assert _raw(client, "RM-1", "100").status_code == 201
    body = _locations(client, "RM-1")
    assert body["batch_no"] == "RM-1"
    assert body["remaining_qty"] == 100.0
    assert body["locations"] == [{"location_no": "DEFAULT", "qty": 100.0}]
    _assert_two_level_consistent(client, "RM-1")


def test_locations_of_unknown_batch_404(client):
    r = client.get("/batches/GHOST/locations")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


# --------------------------------------------------------------- 调拨主流程


def test_transfer_moves_between_locations(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "30")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {
        "transfer_no": "TR-1",
        "batch_no": "RM-1",
        "from_location": "DEFAULT",
        "to_location": "L-01",
        "quantity": 30.0,
        "from_remaining_qty": 70.0,
        "to_remaining_qty": 30.0,
    }
    # 全局余量不变，库位层同步反映
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 30.0}
    _assert_two_level_consistent(client, "RM-1")

    # 同向再调拨：目标库位累加
    r = _transfer(client, "TR-2", "RM-1", "DEFAULT", "L-01", "20")
    assert r.status_code == 201
    assert r.json()["to_remaining_qty"] == 50.0
    assert _loc_map(client, "RM-1") == {"DEFAULT": 50.0, "L-01": 50.0}


def test_transfer_to_same_location_rejected(client):
    _raw(client, "RM-1", "100")
    r = _transfer(client, "TR-1", "RM-1", "DEFAULT", "DEFAULT", "10")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SAME_LOCATION"
    # 余额不变
    assert _loc_map(client, "RM-1") == {"DEFAULT": 100.0}


def test_transfer_empty_or_blank_location_rejected(client):
    _raw(client, "RM-1", "100")
    for from_loc, to_loc in (("", "L-01"), ("DEFAULT", "  ")):
        r = client.post(
            "/transfers",
            json={
                "transfer_no": "TR-X",
                "batch_no": "RM-1",
                "from_location": from_loc,
                "to_location": to_loc,
                "quantity": "10",
            },
            headers={"Idempotency-Key": f"k-{from_loc}-{to_loc}"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _loc_map(client, "RM-1") == {"DEFAULT": 100.0}


def test_transfer_unknown_batch_rejected(client):
    r = _transfer(client, "TR-1", "GHOST", "DEFAULT", "L-01", "10")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_transfer_insufficient_source_rejected_without_change(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "30")
    # 来源库位不足（L-01 只有 30）
    r = _transfer(client, "TR-2", "RM-1", "L-01", "L-02", "31")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    # 从未存放过的库位余额为 0，同样不足
    r = _transfer(client, "TR-3", "RM-1", "L-09", "L-02", "1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    # 两库位余额均未改变
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 30.0}


def test_transfer_no_reuse_rejected(client):
    _raw(client, "RM-1", "100")
    assert _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "10").status_code == 201
    # 不同幂等键复用同一调拨单号：拒绝
    r = _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-02", "5", key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSFER_NO_REUSED"
    assert _loc_map(client, "RM-1") == {"DEFAULT": 90.0, "L-01": 10.0}


def test_transfer_non_positive_qty_rejected(client):
    _raw(client, "RM-1", "100")
    for qty in ("0", "-5"):
        r = _transfer(client, f"TR-{qty}", "RM-1", "DEFAULT", "L-01", qty)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _loc_map(client, "RM-1") == {"DEFAULT": 100.0}


def test_transfer_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/transfers",
        json={
            "transfer_no": "TR-1",
            "batch_no": "RM-1",
            "from_location": "DEFAULT",
            "to_location": "L-01",
            "quantity": "10",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _loc_map(client, "RM-1") == {"DEFAULT": 100.0}


def test_transfer_idempotent_replay_and_conflict(client):
    _raw(client, "RM-1", "100")
    payload = {
        "transfer_no": "TR-1",
        "batch_no": "RM-1",
        "from_location": "DEFAULT",
        "to_location": "L-01",
        "quantity": "40",
    }
    r1 = client.post("/transfers", json=payload, headers={"Idempotency-Key": "K1"})
    assert r1.status_code == 201
    # 同键同体重放：返回首次结果，只调拨一次
    r2 = client.post("/transfers", json=payload, headers={"Idempotency-Key": "K1"})
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert _loc_map(client, "RM-1") == {"DEFAULT": 60.0, "L-01": 40.0}

    # 同键异体：冲突
    other = dict(payload, quantity="10")
    r3 = client.post("/transfers", json=other, headers={"Idempotency-Key": "K1"})
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _loc_map(client, "RM-1") == {"DEFAULT": 60.0, "L-01": 40.0}


def test_quarantined_batch_can_be_transferred(client):
    _raw(client, "RM-1", "100")
    client.post(
        "/contamination",
        json={"batch_no": "RM-1"},
        headers={"Idempotency-Key": "C1"},
    )
    # 隔离批次可调拨
    r = _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-Q", "40")
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1") == {"DEFAULT": 60.0, "L-Q": 40.0}
    # 但仍不得投料、不得分装
    r = _produce(client, "FG-1", [("RM-1", "10")], key="p1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    r = _split(client, "RM-1", [("RM-1-A", "10")], "s1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"


# --------------------------------------------------------------- 按库位投料


def test_production_unspecified_deducts_in_location_order(client):
    _raw(client, "RM-1", "100")
    # "A-01" 字典序先于 "DEFAULT"：未指定库位时先扣 A-01
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "A-01", "30")
    r = _produce(client, "FG-1", [("RM-1", "40")], key="p1")
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1") == {"A-01": 0.0, "DEFAULT": 60.0}
    # 产出进入默认库位
    assert _loc_map(client, "FG-1") == {"DEFAULT": 40.0}
    _assert_two_level_consistent(client, "RM-1")
    _assert_two_level_consistent(client, "FG-1")


def test_production_specified_location_deducts_only_there(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "30")
    r = _produce(client, "FG-1", [("RM-1", "25", "L-01")], key="p1")
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 5.0}

    # 指定库位不足即整笔拒绝——即使全局余量足够
    r = _produce(client, "FG-2", [("RM-1", "10", "L-01")], key="p2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert client.get("/batches/FG-2").status_code == 404
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 5.0}
    _assert_two_level_consistent(client, "RM-1")


def test_production_multi_input_locations(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "40")
    r = _produce(
        client,
        "FG-1",
        [("RM-1", "50"), ("RM-2", "20", "DEFAULT")],
        out_qty="65",
        key="p1",
    )
    assert r.status_code == 201, r.text
    # RM-1 未指定库位：先 DEFAULT(60) 再 L-01... 字典序 DEFAULT < L-01
    assert _loc_map(client, "RM-1") == {"DEFAULT": 10.0, "L-01": 40.0}
    assert _loc_map(client, "RM-2") == {"DEFAULT": 30.0}
    _assert_two_level_consistent(client, "RM-1")


# --------------------------------------------------------------- 按库位分装


def test_split_unspecified_deducts_in_location_order(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "A-01", "30")
    r = _split(client, "RM-1", [("RM-1-A", "40")], "s1")
    assert r.status_code == 201, r.text
    # 先扣 A-01(30)，再扣 DEFAULT(10)
    assert _loc_map(client, "RM-1") == {"A-01": 0.0, "DEFAULT": 60.0}
    # 子批次进入默认库位
    assert _loc_map(client, "RM-1-A") == {"DEFAULT": 40.0}
    _assert_two_level_consistent(client, "RM-1")


def test_split_specified_location_only(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "30")
    r = _split(client, "RM-1", [("RM-1-A", "20")], "s1", location="L-01")
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 10.0}

    # 指定库位不足：整笔拒绝（全局余量充足也不行）
    r = _split(client, "RM-1", [("RM-1-B", "50")], "s2", location="L-01")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert client.get("/batches/RM-1-B").status_code == 404
    assert _loc_map(client, "RM-1") == {"DEFAULT": 70.0, "L-01": 10.0}


# --------------------------------------------------------------- 撤销按原库位返还


def test_cancel_returns_to_original_locations(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "A-01", "30")
    # 未指定库位投料 40：A-01 扣 30、DEFAULT 扣 10
    _produce(client, "MIX-1", [("RM-1", "40")], key="p1")
    assert _loc_map(client, "RM-1") == {"A-01": 0.0, "DEFAULT": 60.0}

    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    # 按原消耗库位返还：A-01 回到 30、DEFAULT 回到 70
    assert _loc_map(client, "RM-1") == {"A-01": 30.0, "DEFAULT": 70.0}
    _assert_two_level_consistent(client, "RM-1")


def test_cancel_returns_to_specified_location(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "40")
    _produce(client, "MIX-1", [("RM-1", "40", "L-01")], key="p1")
    assert _loc_map(client, "RM-1") == {"DEFAULT": 60.0, "L-01": 0.0}
    client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert _loc_map(client, "RM-1") == {"DEFAULT": 60.0, "L-01": 40.0}
    _assert_two_level_consistent(client, "RM-1")


# --------------------------------------------------------------- 混合操作后两层一致


def test_location_sum_matches_global_after_mixed_ops(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "80")
    _transfer(client, "TR-1", "RM-1", "DEFAULT", "L-01", "25")
    _transfer(client, "TR-2", "RM-1", "L-01", "L-02", "10")
    _split(client, "RM-1", [("RM-1-A", "20")], "s1", location="DEFAULT")
    _produce(client, "FG-1", [("RM-1", "15"), ("RM-2", "30", "DEFAULT")], key="p1")
    for no in ("RM-1", "RM-1-A", "RM-2", "FG-1"):
        _assert_two_level_consistent(client, no)
    assert _loc_map(client, "RM-1") == {
        "DEFAULT": 40.0,
        "L-01": 15.0,
        "L-02": 10.0,
    }


# --------------------------------------------------------------- 既有库回填


def test_existing_balances_backfilled_to_default_location(engine, client):
    """库位核算上线前的批次（无库位行）在启动迁移后归入默认库位。"""
    from sqlalchemy import text

    from app import db

    # 模拟历史数据：直接写 batches 行，不写 batch_locations
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO batches (batch_no, unit, initial_qty, remaining_qty,"
                " is_suspect, is_quarantined) VALUES"
                " ('LEGACY-1', 'kg', 50, 45, 0, 0),"
                " ('LEGACY-0', 'kg', 10, 0, 0, 0)"
            )
        )
    # 再次执行启动迁移（幂等回填）
    db.init_db()

    assert _loc_map(client, "LEGACY-1") == {"DEFAULT": 45.0}
    # 余量为 0 的历史批次无需库位行，两层之和仍为 0
    assert _locations(client, "LEGACY-0")["locations"] == []
    _assert_two_level_consistent(client, "LEGACY-1")
    _assert_two_level_consistent(client, "LEGACY-0")

    # 回填后的批次可正常调拨/投料
    assert _transfer(client, "TR-1", "LEGACY-1", "DEFAULT", "L-01", "5").status_code == 201
    assert _produce(client, "FG-1", [("LEGACY-1", "10")], key="p1").status_code == 201
    _assert_two_level_consistent(client, "LEGACY-1")
