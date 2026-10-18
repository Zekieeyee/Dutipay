"""Production entrypoint (gunicorn wsgi:app).

app.py only starts its schedulers inside `if __name__ == '__main__'`,
which never runs under gunicorn. This file starts them instead.
Run with ONE worker so each job fires once (see render.yaml).
"""
from apscheduler.schedulers.background import BackgroundScheduler
from app import (app, auto_generate_payroll_job, auto_expire_interview_requests,
                 auto_timeout_job, weekly_absentee_report_job)

scheduler = BackgroundScheduler(timezone="Asia/Manila")
scheduler.add_job(auto_generate_payroll_job, 'cron', hour=1, minute=0)
scheduler.add_job(auto_expire_interview_requests, 'cron', hour=2, minute=0)
scheduler.add_job(auto_timeout_job, 'cron', hour=3, minute=0)
scheduler.add_job(weekly_absentee_report_job, 'cron', day_of_week='mon', hour=8, minute=0)
scheduler.start()

# Lightweight endpoint for uptime monitors (keeps the free Render service awake)
if 'healthz' not in app.view_functions:
    app.add_url_rule('/healthz', 'healthz', lambda: ('ok', 200))
