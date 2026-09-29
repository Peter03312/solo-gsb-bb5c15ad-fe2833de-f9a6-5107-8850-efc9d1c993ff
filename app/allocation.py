"""库存批次 FEFO 分配核心逻辑。

规则：
- 批次按失效日期从早到晚排序，同日按批次号字典序排序（FEFO）。
- 订单按请求中的顺序依次满足，一个订单可拆分到多个批次。
- 任一订单无法足量满足时整单失败：不提交任何部分分配。

本模块只处理已解析为 Python 内建类型的载荷，不依赖 Web 框架，
校验全部失败统一抛 AllocationError（API 层转换为 HTTP 400）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any


class AllocationError(ValueError):
    """请求级错误：携带机器可读的 code 与面向调用方的 message。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Batch:
    batch_no: str
    available: float
    expiry: date
    # 保留批次在请求中的原始顺序，便于稳定输出
    index: int


def parse_expiry(value: Any) -> date:
    """严格解析 YYYY-MM-DD 失效日期。

    拒绝非字符串、非 YYYY-MM-DD 形态（如 2026/03/01、2026-3-1）
    以及不存在的日历日（如 2026-02-30）。
    """
    if not isinstance(value, str):
        raise AllocationError(
            "invalid_date",
            f"expiry date must be a YYYY-MM-DD string, got {type(value).__name__}",
        )
    parts = value.split("-")
    if (
        len(parts) != 3
        or len(parts[0]) != 4
        or len(parts[1]) != 2
        or len(parts[2]) != 2
        or not all(p.isdigit() for p in parts)
    ):
        raise AllocationError(
            "invalid_date", f"invalid expiry date {value!r}: expected YYYY-MM-DD"
        )
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise AllocationError(
            "invalid_date", f"invalid expiry date {value!r}: not a real calendar date"
        ) from None


def parse_quantity(value: Any, field: str) -> float:
    """解析数量：必须是正整数 / 正有限小数，拒绝布尔值、字符串、非正数、NaN/Inf。"""
    # bool 是 int 的子类，需显式排除
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AllocationError(
            "invalid_quantity",
            f"{field} must be a positive number, got {value!r}",
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise AllocationError(
            "invalid_quantity", f"{field} must be a positive number, got {value!r}"
        )
    return number


def parse_batch(raw: Any, index: int) -> Batch:
    if not isinstance(raw, dict):
        raise AllocationError(
            "invalid_request", f"batches[{index}] must be an object"
        )
    for key in ("batch_no", "available", "expiry"):
        if key not in raw:
            raise AllocationError(
                "invalid_request", f"batches[{index}] missing field {key!r}"
            )
    batch_no = raw["batch_no"]
    if not isinstance(batch_no, str) or not batch_no.strip():
        raise AllocationError(
            "invalid_request",
            f"batches[{index}].batch_no must be a non-empty string",
        )
    return Batch(
        batch_no=batch_no,
        available=parse_quantity(raw["available"], f"batches[{index}].available"),
        expiry=parse_expiry(raw["expiry"]),
        index=index,
    )


def parse_order(raw: Any, index: int) -> tuple[str, float]:
    if not isinstance(raw, dict):
        raise AllocationError(
            "invalid_request", f"orders[{index}] must be an object"
        )
    for key in ("order_no", "quantity"):
        if key not in raw:
            raise AllocationError(
                "invalid_request", f"orders[{index}] missing field {key!r}"
            )
    order_no = raw["order_no"]
    if not isinstance(order_no, str) or not order_no.strip():
        raise AllocationError(
            "invalid_request",
            f"orders[{index}].order_no must be a non-empty string",
        )
    return order_no, parse_quantity(raw["quantity"], f"orders[{index}].quantity")


def allocate(payload: Any) -> dict[str, Any]:
    """校验并执行整批分配，返回可直接序列化的结果。

    成功返回:
        {
          "allocations": [{"order_no", "total", "lines": [{"batch_no", "quantity", "expiry"}]}],
          "remaining": [{"batch_no", "expiry", "remaining"}],
          "total_allocated": ...,
        }
    任何校验或库存问题抛 AllocationError，调用方不得据此提交部分分配。
    """
    if not isinstance(payload, dict):
        raise AllocationError(
            "invalid_request", "request body must be a JSON object"
        )
    if "batches" not in payload or "orders" not in payload:
        raise AllocationError(
            "invalid_request", "request body must contain 'batches' and 'orders'"
        )
    raw_batches, raw_orders = payload["batches"], payload["orders"]
    if not isinstance(raw_batches, list) or not raw_batches:
        raise AllocationError(
            "invalid_request", "'batches' must be a non-empty array"
        )
    if not isinstance(raw_orders, list) or not raw_orders:
        raise AllocationError(
            "invalid_request", "'orders' must be a non-empty array"
        )

    batches = [parse_batch(raw, i) for i, raw in enumerate(raw_batches)]
    orders = [parse_order(raw, i) for i, raw in enumerate(raw_orders)]

    seen_batches: set[str] = set()
    for batch in batches:
        if batch.batch_no in seen_batches:
            raise AllocationError(
                "duplicate_batch", f"duplicate batch_no: {batch.batch_no!r}"
            )
        seen_batches.add(batch.batch_no)

    seen_orders: set[str] = set()
    for order_no, _ in orders:
        if order_no in seen_orders:
            raise AllocationError(
                "duplicate_order", f"duplicate order_no: {order_no!r}"
            )
        seen_orders.add(order_no)

    total_demand = sum(qty for _, qty in orders)
    total_stock = sum(b.available for b in batches)
    if total_demand > total_stock:
        raise AllocationError(
            "insufficient_stock",
            f"insufficient stock: demand {_num(total_demand)} "
            f"exceeds available {_num(total_stock)}",
        )

    # FEFO 顺序：失效日期升序，同日按批次号字典序
    order_queue = sorted(
        batches, key=lambda b: (b.expiry.isoformat(), b.batch_no)
    )
    stock = {b.batch_no: b.available for b in batches}
    expiry_of = {b.batch_no: b.expiry for b in batches}

    allocations: list[dict[str, Any]] = []
    for order_no, demand in orders:
        remaining_demand = demand
        lines: list[dict[str, Any]] = []
        # 库存已在总额上保证充足；逐单再防御一次，绝不留下未足额订单
        for batch in order_queue:
            if remaining_demand <= 0:
                break
            on_hand = stock[batch.batch_no]
            if on_hand <= 0:
                continue
            take = min(on_hand, remaining_demand)
            stock[batch.batch_no] = on_hand - take
            remaining_demand -= take
            lines.append(
                {
                    "batch_no": batch.batch_no,
                    "quantity": _num(take),
                    "expiry": batch.expiry.isoformat(),
                }
            )
        if remaining_demand > 0:
            raise AllocationError(
                "insufficient_stock",
                f"insufficient stock for order {order_no!r}: "
                f"short by {_num(remaining_demand)}",
            )
        allocations.append(
            {"order_no": order_no, "total": _num(demand), "lines": lines}
        )

    remaining = [
        {
            "batch_no": b.batch_no,
            "expiry": b.expiry.isoformat(),
            "remaining": _num(stock[b.batch_no]),
        }
        for b in order_queue
    ]
    return {
        "allocations": allocations,
        "remaining": remaining,
        "total_allocated": _num(total_demand),
    }


def _num(value: float) -> float | int:
    """整数值的浮点数量输出为 int，其余原样保留。"""
    if float(value).is_integer():
        return int(value)
    return value
