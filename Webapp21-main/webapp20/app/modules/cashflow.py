# cashflow


from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Dict, Iterable, List, Tuple
from app import db

from app.models import (Cashflow, Category, CashflowSummary, Transaction,
                        TransactionKind, month_str,)


# ---------------------------------------------------------------------------
# Tiện ích Decimal
# ---------------------------------------------------------------------------

QUANT = Decimal("0.01")


@contextmanager
def _money_ctx():
    """Thiết lập ngữ cảnh số thập phân cho phép tính tiền tệ."""
    with localcontext() as ctx:
        ctx.prec = 28             # đủ chính xác
        ctx.rounding = ROUND_HALF_UP
        yield

# ---------------------------------------------------------------------------
# Chuyển input thô ➜ Transaction list
# ---------------------------------------------------------------------------


def process_user_entries(entries: Iterable[dict]) -> List[Transaction]:

    txns: List[Transaction] = []
    for ent in entries:
        amt = Decimal(str(ent["amount"])).quantize(QUANT)
        if amt <= 0:
            raise ValueError("amount must be > 0")

        raw_kind = ent["kind"].capitalize()
        if raw_kind == "Saving":
            kind = TransactionKind.EXPENSE
            cat = Category.SAVING
        elif raw_kind == "Repayment":
            kind = TransactionKind.REPAYMENT
            cat = Category(ent.get("category", Category.OTHER))
        else:
            kind = TransactionKind(raw_kind)
            cat = Category(ent.get("category", Category.OTHER))

        raw_date = ent["date"]
        d: date = raw_date if isinstance(
            raw_date, date) else date.fromisoformat(raw_date)

        txns.append(Transaction(amount=amt, kind=kind,
                    category=cat, txn_date=d))
    return txns

# ---------------------------------------------------------------------------
# Hàm tính toán chính
# ---------------------------------------------------------------------------


def net_cash_flow(txns: Iterable[Transaction]) -> Decimal:
    """Thu nhập – chi tiêu (đã *quantize*)."""
    with _money_ctx():
        total = sum(
            t.amount if t.kind is TransactionKind.INCOME else -t.amount for t in txns
        )
    return total.quantize(QUANT)


def summarise_month(txns: Iterable[Transaction], month: str | date | datetime) -> CashflowSummary:
    """Tính Income/Expense/Net cho một tháng (`YYYY‑MM`)."""
    key = _normalise_month(month)
    inc = Decimal("0")
    exp = Decimal("0")
    with _money_ctx():
        for t in txns:
            if month_str(t.txn_date) != key:
                continue
            if t.kind is TransactionKind.INCOME:
                inc += t.amount
            else:
                exp += t.amount
    return CashflowSummary(
        month=key,
        income=inc.quantize(QUANT),
        expense=exp.quantize(QUANT),
        net=(inc - exp).quantize(QUANT),
    )


def monthly_series(txns: Iterable[Transaction]) -> List[CashflowSummary]:
    """Danh sách `CashflowSummary` cho từng tháng có dữ liệu (đã sort)."""
    buckets = _iter_months(txns)
    summaries = [summarise_month(lst, key) for key, lst in buckets.items()]
    summaries.sort(key=lambda s: s.month)
    return summaries


def category_breakdown(
    txns: Iterable[Transaction], month: str | date | datetime | None = None
) -> Dict[Category, Decimal]:
    """Tổng chi tiêu theo danh mục (cho biểu đồ pie)."""
    if month is not None:
        key = _normalise_month(month)
        txns = [t for t in txns if month_str(t.txn_date) == key]

    totals: Dict[Category, Decimal] = defaultdict(lambda: Decimal(0))
    with _money_ctx():
        for t in txns:
            totals[t.category] += t.amount
    return {cat: amt.quantize(QUANT) for cat, amt in totals.items()}


def summarise_period(txns: Iterable[Transaction]) -> List[Tuple[str, Decimal]]:
    """Trả list (`YYYY‑MM`, net) dùng cho line‑chart dài hạn."""
    return [(s.month, s.net) for s in monthly_series(txns)]
# ---------------------------------------------------------------------------
# Alerts/Warnings
# ---------------------------------------------------------------------------


