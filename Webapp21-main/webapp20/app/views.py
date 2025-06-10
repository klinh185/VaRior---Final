from flask import Blueprint, render_template, request, flash, jsonify, redirect, url_for
from flask_login import login_required, current_user
from . import db
from datetime import datetime, date, timedelta
from app.models import Cashflow, Budget, SavingGoal, Category, User, IncomeCategory, Outstanding, EmergencyFundGoal
from app.modules.cashflow import net_cash_flow, summarise_month, Transaction, TransactionKind, Category as CashflowCategory, category_breakdown
from sqlalchemy import func, and_
from calendar import monthrange
from collections import OrderedDict, defaultdict
from werkzeug.security import generate_password_hash
import random
import statistics
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

views = Blueprint('views', __name__)
api_key = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=api_key)

def get_previous_month(date, n):
    """Get the date n months before the given date."""
    year = date.year
    month = date.month - n
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1)

def create_sample_data(user_id):
    # Create sample cashflows
    current_date = datetime.now().date()
    sample_cashflows = [
        # Income
        Cashflow(user_id=user_id, amount=15000000, kind='Income', category='Full‑time Income', date=current_date),
        Cashflow(user_id=user_id, amount=2000000, kind='Income', category='Freelance Income', date=current_date - timedelta(days=5)),
        
        # Expenses
        Cashflow(user_id=user_id, amount=5000000, kind='Expense', category='Essential Spending', date=current_date - timedelta(days=2)),
        Cashflow(user_id=user_id, amount=2000000, kind='Expense', category='Shopping & Entertainment', date=current_date - timedelta(days=3)),
        Cashflow(user_id=user_id, amount=1000000, kind='Expense', category='Health', date=current_date - timedelta(days=4)),
        Cashflow(user_id=user_id, amount=3000000, kind='Expense', category='Education', date=current_date - timedelta(days=6)),
        
        # Savings
        Cashflow(user_id=user_id, amount=2000000, kind='Savings', category='Emergency Fund', date=current_date - timedelta(days=1)),
    ]
    
    # Create sample budgets for current month
    current_month = current_date.strftime('%Y-%m')
    sample_budgets = [
        Budget(user_id=user_id, category='Essential Spending', month=current_month, planned_amount=6000000),
        Budget(user_id=user_id, category='Shopping & Entertainment', month=current_month, planned_amount=3000000),
        Budget(user_id=user_id, category='Health', month=current_month, planned_amount=2000000),
        Budget(user_id=user_id, category='Education', month=current_month, planned_amount=4000000),
    ]
    
    # Create sample saving goals
    sample_goals = [
        SavingGoal(
            user_id=user_id,
            name='Emergency Fund',
            total_amount=50000000,
            start_date=current_date - timedelta(days=30),
            target_date=current_date + timedelta(days=180),
            past_savings=10000000
        ),
        SavingGoal(
            user_id=user_id,
            name='Vacation Fund',
            total_amount=30000000,
            start_date=current_date - timedelta(days=15),
            target_date=current_date + timedelta(days=90),
            past_savings=5000000
        )
    ]
    
    try:
        # Add all sample data
        db.session.add_all(sample_cashflows)
        db.session.add_all(sample_budgets)
        db.session.add_all(sample_goals)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        print(f"Error creating sample data: {str(e)}")
        return False
    

@views.route('/')
def landing():
    return render_template('landing_page.html')

@views.route('/about-us')
def about_us():
    return render_template('about_us.html')

