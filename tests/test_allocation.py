"""FEFO 批次分配：领域逻辑测试。"""

import pytest

from app.allocation import AllocationError, allocate


def make_payload():
    return {
        "batches": [
            {"batch_no": "B-003", "available": 100, "expiry": "2027-06-30"},
            {"batch_no": "B-001", "available": 40, "expiry": "2026-05-01"},
            {"batch_no": "B-002", "available": 30, "expiry": "2026-05-01"},
        ],
        "orders": [
            {"order_no": "SO-1", "quantity": 80},
            {"order_no": "SO-2", "quantity": 20},
        ],
    }


def test_fefo_order_same_expiry_lexicographic():
    result = allocate(make_payload())
    so1, so2 = result["allocations"]
    # 同日 B-001 / B-002 字典序，SO-1=80：40 全取 B-001，再取 30 B-002，最后 10 取 B-003
    assert so1["lines"] == [
        {"batch_no": "B-001", "quantity": 40, "expiry": "2026-05-01"},
        {"batch_no": "B-002", "quantity": 30, "expiry": "2026-05-01"},
        {"batch_no": "B-003", "quantity": 10, "expiry": "2027-06-30"},
    ]
    assert so1["total"] == 80
    # SO-2=20 只能从 B-003 剩余 90 中取
    assert so2["lines"] == [
        {"batch_no": "B-003", "quantity": 20, "expiry": "2027-06-30"}
    ]


def test_remaining_and_total():
    result = allocate(make_payload())
    assert result["total_allocated"] == 100
    assert result["remaining"] == [
        {"batch_no": "B-001", "expiry": "2026-05-01", "remaining": 0},
        {"batch_no": "B-002", "expiry": "2026-05-01", "remaining": 0},
        {"batch_no": "B-003", "expiry": "2027-06-30", "remaining": 70},
    ]


def test_exact_stock_boundary():
    payload = {
        "batches": [
            {"batch_no": "B-001", "available": 5, "expiry": "2026-05-01"},
            {"batch_no": "B-002", "available": 5, "expiry": "2026-05-02"},
        ],
        "orders": [{"order_no": "SO-1", "quantity": 10}],
    }
    result = allocate(payload)
    assert result["remaining"][-1]["remaining"] == 0
    assert result["total_allocated"] == 10


def test_insufficient_stock_rejected():
    payload = make_payload()
    payload["orders"][1]["quantity"] = 999
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "insufficient_stock"


def test_non_positive_quantity_rejected():
    payload = make_payload()
    payload["batches"][0]["available"] = 0
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "invalid_quantity"


def test_boolean_quantity_rejected():
    payload = make_payload()
    payload["orders"][0]["quantity"] = True
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "invalid_quantity"


def test_duplicate_batch_rejected():
    payload = make_payload()
    payload["batches"][1]["batch_no"] = "B-003"
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "duplicate_batch"


def test_duplicate_order_rejected():
    payload = make_payload()
    payload["orders"][1]["order_no"] = "SO-1"
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "duplicate_order"


@pytest.mark.parametrize("bad", ["2026/05/01", "2026-5-1", "2026-02-30", "not-a-date", 20260501])
def test_bad_expiry_rejected(bad):
    payload = make_payload()
    payload["batches"][1]["expiry"] = bad
    with pytest.raises(AllocationError) as exc:
        allocate(payload)
    assert exc.value.code == "invalid_date"


def test_decimal_quantities():
    payload = {
        "batches": [
            {"batch_no": "B-001", "available": 1.5, "expiry": "2026-05-01"},
            {"batch_no": "B-002", "available": 2.25, "expiry": "2026-05-02"},
        ],
        "orders": [{"order_no": "SO-1", "quantity": 2.0}],
    }
    result = allocate(payload)
    lines = result["allocations"][0]["lines"]
    assert lines == [
        {"batch_no": "B-001", "quantity": 1.5, "expiry": "2026-05-01"},
        {"batch_no": "B-002", "quantity": 0.5, "expiry": "2026-05-02"},
    ]
