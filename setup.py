from setuptools import setup, find_packages

setup(
    name="help_cleaners_bot",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "aiogram==3.4.1",
        "sqlalchemy==2.0.27",
        "aiosqlite==0.19.0",
        "python-dotenv==1.0.1",
        "APScheduler==3.10.4",
        "pytz==2024.1",
        "gunicorn==21.2.0",
    ],
    python_requires=">=3.11",
)