@views.route('/dashboard')
@login_required
def dashboard():
    # --- A. THIẾT LẬP KHOẢNG THỜI GIAN ---
    current_month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # === B. TÍNH TOÁN CÁC CHỈ SỐ CỐT LÕI (PHIÊN BẢN TƯƠNG THÍCH PYTHON 3.13) ===

    # B.1: Lấy tất cả giao dịch Cashflow trong tháng (CHƯA kèm Outstanding)
    monthly_cashflows_base = Cashflow.query.filter(
        Cashflow.user_id == current_user.id,
        Cashflow.date >= current_month_start
    ).all()
    
    # B.2: Thu thập ID của các cashflow này
    cashflow_ids = [cf.id for cf in monthly_cashflows_base]
    
    # B.3: Tạo một dictionary để tra cứu nhanh các khoản nợ
    outstanding_map = defaultdict(list)
    if cashflow_ids: # Chỉ truy vấn nếu có giao dịch trong tháng
        # Lấy TẤT CẢ các mục nợ liên quan trong MỘT câu lệnh duy nhất
        all_related_outstandings = Outstanding.query.filter(
            Outstanding.cashflow_id.in_(cashflow_ids)
        ).all()
        # "Khớp" các khoản nợ vào dictionary với key là cashflow_id
        for item in all_related_outstandings:
            outstanding_map[item.cashflow_id].append(item)

    # --- Phần còn lại của logic tính toán (giữ nguyên, chỉ thay đổi cách lấy cf_ar/cf_ap) ---

    # Khởi tạo biến
    total_cash_inflow = Decimal('0.0')
    total_cash_outflow = Decimal('0.0')
    total_ar = Decimal('0.0')
    total_ap = Decimal('0.0')
    actual_income = Decimal('0.0')
    actual_expense = Decimal('0.0')
    category_expense_breakdown = defaultdict(Decimal)
    category_income_breakdown = defaultdict(Decimal)

    # Lặp qua các giao dịch và tính toán
    for cf in monthly_cashflows_base: # Lặp qua danh sách cashflow gốc
        # Lấy các khoản nợ từ map đã tạo, thay vì từ cf.outstanding_items
        related_outstandings = outstanding_map.get(cf.id, [])
        cf_ar = sum(Decimal(str(item.amount)) for item in related_outstandings if item.debt_type == 'AR')
        cf_ap = sum(Decimal(str(item.amount)) for item in related_outstandings if item.debt_type == 'AP')
        cf_amount = Decimal(str(cf.amount))
        # Logic tính toán bên dưới không thay đổi
        if cf.kind == 'Income':
            total_cash_inflow += cf_amount; total_ar += cf_ar; total_ap += cf_ap
            income_for_cf = cf_amount + cf_ar - cf_ap
            actual_income += income_for_cf
            if cf.category: category_income_breakdown[cf.category] += income_for_cf
        elif cf.kind == 'Expense':
            total_cash_outflow += cf_amount; total_ar += cf_ar; total_ap += cf_ap
            expense_for_cf = cf_amount - cf_ar + cf_ap
            actual_expense += expense_for_cf
            if cf.category: category_expense_breakdown[cf.category] += expense_for_cf

    # === C. TÍNH TOÁN CÁC BIẾN TỔNG HỢP ===
    # Lấy các giao dịch tiết kiệm từ danh sách đã có, không cần truy vấn lại
    monthly_savings_cashflows = [cf for cf in monthly_cashflows_base if cf.kind == 'Savings']
    exp_saving_this_month = sum(Decimal(str(cf.amount)) for cf in monthly_savings_cashflows if cf.category != 'Emergency Fund')
    emergency_saving_this_month = sum(Decimal(str(cf.amount)) for cf in monthly_savings_cashflows if cf.category == 'Emergency Fund')
    
    current_saving_total_monthly = exp_saving_this_month + emergency_saving_this_month
    net_balance = actual_income - actual_expense - current_saving_total_monthly
    net_cash_on_hand = max(total_cash_inflow - total_cash_outflow, Decimal('0.0'))

    # === D. LẤY DỮ LIỆU CHO CÁC MODULE TIẾN ĐỘ (giữ nguyên logic) ===
    # (Toàn bộ phần D và E của bạn không cần thay đổi vì chúng đã chính xác)
    # ... (code cho Budget Health, Savings Goals, Emergency Fund, và dashboard_data) ...
    
    # Chỉ cần đảm bảo bạn copy đúng toàn bộ phần D và E từ code cũ của bạn vào đây. 
    # Tôi sẽ dán lại đầy đủ để chắc chắn.

    # 1. Budget Health
    budgets = Budget.query.filter_by(user_id=current_user.id, month=current_month_start.strftime('%Y-%m')).all()
    budget_health_data = []
    for budget in budgets:
        actual_spending = Decimal(str(category_expense_breakdown.get(budget.category, Decimal('0.0'))))
        planned = Decimal(str(budget.planned_amount))
        progress = (actual_spending / planned * Decimal('100')) if planned > 0 else Decimal('0.0')
        budget_health_data.append({'category': budget.category, 'planned': planned, 'actual': actual_spending, 'progress': float(progress)})

    # 2. Savings Goals (Tối ưu hóa)
    saving_goals = SavingGoal.query.filter_by(user_id=current_user.id).all()
    saving_goals_data = []
    all_savings_cashflows = Cashflow.query.filter_by(user_id=current_user.id, kind='Savings').all()
    savings_by_category = defaultdict(list)
    for cf in all_savings_cashflows:
        if cf.category: savings_by_category[cf.category].append(cf)

    for goal in saving_goals:
        goal_cashflows = [cf for cf in savings_by_category.get(goal.name, []) if goal.start_date <= cf.date <= goal.target_date]
        current_savings = sum(Decimal(str(cf.amount)) for cf in goal_cashflows)
        progress = goal.calculate_progress(float(current_savings))
        saving_goals_data.append({'name': goal.name, 'total_amount': goal.total_amount, 'current_savings': current_savings, 'progress': progress})

    # Tổng số tiền mục tiêu của tất cả các saving goals và tổng đã tiết kiệm được
    total_saving_goals = sum(g['total_amount'] for g in saving_goals_data)
    total_experience_saving = sum(g['current_savings'] for g in saving_goals_data)

    # 3. Emergency Fund (Lấy dữ liệu tổng thể)
    emergency_goal = EmergencyFundGoal.query.filter_by(user_id=current_user.id, status='active').first()
    total_emergency_fund_saved = Decimal(str(emergency_goal.current_savings)) if emergency_goal and emergency_goal.current_savings is not None else Decimal('0.0')

    # ===============================================================
    # PHẦN MỚI: RULE ENGINE - TẠO RECOMMENDATIONS
    # ===============================================================
    recommendations = {
        'financial_overview': [],
        'budget_health': [],
        'savings_goals': [],
        'emergency_fund': []
    }

    # === I. TRANSACTION INSIGHTS ===
    if net_balance < 0 and net_cash_on_hand > 0:
        recommendations['financial_overview'].append("You still have cash on hand, even though your net balance is in the negative. Maybe check if there are any debts to settle?")
    elif net_balance > 0 and net_cash_on_hand < (actual_expense / 4): # Heuristic cho "short on cash"
        recommendations['financial_overview'].append("Your net balance is positive but your cash is low. Profitable on paper but short on liquidity? Might be time to check what's still unpaid or held up.")
    elif net_balance > 0:
        surplus_amount = f"{net_balance:,.0f} ₫"
        recommendations['financial_overview'].append(f"Nice! You've got a surplus this month (~{surplus_amount}). Maybe it's a good time to grow your savings or treat yourself a little.")
    elif net_balance < 0:
        deficit_amount = f"{-net_balance:,.0f} ₫"
        recommendations['financial_overview'].append(f"Looks like you've spent more than you earned (-{deficit_amount}). Maybe review where most of it went?")
    else: # net_balance == 0
        recommendations['financial_overview'].append("You're breaking even this month. That's balance!")
    
    if total_ap > 0 and net_cash_on_hand < total_ap:
        recommendations['financial_overview'].append("Your cash on hand is currently less than amount you owed. Consider tackling high-priority ones first.")

    # === II. BUDGET INSIGHTS ===
    # Budget
    overspent_count = sum(1 for b in budget_health_data if b['progress'] > 100)
    if overspent_count > 3:
        recommendations['budget_health'].append("More than 3 categories are over budget this period. Spending is off-track in a few areas. A quick budget check could help realign things.")
    elif overspent_count > 0:
        recommendations['budget_health'].append("A few categories are a bit over budget. Small adjustments might be all you need.")
    elif len(budget_health_data) > 0: # Chỉ hiển thị nếu có budget
        recommendations['budget_health'].append("Your spending is fully within plan. Love to see that kind of control!")

    # === III. SAVINGS INSIGHTS ===
    if total_saving_goals > 0:
        progress_percentage = (Decimal(total_experience_saving) / Decimal(total_saving_goals)) * Decimal('100')
        if progress_percentage >= 90:
            recommendations['savings_goals'].append(
                "You've saved over 90% of your goal this period. Great job! Your plans are almost on track."
            )
        elif progress_percentage >= 70:
            recommendations['savings_goals'].append(
                "You're currently between 70-90% of your savings goal. That's good progress—keep the momentum going, you're almost there!"
            )
        else:
            recommendations['savings_goals'].append(
                "You've reached less than 70% of your saving goal so far. You're not quite hitting your target yet. Want to tweak your plan or schedule small top-ups?"
            )

    # === IV. EMERGENCY FUND READINESS ===
    # emergency_goal: đối tượng mục tiêu EF của người dùng (có thể là None)
    # total_emergency_fund_saved: tổng số tiền đã tiết kiệm cho EF
    # net_cash_on_hand: tiền mặt ròng hiện có
    # total_experience_saving: tổng tiền tiết kiệm cho các mục tiêu khác
    if emergency_goal:
        ef_target = emergency_goal.target_amount

        # Kịch bản 1: Quỹ EF đã đủ hoặc vượt mục tiêu
        if total_emergency_fund_saved >= ef_target:
            recommendations['emergency_fund'].append(
                "Your emergency fund has reached (or exceeded) the goal you set. That's great! You're in a strong position to handle unexpected situations."
            )
        # Kịch bản 2: Quỹ EF chưa đủ, nhưng nếu cộng tiền mặt vào thì đủ
        if total_emergency_fund_saved < ef_target and (total_emergency_fund_saved + net_cash_on_hand) >= ef_target:
            recommendations['emergency_fund'].append(
                "Your emergency fund hasn't quite hit your goal, but your available cash can cover it if needed. You might still want to keep building that buffer just in case."
            )
        # Kịch bản 3: Quỹ EF + Tiền mặt chưa đủ, nhưng nếu cộng cả tiết kiệm trải nghiệm vào thì đủ
        if (total_emergency_fund_saved + net_cash_on_hand) < ef_target and (total_emergency_fund_saved + net_cash_on_hand + total_experience_saving) >= ef_target:
            recommendations['emergency_fund'].append(
                "You don't have quite enough cash to meet your emergency goal right now. You could dip into your experience savings if needed—but that might impact your other plans."
            )
        # Kịch bản 4: Mức độ rủi ro cao nhất, cộng tất cả vào vẫn không đủ
        if (total_emergency_fund_saved + net_cash_on_hand + total_experience_saving) < ef_target:
            recommendations['emergency_fund'].append(
                "Your current emergency fund, cash on hand, and experience savings combined still don't meet your full emergency goal. If an urgent situation comes up, you might fall short - consider making small, steady contributions to build better protection."
            )

    # === E. GÓI DỮ LIỆU TRẢ VỀ ===
    dashboard_data = {
        'cash_inflow': float(total_cash_inflow), 'cash_outflow': float(total_cash_outflow),
        'actual_income': float(actual_income), 'actual_expense': float(actual_expense),
        'current_saving': float(current_saving_total_monthly), 'net_balance': float(net_balance),
        'net_cash_on_hand': float(net_cash_on_hand), 'amount_you_owed': float(total_ap),
        'amount_owed_to_you': float(total_ar),
        'category_expenses_breakdown': {k: float(v) for k, v in category_expense_breakdown.items()},
        'category_income_breakdown':  {k: float(v) for k, v in category_income_breakdown.items()},
        'budget_health': budget_health_data,
        'saving_goals': [
            {**g, 'total_amount': float(g['total_amount']), 'current_savings': float(g['current_savings'])}
            for g in saving_goals_data
        ],
        'total_saving_goals': float(total_saving_goals),
        'emergency_fund': {
            'target': float(emergency_goal.target_amount) if emergency_goal and emergency_goal.target_amount is not None else 0,
            'total_saved': float(total_emergency_fund_saved),
            'this_month_contribution': float(emergency_saving_this_month),
            'remaining': float(emergency_goal.target_amount - total_emergency_fund_saved) if emergency_goal and emergency_goal.target_amount is not None else 0
        }
    }
    
    # TRUYỀN CẢ dashboard_data VÀ recommendations RA TEMPLATE
    return render_template('dashboard.html', 
                           dashboard_data=dashboard_data,
                           recommendations=recommendations)

