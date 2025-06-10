# backend/budgeting.py

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
from typing import List, Dict, Optional, Tuple

from backend.domain import Category, Transaction, TransactionKind

# ------------------------------------------------------------------------------
# Decimal context
# ------------------------------------------------------------------------------

QUANT = Decimal("0.01")

def _money_ctx():
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_UP
        yield

# ------------------------------------------------------------------------------
# Domain models
# ------------------------------------------------------------------------------

class BudgetStatus(str, Enum):
    OK       = "OK"
    WARNING  = "WARNING"
    EXCEEDED = "EXCEEDED"

@dataclass(slots=True)
class BudgetRule:
    category: Category
    monthly_limit: Decimal

@dataclass(slots=True)
class BudgetResult:
    category: Category
    actual: Decimal
    planned: Decimal
    remaining: Decimal
    usage_pct: Decimal
    status: BudgetStatus

@dataclass(slots=True)
class SavingGoal:
    name: str
    amount: Decimal
    target_date: date

@dataclass(slots=True)
class GoalFeasibility:
    name: str
    months_left: int
    required_monthly: Decimal
    feasible: bool
    message: str

# ------------------------------------------------------------------------------
# 1) Budget rules processing & comparison
# ------------------------------------------------------------------------------

def process_budget_entries(entries: List[dict]) -> List[BudgetRule]:
    """
    entries: [{ "category": "Essential Spending",
                 "monthly_limit": "5000000" }, ...]
    """
    rules: List[BudgetRule] = []
    for ent in entries:
        amt = Decimal(str(ent["monthly_limit"])).quantize(QUANT)
        if amt <= 0:
            raise ValueError(f"monthly_limit must be > 0, got {amt}")
        cat = Category(ent["category"])
        rules.append(BudgetRule(category=cat, monthly_limit=amt))
    return rules

def aggregate_expense(
    txns: List[Transaction],
    month: str
) -> Dict[Category, Decimal]:
    """
    Sum up all EXPENSE txns in given YYYY-MM by category.
    Returns only categories with actual > 0.
    """
    totals: Dict[Category, Decimal] = {c: Decimal("0") for c in Category}
    with _money_ctx():
        for t in txns:
            if (t.kind is TransactionKind.EXPENSE
                and t.txn_date.strftime("%Y-%m") == month):
                totals[t.category] += t.amount
    # filter zeros and quantize
    return {
        cat: amt.quantize(QUANT)
        for cat, amt in totals.items()
        if amt > 0
    }

def compare_to_budget(
    txns: List[Transaction],
    rules: List[BudgetRule],
    month: str
) -> List[BudgetResult]:
    """
    For each BudgetRule, compute actual spend, remaining, usage% and status.
    """
    actuals = aggregate_expense(txns, month)
    results: List[BudgetResult] = []
    for rule in rules:
        actual = actuals.get(rule.category, Decimal("0"))
        planned = rule.monthly_limit
        remaining = (planned - actual).quantize(QUANT)
        usage = (actual / planned * Decimal("100")).quantize(QUANT)
        if usage <= Decimal("90"):
            status = BudgetStatus.OK
        elif usage <= Decimal("100"):
            status = BudgetStatus.WARNING
        else:
            status = BudgetStatus.EXCEEDED
        results.append(BudgetResult(
            category=rule.category,
            actual=actual,
            planned=planned,
            remaining=remaining,
            usage_pct=usage,
            status=status
        ))
    return results

def budget_alerts(results: List[BudgetResult]) -> List[str]:
    """
    Generate alert messages for any WARNING or EXCEEDED statuses.
    """
    msgs: List[str] = []
    for r in results:
        if r.status is BudgetStatus.OK:
            continue
        if r.status is BudgetStatus.WARNING:
            msgs.append(
                f"⚠ You’re at {r.usage_pct}% of your budget for {r.category.value}."
            )
        else:  # EXCEEDED
            msgs.append(
                f"❌ You have exceeded your budget for {r.category.value}!"
            )
    return msgs

def insights_actual_vs_budget(results: List[BudgetResult]) -> List[str]:
    """
    For each category, generate either:
     - "You overspent your budget in [Category] by X."
     - "You spent less than budgeted in [Category] by X."
    """
    insights: List[str] = []
    for r in results:
        diff = (r.actual - r.planned).quantize(QUANT)
        if diff > 0:
            insights.append(
                f"You overspent your budget in {r.category.value} by {diff:,}."
            )
        elif diff < 0:
            insights.append(
                f"You spent less than budgeted in {r.category.value} by {abs(diff):,}."
            )
    return insights

# ------------------------------------------------------------------------------
# 2) Saving goals processing & feasibility
# ------------------------------------------------------------------------------

def process_saving_goals(entries: List[dict]) -> List[SavingGoal]:
    """
    entries: [{ "name": "Trip Japan",
                "amount": "12000000",
                "target_date": "2025-12-01" }, ...]
    """
    goals: List[SavingGoal] = []
    for ent in entries:
        name = ent["name"].strip()
        if not name:
            raise ValueError("Goal name must be non-empty")
        amt = Decimal(str(ent["amount"])).quantize(QUANT)
        if amt <= 0:
            raise ValueError(f"Goal amount must be > 0, got {amt}")
        td = ent["target_date"]
        tgt = td if isinstance(td, date) else date.fromisoformat(td)
        if tgt <= date.today():
            raise ValueError("target_date must be in the future")
        goals.append(SavingGoal(name=name, amount=amt, target_date=tgt))
    return goals

def evaluate_saving_goals(
    goals: List[SavingGoal],
    monthly_saving_now: Decimal
) -> List[GoalFeasibility]:
    """
    Given current monthly saving rate, evaluate each goal.
    """
    feasibilities: List[GoalFeasibility] = []
    for g in goals:
        today = date.today()
        # compute number of whole months left
        months_left = (g.target_date.year - today.year) * 12 + (g.target_date.month - today.month)
        if months_left <= 0:
            months_left = 1
        with _money_ctx():
            required = (g.amount / months_left).quantize(QUANT)
        feasible = monthly_saving_now >= required
        if feasible:
            msg = (
                f"You are on track to reach “{g.name}” "
                f"by {g.target_date.isoformat()}."
            )
        else:
            msg = (
                f"You need to save {required:,} VND/month to reach “{g.name}” "
                f"by {g.target_date.isoformat()}, but are saving "
                f"{monthly_saving_now:,} VND now."
            )
        feasibilities.append(GoalFeasibility(
            name=g.name,
            months_left=months_left,
            required_monthly=required,
            feasible=feasible,
            message=msg
        ))
    return feasibilities

# ------------------------------------------------------------------------------
# 3) Combined API
# ------------------------------------------------------------------------------

def run_budget_cycle(
    txns: List[Transaction],
    budget_entries: List[dict],
    saving_goal_entries: List[dict],
    monthly_saving_now: Decimal,
    month: str
) -> dict:
    """
    High-level helper to run the full budgeting + goals evaluation for a given month.
    Returns a dict with all results, alerts & insights ready for JSON response.
    """
    rules = process_budget_entries(budget_entries)
    b_results = compare_to_budget(txns, rules, month)
    b_alerts = budget_alerts(b_results)
    b_insights = insights_actual_vs_budget(b_results)

    goals = process_saving_goals(saving_goal_entries)
    g_results = evaluate_saving_goals(goals, monthly_saving_now)

    return {
        "budget": {
            "results": [r.__dict__ for r in b_results],
            "alerts": b_alerts,
            "insights": b_insights
        },
        "goals": {
            "feasibility": [f.__dict__ for f in g_results]
        }
    }
