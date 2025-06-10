from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from os import path
from flask_login import LoginManager, current_user
from flask_migrate import Migrate

db = SQLAlchemy()
DB_NAME = "database.db"
migrate = Migrate()

def format_currency(value):
    if value is None:
        return ""
    val = float(value)
    return f"{val:,.0f} ₫"

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'VaRriors'
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'
    db.init_app(app)
    migrate.init_app(app, db)  # ✅ hook migrate to app and db
    
    from flask_login import current_user
    @app.context_processor
    def inject_user():
        return dict(user=current_user)

    from .views import views
    from .auth import auth

    app.register_blueprint(views, url_prefix='/')
    app.register_blueprint(auth, url_prefix='/')

    from .models import User, Cashflow
    __all__ = ['User', 'Cashflow']

    with app.app_context():
        db.create_all()

    login_manager = LoginManager()
    login_manager.login_view = 'auth.login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(id):
        return User.query.get(int(id))

    # Đăng ký filter currency cho Jinja2
    app.jinja_env.filters['currency'] = format_currency

    return app


def create_database(app):
    if not path.exists('website/' + DB_NAME):
        db.create_all(app=app)
        print('Database created')

from flask_sqlalchemy import SQLAlchemy


  