@views.route('/add-cashflow', methods=['POST'])
@login_required
def add_cashflow():
    if request.method == 'POST':
        total_amount = float(request.form['amount'].replace(',', ''))
        kind = request.form['kind']
        category = request.form.get('category')
        date = datetime.strptime(request.form['date'], "%Y-%m-%d").date()
        description = request.form.get('description')
        flow_direction = request.form.get("flow_direction")  # 'cash-in' or 'cash-out'

        # Apply sign change for savings cash-out
        if kind == 'Savings' and flow_direction == 'cash-out':
            total_amount *= -1

        # Step A: Create the cashflow record
        new_cashflow = Cashflow(
            user_id=current_user.id,
            amount=total_amount,
            kind=kind,
            category=category,
            date=date,
            description=description
        )
        db.session.add(new_cashflow)

        try:
            # Step B: Handle related debts (Outstanding)
            ar_persons = request.form.getlist('ar_persons[]')
            ar_amounts = request.form.getlist('ar_amounts[]')
            for i in range(len(ar_persons)):
                if ar_persons[i].strip():
                    ar_record = Outstanding(
                        cashflow=new_cashflow,
                        user_id=current_user.id,
                        person_name=ar_persons[i].strip(),
                        amount=float(ar_amounts[i].replace(',', '')),
                        debt_type='AR',
                        date=new_cashflow.date
                    )
                    db.session.add(ar_record)

            ap_persons = request.form.getlist('ap_persons[]')
            ap_amounts = request.form.getlist('ap_amounts[]')
            for i in range(len(ap_persons)):
                if ap_persons[i].strip():
                    ap_record = Outstanding(
                        cashflow=new_cashflow,
                        user_id=current_user.id,
                        person_name=ap_persons[i].strip(),
                        amount=float(ap_amounts[i].replace(',', '')),
                        debt_type='AP',
                        date=new_cashflow.date
                    )
                    db.session.add(ap_record)

            # Step C: Commit to database
            db.session.commit()
            flash('Transaction added successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {e}', 'error')
        
        return redirect(url_for('views.cashflows'))

