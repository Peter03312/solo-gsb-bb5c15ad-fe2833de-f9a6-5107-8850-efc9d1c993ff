"""FEFO 批次分配：HTTP API 层测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PAYLOAD = {
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


def test_allocate_ok():
    resp = client.post("/allocate-batches", json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_allocated"] == 100
    assert [line["batch_no"] for line in body["allocations"][0]["lines"]] == [
        "B-001",
        "B-002",
        "B-003",
    ]
    assert body["remaining"][0]["remaining"] == 0
    assert body["remaining"][2]["remaining"] == 70


def test_insufficient_stock_400_and_no_partial():
    bad = {
        "batches": [
            {"batch_no": "B-001", "available": 5, "expiry": "2026-05-01"}
        ],
        "orders": [
            {"order_no": "SO-1", "quantity": 3},
            {"order_no": "SO-2", "quantity": 9},
        ],
    }
    resp = client.post("/allocate-batches", json=bad)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "insufficient_stock"
    assert "allocations" not in body


def test_non_positive_quantity_400():
    bad = {"batches": [dict(PAYLOAD["batches"][0], available=-1)],
           "orders": PAYLOAD["orders"]}
    resp = client.post("/allocate-batches", json=bad)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_quantity"


def test_duplicate_batch_400():
    dup = {
        "batches": [
            {"batch_no": "X", "available": 5, "expiry": "2026-05-01"},
            {"batch_no": "X", "available": 5, "expiry": "2026-06-01"},
        ],
        "orders": [{"order_no": "SO-1", "quantity": 5}],
    }
    resp = client.post("/allocate-batches", json=dup)
    assert resp.status_code == 400
    assert resp.json()["error"] == "duplicate_batch"


def test_bad_date_400():
    bad = {
        "batches": [
            {"batch_no": "X", "available": 5, "expiry": "2026/05/01"}
        ],
        "orders": [{"order_no": "SO-1", "quantity": 5}],
    }
    resp = client.post("/allocate-batches", json=bad)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_date"


def test_malformed_json_400():
    resp = client.post(
        "/allocate-batches",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