def check_net_cashflow_warning(txns: Iterable[Transaction]) -> str | None:
    """Cảnh báo khi net cash flow < 0."""
    if net_cash_flow(txns) < 0:
        return "⚠️ Overspending: You spent more than you earned."
    return None


def check_income_drop_warning(txns: Iterable[Transaction]) -> str | None:
    """Cảnh báo khi thu nhập tháng hiện tại < 70% trung bình 3 tháng trước."""
    summaries = monthly_series(txns)
    if len(summaries) < 4:
        return None

    latest = summaries[-1]
    previous = summaries[-4:-1]
    avg_income = sum(s.income for s in previous) / Decimal(3)

    if latest.income < avg_income * Decimal("0.7"):
        return "📉 Income decline detected."
    return None


def check_expense_concentration(txns: Iterable[Transaction]) -> str | None:
    """Cảnh báo nếu một danh mục chi tiêu chiếm >40% tổng chi."""
    expense_by_cat = defaultdict(Decimal)
    total_expense = Decimal(0)

    for t in txns:
        if t.kind is TransactionKind.EXPENSE:
            expense_by_cat[t.category] += t.amount
            total_expense += t.amount

    if total_expense == 0:
        return None

    for cat, amt in expense_by_cat.items():
        if amt / total_expense > Decimal("0.4"):
            return f"⚠️ High spending concentration in category: {cat.value} ({(amt / total_expense):.1%})"

    return None
# ---------------------------------------------------------------------------
# Utils nội bộ
# ---------------------------------------------------------------------------


def _normalise_month(month: str | date | datetime) -> str:
    if isinstance(month, (date, datetime)):
        return month_str(month)
    if isinstance(month, str) and len(month) == 7 and month[4] == "-":
        return month
    raise ValueError("month must be 'YYYY-MM' string or date/datetime")


def _iter_months(txns: Iterable[Transaction]) -> Dict[str, List[Transaction]]:
    buckets: Dict[str, List[Transaction]] = defaultdict(list)
    for t in txns:
        buckets[month_str(t.txn_date)].append(t)
    return buckets

# ---------------------------------------------------------------------------
# CLI demo (python cashflow.py) – kiểm thử nhanh
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    demo_entries = [
        {"amount": "1500000", "kind": "Income",
            "category": "Investment",              "date": date(year=2025, month=4, day=1)},
        {"amount": "300000",  "kind": "Expense",
            "category": "Essential Spending",       "date": date(year=2025, month=4, day=2)},
        {"amount": "100000",  "kind": "Saving",
            "category": "Saving",                  "date": date(year=2025, month=4, day=3)},
        {"amount": "250000",  "kind": "Expense",
            "category": "Shopping & Entertainment", "date": date(year=2025, month=4, day=4)},
    ]

    txns = process_user_entries(demo_entries)

    print("Net:", net_cash_flow(txns))
    print("April:", summarise_month(txns, "2025-04"))
    print("Pie:", category_breakdown(txns, "2025-04"))
    print("Series:", summarise_period(txns))

# ---------------------------------------------------------------------------
# Xuất public API
# ---------------------------------------------------------------------------


def save_cashflow_transaction(entry: dict, user_id: int) -> bool:
    """
    Save a user-submitted cashflow transaction to the database.

    Args:
        entry (dict): {'amount': ..., 'kind': ..., 'category': ..., 'date': ...}
        user_id (int): ID of the logged-in user.

    Returns:
        bool: True if saved successfully, False otherwise.
    """
    try:
        # Convert date to a proper Python date object
        raw_date = entry.get("date")
        if isinstance(raw_date, str):
            date_obj = datetime.strptime(raw_date, "%Y-%m-%d").date()
        else:
            date_obj = raw_date

        # Create and save the transaction
        txn = Cashflow(
            user_id=user_id,
            amount=float(entry["amount"]),
            kind=entry["kind"],
            category=entry.get("category", "Other"),
            date=date_obj
        )
        db.session.add(txn)
        db.session.commit()
        return True
    except Exception as e:
        print(f"Error saving transaction: {e}")
        db.session.rollback()
        return False


__all__ = [
    "process_user_entries",
    "net_cash_flow",
    "summarise_month",
    "monthly_series",
    "category_breakdown",
    "summarise_period",
    "check_net_cashflow_warning",
    "check_income_drop_warning",
    "check_expense_concentration",


]