@views.route('/cashflows')
@login_required
def cashflows():
    # Get the selected month from query parameters, default to 'all' for all transactions
    selected_month = request.args.get('month', 'all')
    
    # Query all cashflows for the user
    user_cashflows = Cashflow.query.filter_by(user_id=current_user.id)
    
    # Filter by selected month only if a specific month is selected
    if selected_month != 'all':
        user_cashflows = user_cashflows.filter(
            func.strftime('%Y-%m', Cashflow.date) == selected_month
        )
    
    # Get all months that have cashflows for the dropdown
    months_query = db.session.query(
        func.strftime('%Y-%m', Cashflow.date).label('month')
    ).filter_by(user_id=current_user.id).distinct().order_by('month').all()
    
    # Create month options list with 'All Transactions' as first option
    month_options = ['all'] + [m[0] for m in months_query]
    
    # Get the filtered cashflows ordered by date
    user_cashflows = user_cashflows.order_by(Cashflow.date.desc()).all()
    
    return render_template(
        "cashflows.html",
        user=current_user,
        cashflows=user_cashflows,
        month_options=month_options,
        month=selected_month,
        datetime=datetime,
        categories=[c.value for c in Category]
    )

@views.route('/delete-cashflow', methods=['POST'])
@login_required
def delete_cashflow():
    try:
        cashflow_id = request.json['cashflowId']
        cashflow = Cashflow.query.get(cashflow_id)
        if cashflow and cashflow.user_id == current_user.id:
            db.session.delete(cashflow)
            db.session.commit()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Cashflow not found or unauthorized"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@views.route('/budget', methods=['GET', 'POST'])
@login_required
def budget():
    if request.method == 'POST':
        try:
            # Handle budget updates
            category = request.form.get('category')
            month = request.form.get('month')
            planned_amount = float(request.form.get('planned_amount').replace(',', ''))
            
            # Validate that category is a valid Category enum value
            if category not in [c.value for c in Category]:
                flash('Invalid category selected', 'error')
                return redirect(url_for('views.budget'))
            
            # Check if budget already exists for this category and month
            budget = Budget.query.filter_by(
                user_id=current_user.id,
                category=category,
                month=month
            ).first()
            
            if budget:
                budget.planned_amount = planned_amount
            else:
                budget = Budget(
                    user_id=current_user.id,
                    category=category,
                    month=month,
                    planned_amount=planned_amount
                )
                db.session.add(budget)
            
            db.session.commit()
            flash('Budget updated successfully!', 'success')
            
        except Exception as e:
            flash(f'Error updating budget: {str(e)}', 'error')
    
    # Lấy tháng hiện tại và các budget người dùng đã thiết lập
    current_month = datetime.now().strftime('%Y-%m')
    budgets = Budget.query.filter_by(
        user_id=current_user.id,
        month=current_month
    ).all()
    
    # --- Bước 1: Lấy tất cả giao dịch EXPENSE và các KHOẢN NỢ liên quan trong tháng ---
    # Chỉ lấy các giao dịch có kind = 'Expense' để tính budget
    expense_cashflows = Cashflow.query.filter(
        Cashflow.user_id == current_user.id,
        Cashflow.kind == 'Expense',
        func.strftime('%Y-%m', Cashflow.date) == current_month
    ).options(
        # Tải sẵn các khoản nợ liên quan để tối ưu hiệu năng, tránh nhiều truy vấn DB
        db.joinedload(Cashflow.outstanding_items) 
    ).all()

    # --- Bước 2: Tính toán ACTUAL EXPENSE chính xác cho từng category ---
    # Dùng defaultdict để không cần kiểm tra key có tồn tại hay không khi cộng dồn
    category_actual = defaultdict(float) 

    for cf in expense_cashflows:
        # Bắt đầu với cash outflow (tiền mặt thực chi) của giao dịch
        cash_outflow = float(cf.amount)
        
        # Tính tổng AR (tiền mình trả hộ) trong giao dịch này
        total_ar_in_cf = sum(float(item.amount) for item in cf.outstanding_items if item.debt_type == 'AR')
        
        # Tính tổng AP (tiền người khác trả hộ) trong giao dịch này
        total_ap_in_cf = sum(float(item.amount) for item in cf.outstanding_items if item.debt_type == 'AP')
        
        # Áp dụng công thức: Chi tiêu thực tế = Tiền mặt ra - Trả hộ + Được trả hộ
        actual_expense_for_cf = cash_outflow - total_ar_in_cf + total_ap_in_cf
        
        # Cộng dồn chi tiêu thực tế vào đúng category của nó
        if cf.category:
            category_actual[cf.category] += actual_expense_for_cf

    # --- Bước 3: Chuẩn bị dữ liệu `budget_data` để hiển thị, sử dụng `category_actual` đã được tính đúng ---
    budget_data = []
    total_planned = 0
    total_actual = sum(category_actual.values()) # Tổng chi tiêu thực tế của tất cả các category
    alerts = []
    
    # Lấy danh sách các category đã được đặt budget
    existing_budget_categories = {b.category for b in budgets}

    # Vòng lặp 1: Xử lý các category ĐÃ ĐƯỢC đặt budget
    for budget in budgets:
        # Lấy chi tiêu thực tế của category này từ dictionary đã tính đúng ở trên
        actual_spending = category_actual.get(budget.category, 0)
        
        usage_percent = budget.calculate_usage(actual_spending)
        remaining = budget.get_remaining(actual_spending)
        
        # Tạo cảnh báo nếu vượt budget
        if usage_percent > 100:
            alerts.append({
                'type': 'danger',
                'message': f'❌ You have exceeded your budget for {budget.category}!'
            })
        elif usage_percent > 90:
            alerts.append({
                'type': 'warning',
                'message': f'⚠ You\'re at {usage_percent:.1f}% of your budget for {budget.category}.'
            })
            
        budget_data.append({
            'category': budget.category,
            'planned': budget.planned_amount,
            'actual': actual_spending,
            'remaining': remaining,
            'usage_percent': usage_percent
        })
        total_planned += budget.planned_amount

    # Vòng lặp 2: Xử lý các category CÓ CHI TIÊU nhưng CHƯA ĐƯỢC đặt budget
    for category, actual_spending in category_actual.items():
        if category not in existing_budget_categories:
            budget_data.append({
                'category': category,
                'planned': 0, # Ngân sách dự kiến là 0
                'actual': actual_spending,
                'remaining': -actual_spending, # Số tiền còn lại là số âm
                'usage_percent': 100 if actual_spending > 0 else 0
            })

    # Sắp xếp lại danh sách để hiển thị theo thứ tự alphabet
    budget_data.sort(key=lambda x: x['category'])

    # Kiểm tra cảnh báo tổng thể
    if len([d for d in budget_data if d['usage_percent'] > 100]) >= 3:
        alerts.append({
            'type': 'danger',
            'message': 'Spending is drifting from plan in multiple areas.'
        })

    return render_template(
        'budget.html',
        user=current_user,
        budget_data=budget_data,
        alerts=alerts,
        total_planned=total_planned,
        total_actual=total_actual,
        current_month=current_month,
        categories=[c.value for c in Category if c != Category.OTHER],  # Pass categories to template
        min=min
    )

@views.route('/saving-goals')
@login_required
def saving_goals_page():
    # Get saving goals
    user_saving_goals = SavingGoal.query.filter_by(user_id=current_user.id).all()
    goals_data = []

    for goal in user_saving_goals:
        # Calculate current savings from cashflows
        savings_cashflows = Cashflow.query.filter(
            Cashflow.user_id == current_user.id,
            Cashflow.kind == 'Savings',
            Cashflow.category == goal.name,  # Only count savings for this specific goal
            Cashflow.date >= goal.start_date,
            Cashflow.date <= goal.target_date
        ).all()

        current_savings = sum(float(cf.amount) for cf in savings_cashflows)

        # Calculate monthly saving (if goal started in current month)
        if goal.start_date.year == datetime.now().year and goal.start_date.month == datetime.now().month:
            current_monthly_saving = current_savings
        else:
            months_active = ((datetime.now().year - goal.start_date.year) * 12 +
                           datetime.now().month - goal.start_date.month)
            current_monthly_saving = current_savings / max(1, months_active)

        progress = goal.calculate_progress(current_savings)

        goals_data.append({
            'id': goal.id,
            'name': goal.name,
            'total_amount': goal.total_amount,
            'current_savings': current_savings,
            'monthly_required': goal.calculate_monthly_saving(),
            'current_monthly_saving': current_monthly_saving,
            'progress': progress,
            'on_track': goal.is_on_track(current_monthly_saving),
            'start_date': goal.start_date,
            'target_date': goal.target_date,
            'saving_gap': goal.get_saving_gap(current_monthly_saving)
        })

    # Get saving goal alerts (if any)
    alerts = []
    # You might want to add specific saving goal related alerts here if needed

    return render_template(
        'saving_goals.html',
        user=current_user,
        goals_data=goals_data,
        alerts=alerts,  # Pass alerts to the template
        datetime=datetime
    )

@views.route('/add-saving-goal', methods=['POST'])
@login_required
def add_saving_goal():
    try:
        name = request.form.get('name')
        total_amount = float(request.form.get('total_amount').replace(',', ''))
        start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
        target_date = datetime.strptime(request.form.get('target_date'), '%Y-%m-%d').date()
        
        # Make past_savings optional with default 0
        past_savings_str = request.form.get('past_savings', '').strip()
        past_savings = float(past_savings_str.replace(',', '')) if past_savings_str else 0
        
        goal = SavingGoal(
            user_id=current_user.id,
            name=name,
            total_amount=total_amount,
            start_date=start_date,
            target_date=target_date,
            past_savings=past_savings
        )
        
        db.session.add(goal)
        db.session.commit()
        
        flash('Saving goal added successfully!', 'success')
        # Redirect to the saving goals page
        return redirect(url_for('views.saving_goals_page'))
        
    except Exception as e:
        flash(f'Error adding saving goal: {str(e)}', 'error')
        # Redirect to the saving goals page on error
        return redirect(url_for('views.saving_goals_page'))

@views.route('/delete-saving-goal', methods=['POST'])
@login_required
def delete_saving_goal():
    try:
        goal_id = request.json['goalId']
        goal = SavingGoal.query.get(goal_id)
        if goal and goal.user_id == current_user.id:
            db.session.delete(goal)
            db.session.commit()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Goal not found or unauthorized"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@views.route('/edit-saving-goal', methods=['POST'])
@login_required
def edit_saving_goal():
    try:
        goal_id = request.form.get('goal_id')
        name = request.form.get('name')
        total_amount = float(request.form.get('total_amount').replace(',', ''))
        start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
        target_date = datetime.strptime(request.form.get('target_date'), '%Y-%m-%d').date()
        
        # Get the goal and verify ownership
        goal = SavingGoal.query.get(goal_id)
        if not goal or goal.user_id != current_user.id:
            return jsonify({"success": False, "error": "Goal not found or unauthorized"}), 404
        
        # If the name has changed, update all related cashflows
        if goal.name != name:
            # Update all cashflows that reference this goal
            # Need to import Cashflow and db from app.models and . respectively
            from app.models import Cashflow
            db.session.query(Cashflow).filter(
                Cashflow.user_id==current_user.id,
                Cashflow.kind=='Savings',
                Cashflow.category==goal.name
            ).update({'category': name})
        
        # Update goal details
        goal.name = name
        goal.total_amount = total_amount
        goal.start_date = start_date
        goal.target_date = target_date
        
        db.session.commit()
        flash('Saving goal updated successfully!', 'success')
        # Redirect to the saving goals page
        return redirect(url_for('views.saving_goals_page'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating saving goal: {str(e)}', 'error')
        # Redirect to the saving goals page on error
        return redirect(url_for('views.saving_goals_page'))

@views.route('/create-test-account')
def create_test_account():
    # Check if test account already exists
    test_user = User.query.filter_by(email='test@gmail.com').first()
    if test_user:
        return 'Test account already exists!'
    
    # Create new test account
    new_user = User(
        email='test@gmail.com',
        first_name='VaRiors',
        password=generate_password_hash('testpassword', method='pbkdf2:sha256')
    )
    
    try:
        db.session.add(new_user)
        db.session.commit()
        
        # Create sample data for the new user
        if create_sample_data(new_user.id):
            return 'Test account and sample data created successfully!'
        else:
            return 'Test account created but failed to create sample data.'
            
    except Exception as e:
        db.session.rollback()
        return f'Error creating test account: {str(e)}'

@views.route('/outstanding')
@login_required
def outstanding():
    # Lấy tất cả các khoản outstanding của user
    ar_items = Outstanding.query.filter_by(user_id=current_user.id, debt_type='AR').order_by(Outstanding.date.desc()).all()
    ap_items = Outstanding.query.filter_by(user_id=current_user.id, debt_type='AP').order_by(Outstanding.date.desc()).all()

    total_receivable = sum(float(item.amount) for item in ar_items)
    total_payable = sum(float(item.amount) for item in ap_items)

    # Lấy tất cả category cho dropdown nếu cần
    categories = [c.value for c in Category]
    
    return render_template(
        "outstanding.html",
        user=current_user,
        payables=ap_items,
        receivables=ar_items,
        total_payable=total_payable,
        total_receivable=total_receivable,
        categories=categories,
        datetime=datetime
    )

@views.route('/get-transactions/<person_name>')
@login_required
def get_transactions(person_name):
    transactions = Outstanding.query.filter_by(
        user_id=current_user.id,
        person_name=person_name
    ).order_by(Outstanding.date.desc()).all()

    transaction_list = [
        {
            'date': t.date.strftime('%Y-%m-%d'),
            'amount': float(t.amount),
            'type': 'Lent' if t.debt_type == 'AR' else 'Owed',
            'note': ''
        }
        for t in transactions
    ]
    return jsonify(transaction_list)

@views.route('/edit-cashflow', methods=['POST'])
@login_required
def edit_cashflow():
    if request.method == 'POST':
        try:
            cashflow_id = request.form.get('cashflow_id')
            amount = float(request.form['amount'].replace(',', ''))
            date = datetime.strptime(request.form['date'], "%Y-%m-%d").date()
            category = request.form.get('category')

            # Get the cashflow and verify ownership
            cashflow = Cashflow.query.get(cashflow_id)
            if not cashflow or cashflow.user_id != current_user.id:
                flash('Transaction not found or unauthorized', 'error')
                return redirect(url_for('views.outstanding'))

            # Update the cashflow
            cashflow.amount = amount
            cashflow.date = date
            cashflow.category = category

            db.session.commit()
            flash('Transaction updated successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating transaction: {str(e)}', 'error')
        
        return redirect(url_for('views.cashflows'))

@views.route('/emergency-fund')
@login_required
def emergency_fund_page():
    # Luôn render trang chọn loại calculator (selection view)
    return render_template('emergency_fund.html', active_goal=None, user=current_user)

@views.route('/save-emergency-fund-goal', methods=['POST'])
@login_required
def save_emergency_fund_goal():
    """
    API endpoint để tính toán và lưu (hoặc cập nhật) mục tiêu.
    Xử lý cả 2 loại: 'simple' và 'customized'.
    """
    data = request.get_json()
    calc_type = data.get('type')
    
    target_amount = Decimal('0.0')
    goal_details = {'calculation_type': calc_type.capitalize(), 'status': 'active'}

    try:
        # Cách 1: Simple Calculation
        if calc_type == 'simple':
            monthly_expense = Decimal(data.get('monthly_expense', '0'))
            months = int(data.get('months', 0))
            if monthly_expense <= 0 or months <= 0:
                return jsonify({'success': False, 'error': 'Vui lòng nhập số liệu hợp lệ.'}), 400
            target_amount = monthly_expense * months
            goal_details.update({
                'monthly_expense_base': monthly_expense,
                'months_to_cover': months,
                'customized_scenarios': None # Xóa dữ liệu cũ nếu đổi từ customized sang
            })
        
        # Cách 2: Customized Calculation
        elif calc_type == 'customized':
            scenarios = data.get('scenarios', [])
            if not scenarios:
                return jsonify({'success': False, 'error': 'Vui lòng thêm ít nhất một kịch bản.'}), 400
            for scenario in scenarios:
                expense = Decimal(scenario.get('expense_covered', '0'))
                frequency = Decimal(scenario.get('frequency', '1'))
                target_amount += expense * frequency
            goal_details.update({
                'customized_scenarios': scenarios,
                'monthly_expense_base': None, # Xóa dữ liệu cũ
                'months_to_cover': None
            })
        else:
            return jsonify({'success': False, 'error': 'Loại tính toán không hợp lệ.'}), 400

        goal_details['target_amount'] = target_amount

        # Logic cốt lõi: Cập nhật nếu đã tồn tại, nếu không thì tạo mới
        existing_goal = EmergencyFundGoal.query.filter_by(user_id=current_user.id, status='active').first()
        if existing_goal:
            for key, value in goal_details.items():
                setattr(existing_goal, key, value)
            goal = existing_goal
        else:
            goal = EmergencyFundGoal(user_id=current_user.id, **goal_details)
            db.session.add(goal)
            
        db.session.commit()
        
        return jsonify({
            'success': True, 'message': 'Mục tiêu đã được lưu!',
            'goal_id': goal.id, 'target_amount': float(target_amount)
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@views.route('/save-fund-roadmap', methods=['POST'])
@login_required
def save_fund_roadmap():
    data = request.get_json()
    goal_id = data.get('goal_id')
    current_savings = Decimal(data.get('current_savings', '0'))
    monthly_contribution = Decimal(data.get('monthly_contribution', '0'))
    
    goal = EmergencyFundGoal.query.filter_by(id=goal_id, user_id=current_user.id).first()
    if not goal:
        return jsonify({'success': False, 'error': 'Không tìm thấy mục tiêu.'}), 404
        
    if monthly_contribution <= 0:
        return jsonify({'success': False, 'error': 'Số tiền tiết kiệm hàng tháng phải lớn hơn 0.'}), 400
    
    try:
        goal.current_savings = current_savings
        goal.monthly_contribution = monthly_contribution
        db.session.commit()
        
        remaining = goal.target_amount - current_savings
        months_to_target = 0 if remaining <= 0 else round(remaining / monthly_contribution, 1)

        return jsonify({'success': True, 'message': 'Lộ trình đã được lưu!', 'months_to_target': months_to_target})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

def calculate_actuals(user_id, start_date=None, end_date=None):
    from sqlalchemy import func, and_
    # Lọc theo ngày nếu có
    cashflow_filter = [Cashflow.user_id == user_id, Cashflow.kind == 'Expense']
    outstanding_ar_filter = [Outstanding.user_id == user_id, Outstanding.debt_type == 'AR', Cashflow.kind == 'Expense']
    outstanding_ap_filter = [Outstanding.user_id == user_id, Outstanding.debt_type == 'AP', Cashflow.kind == 'Expense']
    if start_date:
        cashflow_filter.append(Cashflow.date >= start_date)
        outstanding_ar_filter.append(Cashflow.date >= start_date)
        outstanding_ap_filter.append(Cashflow.date >= start_date)
    if end_date:
        cashflow_filter.append(Cashflow.date <= end_date)
        outstanding_ar_filter.append(Cashflow.date <= end_date)
        outstanding_ap_filter.append(Cashflow.date <= end_date)

    # Tổng cash outflow
    cash_outflow = db.session.query(func.coalesce(func.sum(Cashflow.amount), 0)).filter(*cashflow_filter).scalar()
    # Tổng AR
    total_ar = db.session.query(func.coalesce(func.sum(Outstanding.amount), 0)).join(Cashflow, Outstanding.cashflow_id == Cashflow.id).filter(*outstanding_ar_filter).scalar()
    # Tổng AP
    total_ap = db.session.query(func.coalesce(func.sum(Outstanding.amount), 0)).join(Cashflow, Outstanding.cashflow_id == Cashflow.id).filter(*outstanding_ap_filter).scalar()
    actual_expense = cash_outflow - total_ar + total_ap

    # Tương tự cho Income
    cashflow_filter_income = [Cashflow.user_id == user_id, Cashflow.kind == 'Income']
    outstanding_ar_filter_income = [Outstanding.user_id == user_id, Outstanding.debt_type == 'AR', Cashflow.kind == 'Income']
    outstanding_ap_filter_income = [Outstanding.user_id == user_id, Outstanding.debt_type == 'AP', Cashflow.kind == 'Income']
    if start_date:
        cashflow_filter_income.append(Cashflow.date >= start_date)
        outstanding_ar_filter_income.append(Cashflow.date >= start_date)
        outstanding_ap_filter_income.append(Cashflow.date >= start_date)
    if end_date:
        cashflow_filter_income.append(Cashflow.date <= end_date)
        outstanding_ar_filter_income.append(Cashflow.date <= end_date)
        outstanding_ap_filter_income.append(Cashflow.date <= end_date)
    cash_inflow = db.session.query(func.coalesce(func.sum(Cashflow.amount), 0)).filter(*cashflow_filter_income).scalar()
    total_ar_income = db.session.query(func.coalesce(func.sum(Outstanding.amount), 0)).join(Cashflow, Outstanding.cashflow_id == Cashflow.id).filter(*outstanding_ar_filter_income).scalar()
    total_ap_income = db.session.query(func.coalesce(func.sum(Outstanding.amount), 0)).join(Cashflow, Outstanding.cashflow_id == Cashflow.id).filter(*outstanding_ap_filter_income).scalar()
    actual_income = cash_inflow + total_ar_income - total_ap_income
    return actual_expense, actual_income

@views.route('/get-openai-emergency-expenses', methods=['POST'])
def fetch_expenses():
    data = request.get_json()
    type_ = data.get('type')
    location = data.get('location')

    if not type_ or not location:
        return jsonify({'error': 'Missing type or location'}), 400

    system_message = """
    You are an expert in global expense estimation for emergency planning. Based on a given emergency type and location (country), your task is to return a list of estimated average expenses in VND for relevant real-life scenarios.

    Each emergency type maps to 3 specific scenarios:
    - "Job Loss":
      • Unemployment (3 months)
      • Career transition period
      • Industry downturn coverage
    - "Medical Emergency":
      • Hospitalization (1 week)
      • Surgery and recovery
      • Chronic condition treatment
    - "Personal Asset":
      • Major car repair
      • Home appliance replacement
      • Roof or structural repair
    - "Dependents":
      • Childcare emergency
      • Elder care support
      • Family emergency travel

    Your response must be a valid JSON object containing an array of entries in the following structure:
    [
      {
        "name": "<scenario>",
        "expense": <average_expense_in_VND>
      }
    ]

    Only return the 3 scenarios related to the provided type. Use the country location to determine the average costs. It should return realistic data, 
    reflecting the social economy in the provided country (For example, developed countries should return higher prices than developing/underdeveloped countries). 
    The key for the returned outputs must be **scenarios**

    Be concise, realistic, and grounded in global or regional expense data. Do not include any explanatory text, only the JSON object.
    """

    user_message = str({
        "type": type_,
        "location": location
    })

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_message.strip()},
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"}
        )
        return jsonify(response.choices[0].message.content)
    except Exception as e:
        return jsonify({"error": str(e)}), 500