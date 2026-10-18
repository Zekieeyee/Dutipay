import os
import calendar
import smtplib
import threading
import qrcode
from html import escape
from email.message import EmailMessage
from io import BytesIO
import base64
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file, render_template, session, redirect, url_for
from flask_cors import CORS
from supabase import create_client
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key-change-this")

# ✅ SECURITY: Session cookie hardening
app.config['SESSION_COOKIE_HTTPONLY'] = True      # Prevent JS access
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'     # CSRF protection
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV', 'development').lower() == 'production'  # HTTPS only in production
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)  # Session expiry
CORS(app)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ==========================================
# EMAIL NOTIFICATIONS
# ==========================================
MAIL_ENABLED = os.getenv("MAIL_ENABLED", "false").strip().lower() == "true"
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "gmail").strip().lower()

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")

BREVO_SMTP_HOST = os.getenv("BREVO_SMTP_HOST", "smtp-relay.brevo.com").strip()
BREVO_SMTP_PORT = int(os.getenv("BREVO_SMTP_PORT", "587"))
BREVO_SMTP_USER = os.getenv("BREVO_SMTP_USER", "").strip()
BREVO_SMTP_KEY = os.getenv("BREVO_SMTP_KEY", "").strip()
MAIL_FROM_EMAIL = os.getenv("MAIL_FROM_EMAIL", "").strip()
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Dutipay").strip() or "Dutipay"
DEV_CLEAR_TOKEN = os.getenv("DEV_CLEAR_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
RESET_RATE_LIMIT_SHORT = int(os.getenv("RESET_RATE_LIMIT_SHORT", "3"))
RESET_RATE_LIMIT_LONG = int(os.getenv("RESET_RATE_LIMIT_LONG", "5"))

# ==========================================
# ✅ SUPABASE RETRY WRAPPER (Windows socket fix)
# ==========================================
import time as _time_module

def sb_execute(query, max_retries=3, delay=0.15):
    """
    Execute a Supabase query with retry on WinError 10035.
    Use this instead of .execute() for critical queries.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return query.execute()
        except Exception as e:
            last_error = e
            error_str = str(e)
            # Retry only on Windows socket errors
            if 'WinError 10035' in error_str or 'non-blocking socket' in error_str:
                if attempt < max_retries - 1:
                    _time_module.sleep(delay * (attempt + 1))
                    continue
            raise
    if last_error:
        raise last_error

def mail_is_configured():
    if not MAIL_ENABLED:
        return False
    if MAIL_PROVIDER == 'brevo':
        return bool(BREVO_SMTP_HOST and BREVO_SMTP_USER and BREVO_SMTP_KEY and MAIL_FROM_EMAIL)
    return bool(GMAIL_USER and GMAIL_APP_PASSWORD)


def _build_email(heading, lines, farm_name, details=None, button=None):
    details = [(k, v) for k, v in (details or []) if v not in (None, "")]
    text_parts = [heading, ""] + list(lines)
    if details:
        text_parts += [""] + [f"{k}: {v}" for k, v in details]
    if button:
        text_parts += ["", f"{button[0]}: {button[1]}"]
    text_parts += ["", f"- {farm_name}", "Powered by Dutipay."]
    text_body = "\n".join(text_parts)

    paragraphs = "".join(
        f'<p style="margin:0 0 12px;color:#3d4a3d;font-size:14px;line-height:1.6;">{escape(l)}</p>' for l in lines
    )
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#7a887a;font-size:13px;">{escape(str(k))}</td>'
        f'<td style="padding:6px 0;color:#2d3e2d;font-size:13px;font-weight:600;">{escape(str(v))}</td></tr>'
        for k, v in details
    )
    details_html = f'<table style="margin:8px 0 16px;border-collapse:collapse;">{rows}</table>' if rows else ""
    button_html = (
        f'<p style="margin:16px 0;"><a href="{escape(button[1], quote=True)}" '
        f'style="background:#4a7c59;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:8px;'
        f'font-size:14px;font-weight:600;display:inline-block;">{escape(button[0])}</a></p>'
    ) if button else ""

    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;background:#f4f7f0;padding:24px;">'
        '<div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;border:1px solid #e0e7d5;">'
        f'<h2 style="margin:0 0 16px;color:#2d3e2d;font-size:20px;">{escape(heading)}</h2>'
        f'{paragraphs}{details_html}{button_html}'
        f'<hr style="border:none;border-top:1px solid #e0e7d5;margin:20px 0 12px;">'
        f'<p style="margin:0;color:#8a9a8a;font-size:12px;">{escape(farm_name)} &middot; Powered by Dutipay.</p>'
        '</div></div>'
    )
    return text_body, html_body


def _send_email_now(to_email, subject, text_body, html_body, reply_to=None):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        if MAIL_PROVIDER == 'brevo':
            msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_EMAIL}>"
        else:
            msg["From"] = f"{MAIL_FROM_NAME} <{GMAIL_USER}>"
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        if MAIL_PROVIDER == 'brevo':
            with smtplib.SMTP(BREVO_SMTP_HOST, BREVO_SMTP_PORT, timeout=20) as server:
                server.starttls()
                server.login(BREVO_SMTP_USER, BREVO_SMTP_KEY)
                server.send_message(msg)
        else:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        print(f"📧 Email sent to {to_email}: {subject}")
    except Exception as e:
        print(f"⚠️ Email to {to_email} failed: {e}")


def send_email(to_email, subject, heading, lines, farm_name="Dutipay", details=None, button=None, reply_to=None):
    try:
        to_email = (to_email or "").strip()
        if "@" not in to_email or " " in to_email:
            return False
        if not mail_is_configured():
            print("ℹ️ Email skipped: not configured.")
            return False
        subject = " ".join(str(subject).split())
        text_body, html_body = _build_email(heading, lines, farm_name, details, button)
        threading.Thread(target=_send_email_now,
                         args=(to_email, subject, text_body, html_body, reply_to),
                         daemon=True).start()
        return True
    except Exception as e:
        print(f"⚠️ Email queue error: {e}")
        return False


def login_button():
    return ("Log in to Dutipay", APP_URL + "/login") if APP_URL else None


def get_farm_mail_info(farm_id):
    info = {"name": "Dutipay", "owner_email": ""}
    try:
        res = supabase.table('farms').select('farm_name, display_name, owner_email').eq('id', farm_id).execute()
        if res.data:
            f = res.data[0]
            info["name"] = f.get('display_name') or f.get('farm_name') or "Dutipay"
            info["owner_email"] = f.get('owner_email') or ""
    except Exception as e:
        print(f"⚠️ get_farm_mail_info error: {e}")
    return info


def _employee_significant_changes(old, new):
    changes = []

    def diff(field, label, fmt=None):
        o = old.get(field)
        n = new.get(field)
        if o is None: o = ''
        if n is None: n = ''
        if field == 'basic_salary':
            try: o = float(o or 0)
            except: o = 0.0
            try: n = float(n or 0)
            except: n = 0.0
        if str(o) == str(n):
            return
        changes.append({'field': field, 'label': label,
                        'old': fmt(o) if fmt else o, 'new': fmt(n) if fmt else n})

    money = lambda v: f"₱{float(v or 0):,.2f}"
    diff('position', 'Position')
    diff('basic_salary', 'Basic salary', fmt=money)
    diff('schedule_in', 'Schedule (Time-In)')
    diff('schedule_out', 'Schedule (Time-Out)')
    diff('email', 'Email on file')
    return changes


def _format_interview_datetime(date_str, time_str):
    if not date_str:
        return "—"
    try:
        d = datetime.strptime(str(date_str), '%Y-%m-%d')
        date_fmt = d.strftime('%A, %B %d, %Y')
    except Exception:
        date_fmt = str(date_str)
    if not time_str:
        return date_fmt
    try:
        t = datetime.strptime(str(time_str), '%H:%M:%S')
        time_fmt = t.strftime('%I:%M %p').lstrip('0')
    except Exception:
        try:
            t = datetime.strptime(str(time_str), '%H:%M')
            time_fmt = t.strftime('%I:%M %p').lstrip('0')
        except Exception:
            time_fmt = str(time_str)
    return f"{date_fmt} at {time_fmt}"


def _resolve_requester_email(req):
    """Helper: get requester email from a worker request (None if self-registered)."""
    requested_by = req.get('requested_by')
    if not requested_by:
        return None, None
    try:
        u_res = supabase.table('users').select('employee_code').eq('id', requested_by).execute()
        if not u_res.data:
            return None, None
        m_code = u_res.data[0].get('employee_code')
        if not m_code:
            return None, None
        m_emp = supabase.table('employees').select('email, first_name').eq('employee_code', m_code).execute()
        if m_emp.data:
            return m_emp.data[0].get('email'), m_emp.data[0].get('first_name')
    except Exception as e:
        print(f"⚠️ _resolve_requester_email error: {e}")
    return None, None


def _notify_applicant_and_manager_about_interview(req, farm_info, is_reschedule=False):
    applicant_name = f"{req.get('first_name', '')} {req.get('last_name', '')}".strip()
    position = req.get('position') or 'the position'
    when = _format_interview_datetime(req.get('interview_date'), req.get('interview_time'))
    where = req.get('interview_location') or 'To be confirmed'
    notes = req.get('interview_notes') or ''

    subject_prefix = "Interview rescheduled" if is_reschedule else "Interview invitation"
    heading = "Your interview has been rescheduled" if is_reschedule else "You're invited to an interview"

    try:
        send_email(
            req.get('email'), f"{subject_prefix} — {position}", heading,
            [
                f"Hi {req.get('first_name', '')}, we'd like to meet you regarding your application for {position}.",
                "Please see the schedule below. Kindly reply to confirm your availability." if not is_reschedule
                else "Please see the updated schedule below.",
            ],
            farm_name=farm_info["name"],
            details=[("Position", position), ("When", when), ("Where / How", where),
                     ("Notes", notes) if notes else None],
            button=login_button()
        )
    except Exception as e:
        print(f"⚠️ Applicant interview email error: {e}")

    try:
        manager_email, _ = _resolve_requester_email(req)  # ✅ FIX: guard null
        send_email(
            manager_email, f"{subject_prefix}: {applicant_name}",
            "Interview scheduled" if not is_reschedule else "Interview rescheduled",
            [f"The interview for {applicant_name} ({position}) has been {'rescheduled' if is_reschedule else 'scheduled'}."],
            farm_name=farm_info["name"],
            details=[("Applicant", applicant_name), ("Position", position),
                     ("When", when), ("Where / How", where)]
        )
    except Exception as e:
        print(f"⚠️ manager interview email error: {e}")


def _notify_hire(req, farm_info, employee_code=None):
    applicant_name = f"{req.get('first_name', '')} {req.get('last_name', '')}".strip()
    position = req.get('position') or 'the position'

    try:
        send_email(
            req.get('email'), f"Welcome aboard — {position}",
            "Congratulations, you're hired!",
            [
                f"Hi {req.get('first_name', '')}, we're pleased to offer you the position of {position}.",
                "You can now log in to Dutipay using the password you created during registration.",
                "If you forgot your password, use the Forgot Password option on the login page.",
            ],
            farm_name=farm_info["name"],
            details=[("Position", position), ("Employee code", employee_code)] if employee_code else [("Position", position)],
            button=login_button()
        )
    except Exception as e:
        print(f"⚠️ Hire applicant email error: {e}")

    try:
        manager_email, _ = _resolve_requester_email(req)  # ✅ FIX
        send_email(
            manager_email, f"{applicant_name} was hired",
            "Applicant hired",
            [f"{applicant_name} has been onboarded for the position of {position}."],
            farm_name=farm_info["name"],
            details=[("Applicant", applicant_name), ("Position", position),
                     ("Employee code", employee_code)] if employee_code else [("Applicant", applicant_name), ("Position", position)]
        )
    except Exception as e:
        print(f"⚠️ Hire manager email error: {e}")


def _notify_rejection(req, farm_info, reason=''):
    applicant_name = f"{req.get('first_name', '')} {req.get('last_name', '')}".strip()
    position = req.get('position') or 'the position'

    try:
        send_email(
            req.get('email'), f"Update on your application — {position}",
            "Application update",
            [
                f"Hi {req.get('first_name', '')}, thank you for your interest and the time you spent with us.",
                "After careful review, we will not be moving forward with your application at this time. We wish you the best.",
            ],
            farm_name=farm_info["name"],
            details=[("Position", position)]
        )
    except Exception as e:
        print(f"⚠️ Rejection applicant email error: {e}")

    try:
        manager_email, _ = _resolve_requester_email(req)  # ✅ FIX
        send_email(
            manager_email, f"{applicant_name} was not selected",
            "Applicant not selected",
            [f"{applicant_name} will not be onboarded for the position of {position}."],
            farm_name=farm_info["name"],
            details=[("Applicant", applicant_name), ("Position", position), ("Reason", reason or "—")]
        )
    except Exception as e:
        print(f"⚠️ Rejection manager email error: {e}")


def notify_applicant_application_received(req, farm_info):
    applicant_name = f"{req.get('first_name', '')} {req.get('last_name', '')}".strip()
    position = req.get('position') or 'the position'
    try:
        send_email(
            req.get('email'), f"We received your application — {position}",
            "Application received",
            [
                f"Hi {req.get('first_name', '')}, thank you for your interest in the {position} position.",
                "We have received your application and it is now being reviewed by our team.",
                "If your qualifications match what we're looking for, we'll reach out to schedule an interview.",
                "We appreciate your patience and interest in joining us.",
            ],
            farm_name=farm_info["name"],
            details=[("Position applied for", position), ("Applicant", applicant_name)]
        )
    except Exception as e:
        print(f"⚠️ Application received email error: {e}")


def _sign_report_attachment(path, filename=None):
    if not path:
        return None
    try:
        res = supabase.storage.from_('report_attachments').create_signed_url(path, 3600)
        url = res.get('signedURL') or res.get('signed_url') or res.get('signedUrl')
        if not url:
            return None
        return {"url": url, "filename": filename or path.split('/')[-1]}
    except Exception as e:
        print(f"⚠️ Report signed URL error: {e}")
        return None

# ==========================================
# AUDIT LOG HELPER
# ==========================================
def log_action(action, target_type=None, target_id=None, details=None):
    """
    Log a sensitive action to audit_logs table.
    Never raises — errors are printed lang para hindi mag-break sa main flow.
    
    Usage:
        log_action('employee.updated', 'employee', emp_id, {'changes': [...]})
        log_action('payroll.received', 'payroll', payroll_id, {'employee_code': 'EMP001'})
    """
    try:
        payload = {
            "farm_id": session.get('farm_id'),
            "user_id": session.get('user_id'),
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": details or {},
            "ip_address": get_client_ip(),
            "user_agent": (request.headers.get('User-Agent', '') or '')[:500]
        }
        supabase.table('audit_logs').insert(payload).execute()
    except Exception as e:
        print(f"⚠️ Audit log error ({action}): {e}")
# ==========================================
# PASSWORD HASHING
# ==========================================
import hashlib
import secrets as _secrets_module

def hash_password(password):
    if not password:
        return ''
    salt = _secrets_module.token_hex(16)
    iterations = 100000
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    salt.encode('utf-8'), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${pwd_hash.hex()}"


def verify_password(plain_password, stored_hash):
    if not plain_password or not stored_hash:
        return False, False
    if '$' not in stored_hash:
        return (plain_password == stored_hash), True
    try:
        parts = stored_hash.split('$')
        if len(parts) != 4 or parts[0] != 'pbkdf2_sha256':
            return False, False
        iterations = int(parts[1])
        salt = parts[2]
        expected_hash = parts[3]
        computed = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'),
                                        salt.encode('utf-8'), iterations)
        return (computed.hex() == expected_hash), False
    except Exception as e:
        print(f"⚠️ Password verify error: {e}")
        return False, False
@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        data = request.json or {}
        rating = data.get('rating')
        feedback_text = (data.get('feedback_text') or '').strip()

        if not rating or rating < 1 or rating > 5:
            return jsonify({"success": False, "message": "Please provide a rating from 1 to 5."}), 400

        user_id = session.get('user_id')
        role = session.get('role')

        # Save sa DB
        supabase.table('app_feedback').insert({
            "user_id": user_id,
            "role": role,
            "rating": rating,
            "feedback_text": feedback_text or None
        }).execute()

        # Mark as seen para hindi na mag-pop up ulit
        supabase.table('users').update({"has_seen_feedback_prompt": True}).eq('id', user_id).execute()

        return jsonify({"success": True, "message": "Thank you for your feedback!"}), 200
    except Exception as e:
        print("FEEDBACK ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500

# ==========================================
# SESSION SECURITY
# ==========================================
SESSION_TIMEOUT_MINUTES = 20 
@app.before_request
def check_session_timeout():
    # ✅ 1. Skip static files at auth pages
    if request.endpoint in ('static', 'login', 'register', 'join_business_page',
                             'validate_business_code', 'submit_join_request',
                             'forgot_password', 'verify_pin', 'reset_password',
                             'home', 'privacy', 'terms',
                             'change_password_required'):
        return
    
    if not is_logged_in():
        return

    # ✅ 2. IGNORE BACKGROUND POLLING
    background_paths = [
        '/api/worker-requests/count',
        '/api/attendance/all',
        '/api/cutoff/current',
    ]
    
    if any(request.path.startswith(path) for path in background_paths):
        return  # ❌ Wag i-update ang last_activity, wag i-check ang timeout

    # ✅ 3. Check timeout (GAMITIN ANG MINUTES)
    now = datetime.now()
    last_activity = session.get('last_activity')
    
    if last_activity:
        try:
            last_dt = datetime.fromisoformat(last_activity)
            # ✅ PALITAN ITO: hours -> minutes
            if (now - last_dt) > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
                session.clear()
                return redirect('/login?timeout=1')
        except Exception as e:
            print(f"⚠️ Session timeout check error: {e}")
    
    # ✅ 4. Update last_activity
    session['last_activity'] = now.isoformat()
    session.permanent = True
    
# ✅ Ito yung para sa Session Timeout (existing na)
@app.context_processor
def inject_session_config():
    return {'SESSION_TIMEOUT_MINUTES': SESSION_TIMEOUT_MINUTES}


# ✅ Ito yung para sa Feedback Prompt (bagong dagdag)
@app.context_processor
def inject_session_meta():
    account_age_days = 0
    if is_logged_in():
        user_id = session.get('user_id')
        try:
            res = supabase.table('users').select('created_at, has_seen_feedback_prompt').eq('id', user_id).execute()
            if res.data:
                user = res.data[0]
                created_at = user.get('created_at')
                if created_at:
                    from datetime import datetime
                    created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    account_age_days = (datetime.now(created_dt.tzinfo) - created_dt).days
                
                session['has_seen_feedback_prompt'] = user.get('has_seen_feedback_prompt', False)
        except Exception as e:
            print(f"⚠️ Account age error: {e}")
            
    return {'account_age_days': account_age_days}
# ==========================================
# RATE LIMITER
# ==========================================
from collections import defaultdict
import time

_rate_limit_store = defaultdict(list)

def get_client_ip():
    cf_ip = request.headers.get('CF-Connecting-IP')
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def check_rate_limit(key, max_attempts, window_seconds):
    """
    Check if rate limit is exceeded.
    Returns: (allowed: bool, remaining_seconds: int)
    - allowed=True → pwede pa mag-attempt
    - allowed=False → locked out, remaining_seconds na ang hihintayin
    """
    now = time.time()
    cutoff = now - window_seconds
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if t > cutoff]

    if len(_rate_limit_store[key]) >= max_attempts:
        oldest = min(_rate_limit_store[key])
        remaining = int(oldest + window_seconds - now)
        return False, max(0, remaining)

    _rate_limit_store[key].append(now)
    return True, 0

def get_rate_limit_remaining(key, max_attempts, window_seconds):
    now = time.time()
    cutoff = now - window_seconds
    attempts = [t for t in _rate_limit_store[key] if t > cutoff]
    return max(0, max_attempts - len(attempts))


def clear_rate_limit(key):
    if key in _rate_limit_store:
        del _rate_limit_store[key]


def generate_random_password(length=12):
    """Generate a secure random password with mixed characters."""
    import secrets
    import string
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    password_chars = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    all_chars = lowercase + uppercase + digits + special
    for _ in range(length - 4):
        password_chars.append(secrets.choice(all_chars))
    for i in range(len(password_chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        password_chars[i], password_chars[j] = password_chars[j], password_chars[i]
    return ''.join(password_chars)


# ==========================================
# BUSINESS CODE GENERATOR
# ==========================================
import secrets
import string

def generate_business_code(business_name=""):
    letters = "ABCDEFGHJKMNPQRSTUVWXYZ"
    digits = "23456789"
    mixed = letters + digits

    def rand_block(length, pool):
        return ''.join(secrets.choice(pool) for _ in range(length))

    pattern = secrets.randbelow(4)
    if pattern == 0:
        code = f"{rand_block(4, mixed)}-{rand_block(4, mixed)}"
    elif pattern == 1:
        code = f"{rand_block(3, mixed)}-{rand_block(4, mixed)}-{rand_block(4, mixed)}"
    elif pattern == 2:
        code = f"{rand_block(4, letters)}-{rand_block(4, digits)}"
    else:
        code = f"{rand_block(2, mixed)}-{rand_block(4, mixed)}-{rand_block(2, mixed)}"
    return code


def super_admin_required(f):
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return redirect('/login')
        if session.get('role') != 'super_admin':
            return redirect('/dashboard')
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


def _attach_business_stats(businesses):
    """Add employee_count + admin info + registration info to each business."""
    if not businesses:
        return businesses
    farm_ids = [b['id'] for b in businesses]

    # Employee counts
    try:
        emp_res = supabase.table('employees').select('farm_id').in_('farm_id', farm_ids).execute()
        emp_counts = {}
        for e in (emp_res.data or []):
            emp_counts[e['farm_id']] = emp_counts.get(e['farm_id'], 0) + 1
    except Exception:
        emp_counts = {}

    # Admins per business
    try:
        admin_res = supabase.table('users').select('farm_id, username, employee_code, is_active') \
            .eq('role', 'admin').in_('farm_id', farm_ids).execute()
        admin_map = {}
        for u in (admin_res.data or []):
            admin_map.setdefault(u['farm_id'], []).append(u)
    except Exception:
        admin_map = {}

    # ✅ FIX: Match registrations by OWNER EMAIL (more reliable than business_name)
    reg_map_by_email = {}
    try:
        reg_res = supabase.table('business_registrations') \
            .select('business_name, owner_first_name, owner_last_name, owner_email') \
            .eq('status', 'Approved') \
            .execute()
        for reg in (reg_res.data or []):
            email = (reg.get('owner_email') or '').lower()
            if email:
                reg_map_by_email[email] = reg
    except Exception as e:
        print(f"⚠️ Registration info lookup error: {e}")

    for b in businesses:
        b['employee_count'] = emp_counts.get(b['id'], 0)
        b['admins'] = admin_map.get(b['id'], [])

        owner_email = (b.get('owner_email') or '').lower()
        reg = reg_map_by_email.get(owner_email)
        if reg:
            b['registration_first_name'] = reg.get('owner_first_name')
            b['registration_last_name'] = reg.get('owner_last_name')
            b['registration_email'] = reg.get('owner_email')
        else:
            b['registration_first_name'] = None
            b['registration_last_name'] = None
            b['registration_email'] = None

    return businesses


def is_logged_in():
    return 'user_id' in session


def get_farm_id():
    return session.get('farm_id')


def role_required(allowed_roles):
    def decorator(f):
        def wrapper(*args, **kwargs):
            if not is_logged_in():
                # ✅ JSON para sa API, redirect para sa pages
                if request.path.startswith('/api/'):
                    return jsonify({"success": False, "message": "Not authenticated"}), 401
                return redirect('/login')

            if session.get('role') not in allowed_roles:
                # ✅ JSON para sa API, redirect para sa pages
                if request.path.startswith('/api/'):
                    return jsonify({
                        "success": False,
                        "message": f"Access denied. Required role: {', '.join(allowed_roles)}"
                    }), 403
                return redirect('/dashboard')

            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator


def is_mobile():
    ua = request.headers.get('User-Agent', '').lower()
    keywords = ['android', 'iphone', 'ipad', 'ipod', 'windows phone', 'mobile']
    return any(k in ua for k in keywords)


def mobile_blocked(f):
    def wrapper(*args, **kwargs):
        if is_mobile():
            return render_template('mobile_blocked.html'), 403
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


# ==========================================
# QR CODE PARSER + TIME NORMALIZER
# ==========================================
def parse_qr_code(qr_code_data):
    if not qr_code_data:
        return None
    if '|' in qr_code_data and ':' in qr_code_data:
        parts = qr_code_data.split('|')
        for part in parts:
            if part.startswith('EMP:'):
                return part.replace('EMP:', '').strip()
    return qr_code_data.strip()


def normalize_time_str(v, default='08:00:00'):
    if v is None or v == '':
        return default
    s = str(v).strip()
    if len(s.split(':')) == 2:
        return s + ':00'
    if len(s.split(':')) == 3:
        return s
    parts = s.split(':')
    if len(parts) >= 3:
        return f"{parts[0]}:{parts[1]}:{parts[2]}"
    return default


# ==========================================
# CONTEXT PROCESSOR
# ==========================================
@app.context_processor
def inject_farm_branding():
    # ✅ Default Dutipay branding
    default_branding = {
        'farm_display_name': 'Dutipay',
        'farm_logo_url': None,
        'farm_tagline': 'Track time. Pay right.',
    }

    if not is_logged_in():
        return default_branding

    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return default_branding

        res = supabase.table('farms').select('farm_name, display_name, logo_url, tagline').eq('id', farm_id).execute()
        if res.data:
            farm = res.data[0]
            return {
                'farm_display_name': farm.get('display_name') or farm.get('farm_name') or 'Dutipay',
                'farm_logo_url': farm.get('logo_url'),
                'farm_tagline': farm.get('tagline') or 'Track time. Pay right.',
            }
    except Exception as e:
        print("BRANDING ERROR:", str(e))
    return default_branding


# ==========================================
# HOME
# ==========================================
@app.route('/')
def home():
    return render_template('home.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


# ==========================================
# REGISTER
# ==========================================
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            business_name = request.form.get('business_name', '').strip() or request.form.get('farm_name', '').strip()
            first_name = request.form.get('first_name', '').strip()
            last_name = request.form.get('last_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            business_type = request.form.get('business_type', '').strip()
            business_address = request.form.get('business_address', '').strip()
            registration_notes = request.form.get('registration_notes', '').strip()

            form_data = {
                'business_name': business_name, 'first_name': first_name, 'last_name': last_name,
                'email': email, 'phone': phone, 'business_type': business_type,
                'business_address': business_address, 'registration_notes': registration_notes,
            }

            if not all([business_name, first_name, last_name, email]):
                return render_template('register.html', error='All required fields must be filled.', form_data=form_data)

            existing = supabase.table('business_registrations').select('id, status') \
                .eq('owner_email', email).in_('status', ['Pending', 'Approved']).execute()
            if existing.data:
                status = existing.data[0].get('status', 'Pending')
                return render_template('register.html',
                    error=f'A registration with this email already exists (Status: {status}).',
                    form_data=form_data)

            payload = {
                "business_name": business_name, "owner_first_name": first_name,
                "owner_last_name": last_name, "owner_email": email,
                "owner_phone": phone or None, "business_type": business_type or None,
                "business_address": business_address or None,
                "registration_notes": registration_notes or None, "status": "Pending"
            }
            res = supabase.table('business_registrations').insert(payload).execute()
            if not res.data:
                return render_template('register.html', error='Failed to save registration.', form_data=form_data)

            try:
                notify_to = MAIL_FROM_EMAIL or "admin@Dutipay.app"
                send_email(notify_to, f"New business registration — {business_name}",
                    "New business registration", ["A new business has registered."],
                    farm_name="Dutipay",
                    details=[("Business", business_name), ("Owner", f"{first_name} {last_name}"),
                             ("Email", email), ("Phone", phone or "—"), ("Type", business_type or "—")],
                    button=login_button())
            except Exception as mail_err:
                print(f"⚠️ Super admin notify error: {mail_err}")

            return render_template('register.html',
                success='✅ Your business registration has been submitted!')
        except Exception as e:
            print("REGISTER ERROR:", str(e))
            return render_template('register.html', error=f'Error: {str(e)}')
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        client_ip = get_client_ip()

        # ✅ 1. IP-based rate limit — 10 attempts / 5 minutes
        ip_key = f"login_ip:{client_ip}"
        ip_allowed, ip_remaining = check_rate_limit(ip_key, max_attempts=10, window_seconds=300)
        if not ip_allowed:
            return render_template('login.html',
                error='🔒 Too many attempts. Please try again later.',
                locked_out=True)

        try:
            # ✅ 2. Lookup user FIRST
            user_res = supabase.table('users').select('*').eq('username', username).eq('is_active', True).execute()
            user_exists = bool(user_res.data)

            user_key = f"login_user:{username.lower()}"

            # ✅ 3. Username-based rate limit — 5 attempts / 15 seconds
            if user_exists:
                user_allowed, user_remaining = check_rate_limit(user_key, max_attempts=5, window_seconds=15)

                if not user_allowed:
                    return render_template('login.html',
                        error='🔒 Too many failed attempts. Please try again later.',
                        locked_out=True)

            # ✅ 4. Invalid username → generic error
            if not user_exists:
                return render_template('login.html',
                    error='❌ Invalid username or password. Please check your credentials.')

            # ✅ 5. Verify password
            db_user = user_res.data[0]
            stored_hash = db_user.get('password_hash') or ''
            is_valid, needs_rehash = verify_password(password, stored_hash)

            if not is_valid:
                # ✅ Attempts 1-5: show remaining
                remaining_attempts = get_rate_limit_remaining(user_key, 5, 15)
                return render_template('login.html',
                    error=f'Invalid username or password. {remaining_attempts} attempt(s) remaining.')

            # ✅ 6. Auto-upgrade password hash if needed
            if needs_rehash:
                try:
                    new_hash = hash_password(password)
                    supabase.table('users').update({"password_hash": new_hash}).eq('id', db_user['id']).execute()
                    print(f"✅ Auto-upgraded hash for {db_user['username']}")
                except Exception as e:
                    print(f"⚠️ Auto-upgrade failed: {e}")

            # ✅ 7. Success — clear BOTH rate limits
            clear_rate_limit(user_key)
            clear_rate_limit(ip_key)

            # ✅ 8. Get full name from employees table
            full_name = username
            try:
                emp_res = supabase.table('employees').select('first_name, last_name') \
                    .eq('employee_code', db_user.get('employee_code', username)).execute()
                if emp_res.data:
                    emp = emp_res.data[0]
                    full_name = f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip()
            except Exception as e:
                print(f"⚠️ Full name lookup failed: {e}")

            # ✅ 9. Setup session
            session.clear()
            session['user_id'] = db_user['id']
            session['username'] = db_user['username']
            session['role'] = db_user.get('role', 'employee').lower()
            session['full_name'] = full_name
            session['farm_id'] = db_user.get('farm_id')
            session['employee_code'] = db_user.get('employee_code', '')
            session['last_activity'] = datetime.now().isoformat()
            session.permanent = True

            # ✅ 10. Audit log (after successful auth)
            log_action('auth.login_success', 'user', db_user['id'], {
                'username': db_user['username'],
                'role': db_user.get('role', 'employee').lower()
            })

            # ✅ 11. Force password change on first login
            if db_user.get('must_change_password'):
                session['must_change_password'] = True
                return redirect('/change-password-required')

            # ✅ 12. Redirect based on role
            if session.get('role') == 'super_admin':
                return redirect('/super-admin')
            return redirect('/dashboard')

        except Exception as e:
            import traceback
            traceback.print_exc()
            print("LOGIN ERROR:", str(e))
            return render_template('login.html', error=f'Error: {str(e)}')

    return render_template('login.html')

# ==========================================
# DEV-ONLY ROUTES
# ==========================================
@app.route('/dev/clear-rate-limit')
def dev_clear_rate_limit():
    """
    ⚠️ DEV ONLY — Clear all rate limit data.
    Authorized via: admin login OR ?token=DEV_CLEAR_TOKEN
    Remove before production deployment.
    """
    is_admin = is_logged_in() and session.get('role') == 'admin'
    token = request.args.get('token', '')
    has_token = bool(DEV_CLEAR_TOKEN) and token == DEV_CLEAR_TOKEN

    if not (is_admin or has_token):
        return jsonify({
            "success": False,
            "message": "Access denied. Provide admin login or valid token."
        }), 403

    count = len(_rate_limit_store)
    _rate_limit_store.clear()
    return f"✅ Cleared {count} rate limit entries. You can test again.", 200


@app.route('/dev/rate-limit-status')
def dev_rate_limit_status():
    """
    ⚠️ DEV ONLY — View rate limit status.
    Authorized via: admin login OR ?token=DEV_CLEAR_TOKEN
    """
    is_admin = is_logged_in() and session.get('role') == 'admin'
    token = request.args.get('token', '')
    has_token = bool(DEV_CLEAR_TOKEN) and token == DEV_CLEAR_TOKEN

    if not (is_admin or has_token):
        return jsonify({"success": False, "message": "Access denied."}), 403

    client_ip = get_client_ip()
    ip_key = f"login_ip:{client_ip}"

    return jsonify({
        "success": True,
        "client_ip": client_ip,
        "ip_attempts": len(_rate_limit_store.get(ip_key, [])),
        "store_size": len(_rate_limit_store),
        "keys": list(_rate_limit_store.keys()),
    }), 200

@app.route('/change-password-required', methods=['GET', 'POST'])
def change_password_required():
    """Force password change for admin accounts with must_change_password flag."""
    if not is_logged_in():
        return redirect('/login')

    if not session.get('must_change_password'):
        return redirect('/dashboard')

    error = None
    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not new_password or not confirm_password:
            error = 'Please fill in all fields.'
        elif new_password != confirm_password:
            error = 'Passwords do not match.'
        elif len(new_password) < 8:
            error = 'Password must be at least 8 characters.'
        else:
            try:
                user_id = session.get('user_id')
                hashed = hash_password(new_password)
                supabase.table('users').update({
                    "password_hash": hashed,
                    "must_change_password": False
                }).eq('id', user_id).execute()

                session.pop('must_change_password', None)
                session['last_activity'] = datetime.now().isoformat()

                # Redirect based on role
                if session.get('role') == 'super_admin':
                    return redirect('/super-admin')
                return redirect('/dashboard')
            except Exception as e:
                error = f'Failed to update password: {str(e)}'

    # Simple inline HTML (no template needed)
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Change Password Required</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ font-family: 'Inter', sans-serif; background: #f8faf5; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }}
            .box {{ background: white; border-radius: 20px; padding: 40px; max-width: 460px; width: 100%; box-shadow: 0 20px 60px rgba(74,124,89,0.15); border: 1px solid #e0e7d5; }}
            h1 {{ font-size: 22px; margin: 0 0 8px; color: #2d3e2d; }}
            p {{ color: #5a6b5a; font-size: 14px; margin-bottom: 24px; line-height: 1.6; }}
            label {{ display: block; font-size: 13px; font-weight: 600; color: #5a6b5a; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
            input {{ width: 100%; padding: 14px; border: 2px solid #e0e7d5; border-radius: 12px; font-size: 14px; margin-bottom: 18px; box-sizing: border-box; font-family: inherit; }}
            input:focus {{ outline: none; border-color: #4a7c59; box-shadow: 0 0 0 4px rgba(74,124,89,0.1); }}
            button {{ width: 100%; padding: 16px; background: linear-gradient(135deg, #4a7c59, #3d6647); color: white; border: none; border-radius: 12px; font-size: 15px; font-weight: 700; cursor: pointer; font-family: inherit; }}
            button:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(74,124,89,0.35); }}
            .alert {{ background: rgba(196,92,74,0.1); color: #c45c4a; border: 1px solid rgba(196,92,74,0.2); padding: 12px 16px; border-radius: 10px; font-size: 13px; margin-bottom: 20px; }}
            .info {{ background: rgba(233,196,106,0.15); color: #8a6d20; border-left: 4px solid #e9c46a; padding: 14px 18px; border-radius: 10px; font-size: 13px; margin-bottom: 24px; }}
        </style>
    </head>
    <body>
        <div class="box">
            <h1>🔐 Change Your Password</h1>
            <div class="info">
                <strong>Security Notice:</strong> Your account was created with a temporary password. Please set a new password to continue.
            </div>
            {f'<div class="alert"><i class="fas fa-exclamation-circle"></i> {error}</div>' if error else ''}
            <form method="POST">
                <label>New Password</label>
                <input type="password" name="new_password" required minlength="8" placeholder="Minimum 8 characters" autofocus>
                <label>Confirm New Password</label>
                <input type="password" name="confirm_password" required minlength="8" placeholder="Re-enter new password">
                <button type="submit"><i class="fas fa-save"></i> Update Password</button>
            </form>
        </div>
    </body>
    </html>
    """


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ==========================================
# DASHBOARD
# ==========================================
@app.route('/dashboard')
def dashboard():
    if not is_logged_in():
        return redirect('/login')

    role = session.get('role')
    full_name = session.get('full_name')
    farm_id = session.get('farm_id')

    if role == 'super_admin':
        return redirect('/super-admin')

    try:
        auto_expire_interview_requests(farm_id=farm_id, grace_days=3)
    except Exception as exp_err:
        print(f"⚠️ Auto-expire check failed: {exp_err}")

    try:
        emp_query = supabase.table('employees').select('*').eq('is_active', True)
        if farm_id and farm_id != 0:
            emp_query = emp_query.eq('farm_id', farm_id)
        emp_res = emp_query.execute()
        employees_only = [emp for emp in emp_res.data
                          if emp.get('position') not in ('Farm Owner', 'Owner', 'Business Owner', 'Company Owner')]
        total_employees = len(employees_only)

        today = datetime.now().date().isoformat()
        att_query = supabase.table('attendance').select('*').eq('attendance_date', today)
        if farm_id and farm_id != 0:
            att_query = att_query.eq('farm_id', farm_id)
        att_res = att_query.execute()

        present_ids = set()
        late_ids = set()
        for a in att_res.data:
            if a.get('time_in'):
                present_ids.add(a.get('employee_id'))
            if a.get('late_minutes', 0) > 0:
                late_ids.add(a.get('employee_id'))
        present_today = len(present_ids)
        late_today = len(late_ids)

        pay_query = supabase.table('payroll').select('*').is_('received_at', 'null')
        if farm_id and farm_id != 0:
            pay_query = pay_query.eq('farm_id', farm_id)
        pay_res = pay_query.execute()

        pending_set = set()
        for p in pay_res.data:
            pending_set.add(f"{p.get('employee_id')}_{p.get('pay_period_start')}_{p.get('pay_period_end')}")
        pending_payroll = len(pending_set)
    except Exception as e:
        print("DASHBOARD ERROR:", str(e))
        total_employees = present_today = late_today = pending_payroll = 0

    employee_stats = {'days_worked': 0, 'hours_worked': 0, 'late_count': 0,
                      'last_net_pay': 0, 'recent_attendance': []}

    if role not in ('admin', 'manager', 'super_admin'):
        try:
            emp_code = session.get('employee_code')
            emp_id = None
            if emp_code:
                emp_res = supabase.table('employees').select('id').eq('employee_code', emp_code).execute()
                if emp_res.data:
                    emp_id = emp_res.data[0]['id']

            if emp_id:
                cutoff = get_current_cutoff()
                period_start, period_end = cutoff['start'], cutoff['end']

                att_res = supabase.table('attendance').select('*').eq('employee_id', emp_id) \
                    .gte('attendance_date', period_start).lte('attendance_date', period_end).execute()
                attendance = att_res.data or []
                days_worked = len([a for a in attendance if a.get('time_in')])

                total_hours = 0
                for a in attendance:
                    if a.get('time_in') and a.get('time_out'):
                        try:
                            t_in = datetime.fromisoformat(a['time_in'].replace('Z', '+00:00'))
                            t_out = datetime.fromisoformat(a['time_out'].replace('Z', '+00:00'))
                            total_hours += (t_out - t_in).total_seconds() / 3600
                        except Exception as e:
                            print(f"⚠️ Hours error: {e}")
                hours_worked = round(total_hours, 1)

                late_count = len([a for a in attendance if a.get('late_minutes', 0) > 0])

                pay_res = supabase.table('payroll').select('net_pay').eq('employee_id', emp_id) \
                    .order('pay_period_start', desc=True).limit(1).execute()
                last_net_pay = float(pay_res.data[0].get('net_pay') or 0) if pay_res.data else 0

                recent_res = supabase.table('attendance').select('*').eq('employee_id', emp_id) \
                    .order('attendance_date', desc=True).limit(5).execute()

                employee_stats = {'days_worked': days_worked, 'hours_worked': hours_worked,
                                  'late_count': late_count, 'last_net_pay': last_net_pay,
                                  'recent_attendance': recent_res.data or []}
        except Exception as e:
            print("EMPLOYEE STATS ERROR:", str(e))

    stats = {'total_employees': total_employees, 'present_today': present_today,
             'late_today': late_today, 'pending_payroll': pending_payroll}

    if role == 'admin':
        return render_template('dashboard_admin.html', role=role, full_name=full_name, active_page='dashboard', **stats)
    elif role == 'manager':
        return render_template('dashboard_manager.html', role=role, full_name=full_name, active_page='dashboard', **stats)
    return render_template('dashboard_staff.html', role=role, full_name=full_name,
                           active_page='dashboard', **stats, **employee_stats)


@app.route('/my-qr')
def my_qr_page():
    if not is_logged_in():
        return redirect('/login')
    if session.get('role') == 'super_admin':
        return redirect('/super-admin')
    return render_template('my_qr.html', role=session.get('role'),
                           full_name=session.get('full_name'),
                           employee_code=session.get('employee_code'),
                           active_page='my_qr')


# ==========================================
# EMPLOYEE SELF-REGISTRATION
# ==========================================
@app.route('/join', methods=['GET'])
def join_business_page():
    prefill_code = request.args.get('code', '').strip().upper()
    return render_template('join_business.html', prefill_code=prefill_code)


@app.route('/api/join/validate-code', methods=['POST'])
def validate_business_code():
    client_ip = get_client_ip()
    allowed, remaining = check_rate_limit(f"validate_code:{client_ip}", max_attempts=10, window_seconds=900)
    if not allowed:
        return jsonify({"success": False,
                        "message": f"Too many code attempts. Try again in {remaining // 60} minute(s)."}), 429
    try:
        data = request.json or {}
        code = (data.get('business_code') or '').strip().upper()
        if not code:
            return jsonify({"success": False, "message": "Business code is required."}), 400
        res = supabase.table('farms').select('id, farm_name, display_name').eq('business_code', code).execute()
        if not res.data:
            remaining_tries = get_rate_limit_remaining(f"validate_code:{client_ip}", 10, 900)
            return jsonify({"success": False,
                            "message": f"Business code not found. {remaining_tries} attempt(s) remaining.",
                            "attempts_remaining": remaining_tries}), 404
        farm = res.data[0]
        clear_rate_limit(f"validate_code:{client_ip}")
        return jsonify({"success": True, "data": {
            "farm_id": farm['id'],
            "business_name": farm.get('display_name') or farm.get('farm_name')
        }}), 200
    except Exception as e:
        print("VALIDATE CODE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/join/submit', methods=['POST'])
def submit_join_request():
    client_ip = get_client_ip()
    allowed, remaining = check_rate_limit(f"join_submit:{client_ip}", max_attempts=5, window_seconds=3600)
    if not allowed:
        return jsonify({"success": False,
                        "message": f"Too many registration attempts. Try again in {remaining // 60} minute(s)."}), 429

    try:
        if request.content_type and 'multipart/form-data' in request.content_type:
            data = request.form
            files = request.files
        else:
            data = request.json or {}
            files = {}

        business_code = (data.get('business_code') or '').strip().upper()
        first_name = (data.get('first_name') or '').strip()
        last_name = (data.get('last_name') or '').strip()
        email = (data.get('email') or '').strip().lower()
        phone = (data.get('phone') or '').strip()
        desired_position = (data.get('desired_position') or '').strip()
        password = (data.get('password') or '').strip()
        confirm_password = (data.get('confirm_password') or '').strip()
        address = (data.get('address') or '').strip()
        date_of_birth = (data.get('date_of_birth') or '').strip()
        emergency_contact_name = (data.get('emergency_contact_name') or '').strip()
        emergency_contact_phone = (data.get('emergency_contact_phone') or '').strip()

        if not all([business_code, first_name, last_name, email, password]):
            return jsonify({"success": False, "message": "Please fill all required fields."}), 400
        if password != confirm_password:
            return jsonify({"success": False, "message": "Passwords do not match."}), 400
        if len(password) < 6:
            return jsonify({"success": False, "message": "Password must be at least 6 characters."}), 400

        farm_res = supabase.table('farms').select('id').eq('business_code', business_code).execute()
        if not farm_res.data:
            return jsonify({"success": False, "message": "Invalid business code."}), 404
        farm_id = farm_res.data[0]['id']

        existing = supabase.table('worker_requests').select('id, status') \
            .eq('farm_id', farm_id).eq('email', email) \
            .in_('status', ['Pending', 'For Interview', 'Approved']).execute()
        if existing.data:
            return jsonify({"success": False,
                            "message": "An application with this email already exists."}), 400

        existing_emp = supabase.table('employees').select('id') \
            .eq('farm_id', farm_id).eq('email', email).execute()
        if existing_emp.data:
            return jsonify({"success": False,
                            "message": "This email is already registered as an employee."}), 400

        resume_url = resume_filename = None
        if 'resume' in files:
            f = files['resume']
            if f and f.filename:
                try:
                    file_bytes = f.read()
                    if len(file_bytes) > 10 * 1024 * 1024:
                        return jsonify({"success": False, "message": "Resume too large. Max 10MB."}), 400
                    resume_filename = f.filename
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = f.filename.replace(' ', '_')
                    unique_filename = f"self_reg/{farm_id}/{ts}_resume_{safe_name}"
                    supabase.storage.from_('worker_documents').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": f.content_type})
                    resume_url = unique_filename
                except Exception as e:
                    print(f"❌ Resume upload error: {e}")
                    return jsonify({"success": False, "message": f"Resume upload failed: {str(e)}"}), 500

        bio_data_url = bio_data_filename = None
        if 'bio_data' in files:
            f = files['bio_data']
            if f and f.filename:
                try:
                    file_bytes = f.read()
                    if len(file_bytes) > 10 * 1024 * 1024:
                        return jsonify({"success": False, "message": "Bio data too large. Max 10MB."}), 400
                    bio_data_filename = f.filename
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = f.filename.replace(' ', '_')
                    unique_filename = f"self_reg/{farm_id}/{ts}_bio_{safe_name}"
                    supabase.storage.from_('worker_documents').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": f.content_type})
                    bio_data_url = unique_filename
                except Exception as e:
                    print(f"❌ Bio data upload error: {e}")
                    return jsonify({"success": False, "message": f"Bio data upload failed: {str(e)}"}), 500

        payload = {
            "farm_id": farm_id, "requested_by": None,
            "first_name": first_name, "last_name": last_name,
            "email": email, "phone": phone or None,
            "position": desired_position or None, "desired_position": desired_position or None,
            "applicant_password": password, "address": address or None,
            "date_of_birth": date_of_birth or None,
            "emergency_contact_name": emergency_contact_name or None,
            "emergency_contact_phone": emergency_contact_phone or None,
            "resume_url": resume_url, "resume_filename": resume_filename,
            "bio_data_url": bio_data_url, "bio_data_filename": bio_data_filename,
            "status": "Pending", "source": "self_registration"
        }
        res = supabase.table('worker_requests').insert(payload).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to save registration."}), 500

        try:
            farm_info = get_farm_mail_info(farm_id)
            send_email(farm_info['owner_email'],
                f"New employee registration — {first_name} {last_name}",
                "New employee registration",
                ["A new employee has self-registered."],
                farm_name=farm_info['name'],
                details=[("Name", f"{first_name} {last_name}"), ("Email", email),
                         ("Phone", phone or "—"), ("Desired Position", desired_position or "—"),
                         ("Resume attached", "Yes" if resume_url else "No"),
                         ("Bio data attached", "Yes" if bio_data_url else "No")],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ admin notify error: {mail_err}")

        return jsonify({"success": True,
                        "message": "✅ Registration submitted!"}), 201
    except Exception as e:
        print("SUBMIT JOIN REQUEST ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
from flask import jsonify

@app.errorhandler(401)
def unauthorized(e):
    if request.path.startswith('/api/'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    return redirect('/login')

@app.errorhandler(403)
def forbidden(e):
    if request.path.startswith('/api/'):
        return jsonify({"success": False, "message": "Forbidden"}), 403
    return redirect('/login')


@app.route('/employees')
def employees_page():
    if not is_logged_in():
        return redirect('/login')
    return render_template('employees.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='employees')


@app.route('/api/employees', methods=['GET'])
def get_employees():
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')

        # ✅ Role check — admin/manager lang pwede
        if role not in ('admin', 'manager'):
            return jsonify({"success": False, "message": "Access denied"}), 403

        query = supabase.table('employees').select(
            'id, employee_code, first_name, last_name, email, position, '
            'basic_salary, schedule_in, schedule_out, is_active, farm_id, '
            'qr_code_data, created_at, updated_at, department, middle_name'
        )
        if farm_id and farm_id != 0:
            query = query.eq('farm_id', farm_id)
        res = query.execute()
        employees = [emp for emp in res.data
                     if emp.get('position') not in ('Farm Owner', 'Owner', 'Business Owner', 'Company Owner')]

        today = datetime.now().date()
        ABSENT_THRESHOLD_DAYS = 14

        for emp in employees:
            try:
                att_res = supabase.table('attendance').select('attendance_date') \
                    .eq('employee_id', emp['id']).order('attendance_date', desc=True).limit(1).execute()
                if att_res.data:
                    last_date_str = att_res.data[0]['attendance_date']
                    last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date()
                    days_since = (today - last_date).days
                    emp['days_since_attendance'] = days_since
                    emp['last_attendance_date'] = last_date_str
                    emp['is_absent_flagged'] = (days_since >= ABSENT_THRESHOLD_DAYS and emp.get('is_active', True))
                else:
                    emp['days_since_attendance'] = None
                    emp['last_attendance_date'] = None
                    emp['is_absent_flagged'] = False
            except Exception as inner_e:
                print(f"Att check error for {emp['id']}: {inner_e}")
                emp['days_since_attendance'] = None
                emp['last_attendance_date'] = None
                emp['is_absent_flagged'] = False
        return jsonify({"success": True, "data": employees}), 200
    except Exception as e:
        print("GET EMPLOYEES ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/employees', methods=['POST'])
@role_required(['admin'])
def add_employee():
    """✅ FIX: Uses hashed_password (was plain-text)"""
    try:
        data = request.json
        farm_id = session.get('farm_id')
        current_role = session.get('role')
        if not farm_id:
            return jsonify({"success": False, "message": "No company assigned"}), 403

        position = data.get('position', 'Staff')
        role = 'manager' if position.lower() == 'manager' else 'staff'
        if role == 'manager' and current_role != 'admin':
            return jsonify({"success": False, "message": "❌ Only admin can create manager accounts."}), 403

        count_res = supabase.table('employees').select('id', count='exact').eq('farm_id', farm_id).execute()
        total_count = count_res.count if count_res.count else 0
        owner_count = supabase.table('employees').select('id', count='exact') \
            .eq('farm_id', farm_id).eq('position', 'Farm Owner').execute()
        owner_total = owner_count.count if owner_count.count else 0
        employee_number = (total_count - owner_total) + 1
        new_employee_code = f"EMP{employee_number:03d}"

        while True:
            existing = supabase.table('employees').select('id').eq('employee_code', new_employee_code).execute()
            if not existing.data:
                break
            employee_number += 1
            new_employee_code = f"EMP{employee_number:03d}"

        qr_string = f"EMP:{new_employee_code}|NAME:{data.get('first_name')} {data.get('last_name')}|ROLE:{position}"

        employee_data = {
            "employee_code": new_employee_code,
            "first_name": data.get('first_name'), "last_name": data.get('last_name'),
            "email": data.get('email'), "position": position,
            "basic_salary": float(data.get('basic_salary', 0)),
            "schedule_in": normalize_time_str(data.get('schedule_in'), '08:00:00'),
            "schedule_out": normalize_time_str(data.get('schedule_out'), '17:00:00'),
            "qr_code_data": qr_string, "is_active": True, "farm_id": farm_id
        }
        res = supabase.table('employees').insert(employee_data).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to create employee"}), 500

        username = new_employee_code.lower()
        default_password = "password123"
        hashed_password = hash_password(default_password)  # ✅ still computed for future

        user_data = {
            "username": username,
            "password_hash": hashed_password,  # ✅ FIXED: was default_password
            "role": role,
            "employee_code": new_employee_code,
            "is_active": True, "farm_id": farm_id
        }
        existing_user = supabase.table('users').select('id').eq('username', username).execute()
        if not existing_user.data:
            user_res = supabase.table('users').insert(user_data).execute()
            if not user_res.data:
                supabase.table('employees').delete().eq('id', res.data[0]['id']).execute()
                return jsonify({"success": False, "message": "Failed to create user account"}), 500

        try:
            farm_info = get_farm_mail_info(farm_id)
            send_email(data.get('email'),
                f"Welcome to {farm_info['name']} — your account is ready",
                "Welcome aboard!",
                [f"Hi {data.get('first_name', '')}, an employee account has been created for you.",
                 "Your administrator will give you your temporary password."],
                farm_name=farm_info["name"],
                details=[("Employee code / username", new_employee_code), ("Position", position)],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ Welcome email error: {mail_err}")

        return jsonify({"success": True, "data": res.data,
                        "employee_code": new_employee_code, "username": username,
                        "password": default_password,
                        "message": f"✅ Employee created! Login: {username} / {default_password}"}), 201
    except Exception as e:
        print("ADD EMPLOYEE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/employees/<int:emp_id>', methods=['PUT'])
@role_required(['admin'])
def update_employee(emp_id):
    try:
        data = request.json
        farm_id = session.get('farm_id')
        check = supabase.table('employees').select('*').eq('id', emp_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Employee not found"}), 404
        old_emp = check.data[0]
        if farm_id and farm_id != 0 and old_emp.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403

        first_name = data.get('first_name')
        last_name = data.get('last_name')
        position = data.get('position')
        email = data.get('email')
        basic_salary = float(data.get('basic_salary', 0) or 0)
        schedule_in = data.get('schedule_in')
        schedule_out = data.get('schedule_out')
        emp_code = old_emp.get('employee_code', '')
        qr_string = f"EMP:{emp_code}|NAME:{first_name} {last_name}|ROLE:{position}"

        update_data = {
            "first_name": first_name, "last_name": last_name, "email": email,
            "position": position, "basic_salary": basic_salary,
            "schedule_in": normalize_time_str(schedule_in, '08:00:00'),
            "schedule_out": normalize_time_str(schedule_out, '17:00:00'),
            "qr_code_data": qr_string
        }
        res = supabase.table('employees').update(update_data).eq('id', emp_id).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Update failed"}), 500

        new_emp = res.data[0]
        changes = _employee_significant_changes(old_emp, new_emp)
        if changes:
            # ✅ Audit log
            log_action(
                'employee.updated',
                'employee',
                emp_id,
                {
                    'employee_code': emp_code,
                    'changes': [{'field': c['field'], 'old': str(c['old']), 'new': str(c['new'])} for c in changes]
                }
            )
            
        try:
                farm_info = get_farm_mail_info(farm_id)
                changed_labels = ", ".join(c['label'] for c in changes)
                details = [(c['label'], f"{c['old']} → {c['new']}") for c in changes]
                send_email(new_emp.get('email'),
                    f"Your employee record was updated ({changed_labels})",
                    "Your employee record was updated",
                    [f"Hi {new_emp.get('first_name', '')}, the following details changed:"],
                    farm_name=farm_info["name"], details=details, button=login_button())

                email_changed = next((c for c in changes if c['field'] == 'email'), None)
                if email_changed and email_changed['old']:
                    send_email(email_changed['old'],
                        "Your email on file was changed",
                        "Your email on file was changed",
                        [f"Hi {new_emp.get('first_name', '')}, the email address changed.",
                         "If you did not request this change, contact your administrator immediately."],
                        farm_name=farm_info["name"],
                        details=[("Old email", email_changed['old']), ("New email", email_changed['new'])])
        except Exception as mail_err:
                print(f"⚠️ Employee update email error: {mail_err}")

        return jsonify({"success": True, "data": res.data, "message": "Employee updated!"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/employees/<int:emp_id>/toggle-status', methods=['POST'])
def toggle_employee_status(emp_id):
    try:
        farm_id = session.get('farm_id')
        check = supabase.table('employees').select('*').eq('id', emp_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Employee not found"}), 404
        emp = check.data[0]
        if farm_id and farm_id != 0 and emp.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if emp.get('position') == 'Farm Owner':
            return jsonify({"success": False, "message": "Cannot deactivate company owner"}), 403

        new_status = not emp.get('is_active', True)
        supabase.table('employees').update({"is_active": new_status}).eq('id', emp_id).execute()
        username = emp.get('employee_code', '').lower()
        supabase.table('users').update({"is_active": new_status}).eq('username', username).execute()
        status_text = "Activated" if new_status else "Deactivated"

        try:
            farm_info = get_farm_mail_info(emp.get('farm_id'))
            if new_status:
                send_email(emp.get('email'), "Your account has been reactivated",
                    "Account reactivated",
                    [f"Hi {emp.get('first_name', '')}, your employee account has been reactivated."],
                    farm_name=farm_info["name"], button=login_button())
            else:
                send_email(emp.get('email'), "Your account has been deactivated",
                    "Account deactivated",
                    [f"Hi {emp.get('first_name', '')}, your employee account has been deactivated."],
                    farm_name=farm_info["name"])
        except Exception as mail_err:
            print(f"⚠️ Status email error: {mail_err}")
        log_action('employee.status_changed', 'employee', emp_id, {
            'employee_code': username,
            'new_status': 'active' if new_status else 'inactive',
            'changed_by': session.get('username')
        })

        return jsonify({"success": True, "message": f"✅ Employee {status_text}!", "is_active": new_status}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/employees/<int:emp_id>/qr')
def generate_qr(emp_id):
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')
        current_employee_code = session.get('employee_code')

        emp_res = supabase.table('employees').select('*').eq('id', emp_id).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        emp = emp_res.data[0]

        # ✅ Verify farm ownership
        if farm_id and farm_id != 0 and emp.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403

        # ✅ Staff can only view their OWN QR
        if role not in ('admin', 'manager'):
            if emp.get('employee_code') != current_employee_code:
                return jsonify({"success": False, "message": "You can only view your own QR code"}), 403

        qr_data = f"EMP:{emp['employee_code']}|NAME:{emp['first_name']} {emp['last_name']}|ROLE:{emp.get('position', 'Staff')}"
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_data); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = BytesIO(); img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        return jsonify({
            "success": True,
            "qr_code": f"data:image/png;base64,{img_str}",
            "employee_id": emp['id'],
            "employee_name": f"{emp['first_name']} {emp['last_name']}",
            "employee_code": emp['employee_code'],
            "position": emp.get('position', 'Staff'),
            "qr_data": qr_data
        }), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/me/qr')
def my_qr():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        emp_code = session.get('employee_code')
        if not emp_code:
            return jsonify({"success": False, "message": "No employee code in session"}), 404
        emp_res = supabase.table('employees').select('*').eq('employee_code', emp_code).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Employee record not found"}), 404
        emp = emp_res.data[0]
        qr_data = f"EMP:{emp['employee_code']}|NAME:{emp['first_name']} {emp['last_name']}|ROLE:{emp.get('position', 'Employee')}"
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_data); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = BytesIO(); img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        return jsonify({"success": True, "qr_code": f"data:image/png;base64,{img_str}",
                        "employee_code": emp['employee_code'],
                        "employee_name": f"{emp['first_name']} {emp['last_name']}",
                        "position": emp.get('position', 'Employee'),
                        "business_name": get_farm_mail_info(emp.get('farm_id'))['name']}), 200
    except Exception as e:
        print("MY QR ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# FORGOT PASSWORD
# ==========================================
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            if not username:
                return render_template('forgot_password.html', error='Please enter your username.')

            # ✅ SECURITY: Check rate limits BEFORE anything else
            user_key_short = f"reset_short:{username.lower()}"
            user_key_long = f"reset_long:{username.lower()}"
            
            short_allowed, short_remain = check_rate_limit(user_key_short, max_attempts=RESET_RATE_LIMIT_SHORT, window_seconds=3600)  # 1 hour
            if not short_allowed:
                # Hindi natin sinasabi kung existing o hindi — generic message
                return render_template('forgot_password.html',
                    error=f'Too many reset requests. Please try again in {short_remain // 60} minute(s).',
                    username=username)

            long_allowed, long_remain = check_rate_limit(user_key_long, max_attempts=RESET_RATE_LIMIT_LONG, window_seconds=86400)  # 24 hours
            if not long_allowed:
                return render_template('forgot_password.html',
                    error='Too many reset requests. Please try again after 24 hours.',
                    username=username)

            # ✅ Look up user
            user_res = supabase.table('users').select('*').eq('username', username).eq('is_active', True).execute()
            
            # ✅ SECURITY: Always show the same message (hindi mag-reveal kung existing o hindi)
            generic_success = 'If an account exists with this username, a verification code has been sent.'
            
            if not user_res.data:
                return render_template('forgot_password.html',
                    success=generic_success,
                    username=username, show_pin_form=True)

            db_user = user_res.data[0]

            # ✅ Get email
            user_email = None
            emp_code = db_user.get('employee_code') or username
            if emp_code:
                emp_res = supabase.table('employees').select('email, first_name').eq('employee_code', emp_code).execute()
                if emp_res.data:
                    user_email = emp_res.data[0].get('email')

            if not user_email:
                # ✅ Same generic message — huwag sabihin na walang email
                return render_template('forgot_password.html',
                    success=generic_success,
                    username=username, show_pin_form=True)

            # ✅ Generate PIN
            import secrets
            pin = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
            expires_at = (datetime.now() + timedelta(minutes=10)).isoformat()

            # ✅ Invalidate old PIN (single-use)
            supabase.table('users').update({
                "reset_token": pin,
                "reset_token_expires_at": expires_at,
                "reset_attempts": 0,
                "reset_lockout_until": None
            }).eq('id', db_user['id']).execute()

            # ✅ Send email
            try:
                send_email(user_email, f"Your Dutipay verification code: {pin}",
                    "Your verification code",
                    [f"Hi {db_user.get('username', 'there')}, here is your password reset code:",
                     "Your code is valid for 10 minutes.",
                     "If you didn't request this, ignore this email."],
                    farm_name="Dutipay", details=[("Verification Code", pin)])
            except Exception as mail_err:
                print(f"⚠️ PIN email error: {mail_err}")
                return render_template('forgot_password.html', error='Failed to send email. Please try again later.')

            # ✅ Mask email
            masked_email = user_email
            if '@' in user_email:
                parts = user_email.split('@')
                local, domain = parts[0], parts[1]
                masked_email = (local[0] + '*' * (len(local) - 2) + local[-1] + '@' + domain) if len(local) > 2 else local[0] + '*@' + domain

            return render_template('forgot_password.html',
                success=f'If this account exists, a 6-digit code was sent to {masked_email}',
                username=username, user_email=user_email,
                masked_email=masked_email, show_pin_form=True)

        except Exception as e:
            print("FORGOT PASSWORD ERROR:", str(e))
            return render_template('forgot_password.html', error='An error occurred. Please try again.')

    return render_template('forgot_password.html')

@app.route('/verify-pin', methods=['POST'])
def verify_pin():
    try:
        username = request.form.get('username', '').strip()
        pin = request.form.get('pin', '').strip()
        if not username or not pin:
            return render_template('forgot_password.html',
                error='Please enter the verification code.', username=username,
                show_pin_form=True, success='Enter the 6-digit code')
        if len(pin) != 6 or not pin.isdigit():
            return render_template('forgot_password.html',
                error='Verification code must be 6 digits.', username=username,
                show_pin_form=True, success='Enter the 6-digit code')

        user_res = supabase.table('users').select('*').eq('username', username).eq('is_active', True).execute()
        if not user_res.data:
            # ✅ Generic error — don't reveal user existence
            return render_template('forgot_password.html',
                error='Invalid or expired verification code.', show_pin_form=True, username=username)

        db_user = user_res.data[0]
        attempts = db_user.get('reset_attempts') or 0
        
        if attempts >= 5:
            return render_template('forgot_password.html',
                error='Too many failed attempts. Please try again in 2 minutes.', 
                show_pin_form=False, username=username)

        if db_user.get('reset_token') != pin:
            new_attempts = attempts + 1
            supabase.table('users').update({"reset_attempts": new_attempts}).eq('id', db_user['id']).execute()
            
            if (5 - new_attempts) <= 0:
                lockout_until = (datetime.now() + timedelta(minutes=2)).isoformat()
                supabase.table('users').update({"reset_lockout_until": lockout_until}).eq('id', db_user['id']).execute()
                return render_template('forgot_password.html',
                    error='Too many failed attempts. Please try again in 2 minutes.', 
                    show_pin_form=False, username=username)
            else:
                return render_template('forgot_password.html',
                    error=f'Invalid code. {5 - new_attempts} attempt(s) remaining.',
                    show_pin_form=True, username=username, success='Enter the 6-digit code')

        # ✅ Check expiry
        expires_at = db_user.get('reset_token_expires_at')
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
                if datetime.now(expires_dt.tzinfo) > expires_dt:
                    return render_template('forgot_password.html',
                        error='Verification code expired. Please request a new one.', 
                        show_pin_form=False, username=username)
            except Exception as e:
                print(f"Expiry check error: {e}")

        # ✅ Success — generate secure token for password reset
        import secrets
        reset_token = secrets.token_urlsafe(32)
        supabase.table('users').update({
            "reset_token": reset_token,
            "reset_token_expires_at": (datetime.now() + timedelta(minutes=15)).isoformat(),
            "reset_attempts": 0
        }).eq('id', db_user['id']).execute()

        return redirect(f'/reset-password?token={reset_token}')

    except Exception as e:
        print("VERIFY PIN ERROR:", str(e))
        return render_template('forgot_password.html', error='An error occurred. Please try again.',
            show_pin_form=True, username=request.form.get('username', ''))


@app.route('/resend-pin', methods=['POST'])
def resend_pin():
    try:
        username = request.form.get('username', '').strip()
        if not username:
            return jsonify({"success": False, "message": "Username required"}), 400
        user_res = supabase.table('users').select('*').eq('username', username).eq('is_active', True).execute()
        if not user_res.data:
            return jsonify({"success": False, "message": "User not found"}), 404
        db_user = user_res.data[0]
        user_email = None
        emp_code = db_user.get('employee_code') or username
        if emp_code:
            emp_res = supabase.table('employees').select('email, first_name').eq('employee_code', emp_code).execute()
            if emp_res.data:
                user_email = emp_res.data[0].get('email')
        if not user_email:
            return jsonify({"success": False, "message": "No email on file"}), 400
        import secrets
        pin = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
        expires_at = (datetime.now() + timedelta(minutes=10)).isoformat()
        supabase.table('users').update({
            "reset_token": pin, "reset_token_expires_at": expires_at, "reset_attempts": 0
        }).eq('id', db_user['id']).execute()
        send_email(user_email, f"Your Dutipay verification code: {pin}",
            "Your verification code",
            [f"Hi {db_user.get('username', 'there')}, here is your new password reset code:",
             "Your code is valid for 10 minutes.", "If you didn't request this, ignore this email."],
            farm_name="Dutipay", details=[("Verification Code", pin)])
        return jsonify({"success": True, "message": "New code sent to your email"}), 200
    except Exception as e:
        print("RESEND PIN ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    token = request.args.get('token') or request.form.get('token')
    if not token:
        return render_template('forgot_password.html', error='Missing reset token.')
    try:
        user_res = supabase.table('users').select('*').eq('reset_token', token).execute()
        if not user_res.data:
            return render_template('forgot_password.html', error='Invalid or expired reset link.')
        db_user = user_res.data[0]
        expires_at = db_user.get('reset_token_expires_at')
        if not expires_at:
            return render_template('forgot_password.html', error='Invalid reset link.')
        try:
            expires_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            if datetime.now(expires_dt.tzinfo) > expires_dt:
                return render_template('forgot_password.html', error='This reset link has expired.')
        except Exception as e:
            print(f"Token expiry check error: {e}")

        if request.method == 'POST':
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            if not new_password or not confirm_password:
                return render_template('reset_password.html', token=token, error='Please fill in all fields.')
            if new_password != confirm_password:
                return render_template('reset_password.html', token=token, error='Passwords do not match.')
            if len(new_password) < 6:
                return render_template('reset_password.html', token=token, error='Min 6 characters.')

            hashed_password = hash_password(new_password)
            supabase.table('users').update({
                "password_hash": hashed_password, "reset_token": None,
                "reset_token_expires_at": None, "reset_attempts": 0, "reset_lockout_until": None
            }).eq('id', db_user['id']).execute()

            try:
                emp_code = db_user.get('employee_code') or db_user.get('username')
                emp_res = supabase.table('employees').select('email, first_name').eq('employee_code', emp_code).execute()
                worker_email = emp_res.data[0].get('email') if emp_res.data else None
                first_name = emp_res.data[0].get('first_name', 'there') if emp_res.data else 'there'
                farm_info = get_farm_mail_info(db_user.get('farm_id'))
                send_email(worker_email, "Your password was changed",
                    "Your password was changed",
                    [f"Hi {first_name}, your Dutipay password has been changed successfully.",
                     "If you did not make this change, contact your administrator immediately."],
                    farm_name=farm_info["name"], details=[("Username", db_user.get('username'))],
                    button=login_button())
            except Exception as mail_err:
                print(f"⚠️ Password change email error: {mail_err}")
                        # ✅ Audit log
            log_action('auth.password_reset_self', 'user', db_user['id'], {
                'username': db_user.get('username')
            })

            return redirect('/login?reset=success')
        return render_template('reset_password.html', token=token)
    except Exception as e:
        print("RESET PASSWORD ERROR:", str(e))
        return render_template('forgot_password.html', error=f'Error: {str(e)}')


@app.route('/api/change-password', methods=['POST'])
def change_password():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    try:
        data = request.get_json()
        current_password = data.get('current_password', '')
        new_password = data.get('new_password', '')
        user_id = session.get('user_id')
        if not current_password or not new_password:
            return jsonify({"success": False, "message": "All fields required"}), 400
        if len(new_password) < 6:
            return jsonify({"success": False, "message": "Min 6 characters"}), 400
        user_res = supabase.table('users').select('*').eq('id', user_id).execute()
        if not user_res.data:
            return jsonify({"success": False, "message": "User not found"}), 404
        db_user = user_res.data[0]
        stored_hash = db_user.get('password_hash') or ''
        is_valid, _ = verify_password(current_password, stored_hash)
        if not is_valid:
            return jsonify({"success": False, "message": "Current password incorrect"}), 401
        if current_password == new_password:
            return jsonify({"success": False, "message": "New password must differ"}), 400
        hashed_password = hash_password(new_password)
        supabase.table('users').update({"password_hash": hashed_password}).eq('id', user_id).execute()
        try:
            emp_code = session.get('employee_code')
            email, first_name = None, session.get('full_name', '')
            if emp_code:
                emp_res = supabase.table('employees').select('email, first_name').eq('employee_code', emp_code).execute()
                if emp_res.data:
                    email = emp_res.data[0].get('email')
                    first_name = emp_res.data[0].get('first_name') or first_name
            farm_info = get_farm_mail_info(session.get('farm_id'))
            send_email(email, "Your password was changed", "Your password was changed",
                [f"Hi {first_name}, your password was changed successfully.",
                 "If this was NOT you, contact your administrator immediately."],
                farm_name=farm_info["name"], details=[("Username", session.get('username', ''))],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ Change password email error: {mail_err}")
        return jsonify({"success": True, "message": "✅ Password updated!"}), 200
    except Exception as e:
        print("CHANGE PASSWORD ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/employees/<int:emp_id>/reset-password', methods=['POST'])
@role_required(['admin'])
def admin_reset_password(emp_id):
    """✅ FIX: Uses random password instead of hardcoded"""
    try:
        farm_id = session.get('farm_id')
        emp_res = supabase.table('employees').select('*').eq('id', emp_id).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Employee not found"}), 404
        emp = emp_res.data[0]
        if farm_id and farm_id != 0 and emp.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403

        new_password = generate_random_password(12)  # ✅ FIX
        hashed_password = hash_password(new_password)
        username = emp.get('employee_code', '').lower()

        user_res = supabase.table('users').update({"password_hash": hashed_password}) \
            .eq('username', username).execute()
        if not user_res.data:
            return jsonify({"success": False, "message": "User account not found"}), 404

        try:
            farm_info = get_farm_mail_info(farm_id)
            send_email(emp.get('email'), "Your password was reset",
                "Your password was reset",
                [f"Hi {emp.get('first_name', '')}, your account password was reset by your administrator.",
                 f"Your new temporary password: {new_password}",
                 "Please log in and change it immediately."],
                farm_name=farm_info["name"],
                details=[("Username", username), ("New Password", new_password)],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ Password reset email error: {mail_err}")
        log_action('employee.password_reset', 'employee', emp_id, {
            'target_username': username,
            'reset_by': session.get('username')
        })

        return jsonify({"success": True,
                        "message": f"Password reset!",
                        "username": username,
                        "new_password": new_password}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# ATTENDANCE
# ==========================================
@app.route('/attendance')
def attendance_page():
    if not is_logged_in():
        return redirect('/login')
    return render_template('attendance.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='attendance')


@app.route('/api/attendance/all', methods=['GET'])
def get_all_attendance():
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        farm_id = session.get('farm_id')
        role = session.get('role')
        
        # ✅ NEW: Pagination params
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (ValueError, TypeError):
            page = 1
        try:
            per_page = int(request.args.get('per_page', 20))
            per_page = max(5, min(per_page, 100))  # clamp 5-100
        except (ValueError, TypeError):
            per_page = 20

        if not date_from and not date_to:
            date_from = date_to = datetime.now().date().isoformat()

        # ✅ Build base query WITH COUNT
        query = supabase.table('attendance').select(
            '*, employees(first_name, last_name, employee_code)',
            count='exact'  # ✅ Get total count
        ).order('attendance_date', desc=True).order('time_in', desc=True)

        if date_from: query = query.gte('attendance_date', date_from)
        if date_to: query = query.lte('attendance_date', date_to)
        if farm_id and farm_id != 0: query = query.eq('farm_id', farm_id)

        # ✅ Staff restriction
        if role not in ('admin', 'manager', 'super_admin'):
            emp_code = session.get('employee_code')
            if emp_code:
                emp_res = supabase.table('employees').select('id').eq('employee_code', emp_code).execute()
                if emp_res.data:
                    query = query.eq('employee_id', emp_res.data[0]['id'])
                else:
                    return jsonify({
                        "success": True, "data": [],
                        "pagination": {"page": 1, "per_page": per_page, "total": 0,
                                       "total_pages": 0, "has_next": False, "has_prev": False}
                    }), 200
            else:
                return jsonify({
                    "success": True, "data": [],
                    "pagination": {"page": 1, "per_page": per_page, "total": 0,
                                   "total_pages": 0, "has_next": False, "has_prev": False}
                }), 200

        # ✅ Apply pagination (Supabase uses .range() — inclusive)
        offset_start = (page - 1) * per_page
        offset_end = offset_start + per_page - 1
        query = query.range(offset_start, offset_end)

        res = query.execute()
        total = res.count if res.count is not None else len(res.data or [])
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        records = []
        for r in res.data:
            emp = r.get('employees', {})
            records.append({
                'id': r['id'],
                'employee_name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
                'employee_code': emp.get('employee_code', ''),
                'attendance_date': r.get('attendance_date'),
                'time_in': r.get('time_in'),
                'time_out': r.get('time_out'),
                'late_minutes': r.get('late_minutes', 0),
                'status': r.get('status', 'Present')
            })

        return jsonify({
            "success": True,
            "data": records,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        }), 200
    except Exception as e:
        print("ATTENDANCE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500



@app.route('/api/attendance/photos', methods=['GET'])
@role_required(['admin', 'manager'])
def get_attendance_photos():
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        farm_id = session.get('farm_id')
        
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (ValueError, TypeError):
            page = 1
        try:
            per_page = int(request.args.get('per_page', 12))
            per_page = max(5, min(per_page, 100))
        except (ValueError, TypeError):
            per_page = 12

        if not date_from and not date_to:
            date_from = date_to = datetime.now().date().isoformat()

        query = supabase.table('attendance').select(
            '*, employees(first_name, last_name, employee_code)',
            count='exact'
        ).order('attendance_date', desc=True).order('time_in', desc=True)

        if date_from: query = query.gte('attendance_date', date_from)
        if date_to: query = query.lte('attendance_date', date_to)
        if farm_id and farm_id != 0: query = query.eq('farm_id', farm_id)

        offset_start = (page - 1) * per_page
        offset_end = offset_start + per_page - 1
        query = query.range(offset_start, offset_end)

        res = query.execute()
        total = res.count if res.count is not None else len(res.data or [])
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        records = []
        for r in res.data:
            emp = r.get('employees', {})
            records.append({
                'id': r['id'],
                'employee_name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
                'employee_code': emp.get('employee_code', ''),
                'attendance_date': r.get('attendance_date'),
                'time_in': r.get('time_in'),
                'time_out': r.get('time_out'),
                'photo_url': r.get('photo_url'),
                'photo_url_out': r.get('photo_url_out'),
                'device_info': r.get('device_info'),
                'late_minutes': r.get('late_minutes', 0),
                'status': r.get('status', 'Present')
            })

        return jsonify({
            "success": True,
            "data": records,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        }), 200
    except Exception as e:
        print("PHOTOS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
# ==========================================
# ✅ NEW: MANUAL ATTENDANCE ENTRY (admin only)
# ==========================================
@app.route('/api/attendance/manual', methods=['POST'])
@role_required(['admin'])
def manual_attendance_entry():
    """
    Admin manually adds an attendance record for an employee.
    Use cases: forgotten scan-in/out, offline days, corrections.
    Requires reason (audit logged).
    """
    try:
        data = request.json or {}
        farm_id = session.get('farm_id')
        
        employee_id = data.get('employee_id')
        attendance_date = (data.get('attendance_date') or '').strip()
        time_in = (data.get('time_in') or '').strip()
        time_out = (data.get('time_out') or '').strip()
        reason = (data.get('reason') or '').strip()

        # Validation
        if not all([employee_id, attendance_date, time_in]):
            return jsonify({"success": False, "message": "Employee, date, and time-in are required."}), 400
        if len(reason) < 5:
            return jsonify({"success": False, "message": "Reason must be at least 5 characters."}), 400

        # Verify employee belongs to this farm
        emp_res = supabase.table('employees').select('*') \
            .eq('id', employee_id).eq('farm_id', farm_id).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Employee not found."}), 404
        emp = emp_res.data[0]

        # Check kung may existing attendance record
        existing = supabase.table('attendance').select('id, time_in, time_out, late_minutes, status') \
            .eq('employee_id', employee_id) \
            .eq('attendance_date', attendance_date) \
            .execute()
        if existing.data:
            return jsonify({
                "success": False,
                "message": f"Attendance already exists for {emp['first_name']} on {attendance_date}. Use Edit instead."
            }), 409

        # Normalize times — expected format HH:MM or HH:MM:SS
        def normalize_time(t):
            if not t: return None
            parts = t.split(':')
            if len(parts) == 2:
                return f"{parts[0]}:{parts[1]}:00"
            return t

        time_in_norm = normalize_time(time_in)
        time_out_norm = normalize_time(time_out) if time_out else None

        # Compute late_minutes based on schedule
        from datetime import datetime as dt
        try:
            sched_in_str = str(emp.get('schedule_in') or '08:00:00')
            if len(sched_in_str.split(':')) == 2:
                sched_in_str += ':00'
            sched_in = dt.strptime(sched_in_str, '%H:%M:%S').time()
            
            att_date = dt.strptime(attendance_date, '%Y-%m-%d').date()
            time_in_dt = dt.combine(att_date, dt.strptime(time_in_norm, '%H:%M:%S').time())
            sched_dt = dt.combine(att_date, sched_in)
            diff = (time_in_dt - sched_dt).total_seconds() / 60
            late_minutes = max(0, int(diff))
        except Exception as e:
            print(f"⚠️ Late compute error: {e}")
            late_minutes = 0

        status = "Late" if late_minutes > 0 else "On-Time"

        # Build ISO timestamps
        try:
            time_in_iso = dt.combine(att_date, dt.strptime(time_in_norm, '%H:%M:%S').time()).isoformat()
            time_out_iso = dt.combine(att_date, dt.strptime(time_out_norm, '%H:%M:%S').time()).isoformat() if time_out_norm else None
        except Exception as e:
            return jsonify({"success": False, "message": f"Invalid time format: {e}"}), 400

        # Insert
        insert_data = {
            "employee_id": employee_id,
            "attendance_date": attendance_date,
            "time_in": time_in_iso,
            "time_out": time_out_iso,
            "late_minutes": late_minutes,
            "status": status,
            "farm_id": farm_id,
            "device_info": f"MANUAL ENTRY by {session.get('username', 'admin')}"
        }

        res = supabase.table('attendance').insert(insert_data).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to save attendance."}), 500

        # ✅ Audit log
        log_action('attendance.manual_entry', 'attendance', res.data[0]['id'], {
            'employee_code': emp.get('employee_code'),
            'employee_name': f"{emp['first_name']} {emp['last_name']}",
            'date': attendance_date,
            'time_in': time_in_norm,
            'time_out': time_out_norm,
            'late_minutes': late_minutes,
            'reason': reason
        })

        return jsonify({
            "success": True,
            "message": f"✅ Manual attendance added for {emp['first_name']} {emp['last_name']}.",
            "data": res.data[0]
        }), 201

    except Exception as e:
        print("MANUAL ATTENDANCE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/attendance/<int:attendance_id>', methods=['PUT'])
@role_required(['admin'])
def update_attendance_record(attendance_id):
    """Admin corrects an existing attendance record. Requires reason."""
    try:
        data = request.json or {}
        farm_id = session.get('farm_id')
        
        time_in = (data.get('time_in') or '').strip()
        time_out = (data.get('time_out') or '').strip()
        reason = (data.get('reason') or '').strip()

        if not time_in:
            return jsonify({"success": False, "message": "Time-in is required."}), 400
        if len(reason) < 5:
            return jsonify({"success": False, "message": "Reason must be at least 5 characters."}), 400

        # Fetch existing
        q = supabase.table('attendance').select('*, employees(first_name, last_name, employee_code, schedule_in)') \
            .eq('id', attendance_id)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()
        
        if not res.data:
            return jsonify({"success": False, "message": "Attendance record not found."}), 404

        old = res.data[0]
        emp = old.get('employees', {})
        attendance_date = old.get('attendance_date')

        # Normalize
        def norm(t):
            if not t: return None
            parts = t.split(':')
            if len(parts) == 2: return f"{parts[0]}:{parts[1]}:00"
            return t

        time_in_norm = norm(time_in)
        time_out_norm = norm(time_out) if time_out else None

        # Compute late
        from datetime import datetime as dt
        try:
            sched_str = str(emp.get('schedule_in') or '08:00:00')
            if len(sched_str.split(':')) == 2:
                sched_str += ':00'
            sched_in = dt.strptime(sched_str, '%H:%M:%S').time()
            att_date = dt.strptime(attendance_date, '%Y-%m-%d').date()
            time_in_dt = dt.combine(att_date, dt.strptime(time_in_norm, '%H:%M:%S').time())
            sched_dt = dt.combine(att_date, sched_in)
            diff = (time_in_dt - sched_dt).total_seconds() / 60
            late_minutes = max(0, int(diff))
        except Exception as e:
            print(f"⚠️ Late compute error: {e}")
            late_minutes = 0

        status = "Late" if late_minutes > 0 else "On-Time"

        # Build ISO
        try:
            att_date_obj = dt.strptime(attendance_date, '%Y-%m-%d').date()
            time_in_iso = dt.combine(att_date_obj, dt.strptime(time_in_norm, '%H:%M:%S').time()).isoformat()
            time_out_iso = dt.combine(att_date_obj, dt.strptime(time_out_norm, '%H:%M:%S').time()).isoformat() if time_out_norm else None
        except Exception as e:
            return jsonify({"success": False, "message": f"Invalid time: {e}"}), 400

        # Update
        supabase.table('attendance').update({
            "time_in": time_in_iso,
            "time_out": time_out_iso,
            "late_minutes": late_minutes,
            "status": status,
            "device_info": f"EDITED by {session.get('username', 'admin')}"
        }).eq('id', attendance_id).execute()

        # ✅ Audit log
        log_action('attendance.edited', 'attendance', attendance_id, {
            'employee_code': emp.get('employee_code'),
            'employee_name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
            'date': attendance_date,
            'old_time_in': old.get('time_in'),
            'old_time_out': old.get('time_out'),
            'old_late_minutes': old.get('late_minutes', 0),
            'new_time_in': time_in_iso,
            'new_time_out': time_out_iso,
            'new_late_minutes': late_minutes,
            'reason': reason
        })

        return jsonify({
            "success": True,
            "message": f"✅ Attendance updated for {emp.get('first_name', '')} {emp.get('last_name', '')}."
        }), 200

    except Exception as e:
        print("UPDATE ATTENDANCE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/attendance/<int:attendance_id>', methods=['DELETE'])
@role_required(['admin'])
def delete_attendance_record(attendance_id):
    """Admin deletes an attendance record. Requires reason."""
    try:
        data = request.json or {}
        farm_id = session.get('farm_id')
        reason = (data.get('reason') or '').strip()

        if len(reason) < 5:
            return jsonify({"success": False, "message": "Reason must be at least 5 characters."}), 400

        q = supabase.table('attendance').select('*, employees(first_name, last_name, employee_code)') \
            .eq('id', attendance_id)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()
        
        if not res.data:
            return jsonify({"success": False, "message": "Attendance record not found."}), 404

        old = res.data[0]
        emp = old.get('employees', {})

        supabase.table('attendance').delete().eq('id', attendance_id).execute()

        # ✅ Audit log
        log_action('attendance.deleted', 'attendance', attendance_id, {
            'employee_code': emp.get('employee_code'),
            'employee_name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
            'date': old.get('attendance_date'),
            'time_in': old.get('time_in'),
            'time_out': old.get('time_out'),
            'reason': reason
        })

        return jsonify({"success": True, "message": "✅ Attendance record deleted."}), 200

    except Exception as e:
        print("DELETE ATTENDANCE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/attendance/timein', methods=['POST'])
def time_in():
    try:
        data = request.json
        qr_code_data = data.get('qr_code_data')
        photo_data = data.get('photo')
        device_info = data.get('device_info', '')
        if not qr_code_data:
            return jsonify({"success": False, "message": "Missing QR code data"}), 400

        employee_code = parse_qr_code(qr_code_data)
        emp_res = supabase.table('employees').select('id, first_name, last_name, schedule_in, farm_id') \
            .eq('employee_code', employee_code).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": f"Employee not found! ({employee_code})"}), 404

        employee = emp_res.data[0]
        emp_id = employee['id']
        employee_name = f"{employee['first_name']} {employee['last_name']}"
        employee_farm_id = employee.get('farm_id')
        today = datetime.now().date()

        att_res = supabase.table('attendance').select('id').eq('employee_id', emp_id) \
            .eq('attendance_date', str(today)).execute()
        if att_res.data:
            return jsonify({"success": False, "message": "Already timed in today"}), 400

        farm_settings = {}
        try:
            fs_res = supabase.table('farm_settings').select('grace_period, require_photo') \
                .eq('farm_id', employee_farm_id).execute()
            if fs_res.data:
                farm_settings = fs_res.data[0]
        except Exception as fs_err:
            print(f"⚠️ Farm settings error: {fs_err}")

        grace_period = int(farm_settings.get('grace_period', 5))
        now = datetime.now()
        schedule_time = datetime.strptime(str(employee['schedule_in']), "%H:%M:%S").time()
        schedule_datetime = datetime.combine(today, schedule_time)
        diff_minutes = (now - schedule_datetime).total_seconds() / 60
        late_minutes = max(0, diff_minutes - grace_period)
        status = "Late" if late_minutes > 0 else "On-Time"

        photo_url = None
        if photo_data and photo_data.startswith('data:image'):
            try:
                header, encoded = photo_data.split(',', 1)
                file_bytes = base64.b64decode(encoded)
                timestamp = now.strftime("%Y%m%d_%H%M%S")
                unique_filename = f"attendance/{employee_farm_id}/{emp_id}_{today}_{timestamp}.jpg"
                supabase.storage.from_('attendance_photos').upload(unique_filename, file_bytes,
                    file_options={"content-type": "image/jpeg", "upsert": "true"})
                photo_url = supabase.storage.from_('attendance_photos').get_public_url(unique_filename)
            except Exception as photo_err:
                print(f"⚠️ Photo upload failed: {photo_err}")

        supabase.table('attendance').insert({
            "employee_id": emp_id, "attendance_date": str(today),
            "time_in": now.isoformat(), "late_minutes": int(late_minutes),
            "status": status, "farm_id": employee_farm_id,
            "photo_url": photo_url, "device_info": device_info
        }).execute()

        return jsonify({"success": True,
                        "message": f"Time-In recorded for {employee_name}",
                        "employee_name": employee_name,
                        "late_minutes": int(late_minutes),
                        "grace_period_used": grace_period}), 200
    except Exception as e:
        print("TIME-IN ERROR:", str(e))
        return jsonify({"success": False, "message": "Python Error: " + str(e)}), 500


@app.route('/api/attendance/timeout', methods=['POST'])
def time_out():
    try:
        data = request.json
        qr_code_data = data.get('qr_code_data')
        photo_data = data.get('photo')
        device_info = data.get('device_info', '')
        if not qr_code_data:
            return jsonify({"success": False, "message": "Missing QR code data"}), 400

        employee_code = parse_qr_code(qr_code_data)
        emp_res = supabase.table('employees').select('id, first_name, last_name, schedule_out, farm_id') \
            .eq('employee_code', employee_code).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": f"Employee not found! ({employee_code})"}), 404

        employee = emp_res.data[0]
        emp_id = employee['id']
        employee_name = f"{employee['first_name']} {employee['last_name']}"
        employee_farm_id = employee.get('farm_id')
        today = datetime.now().date()

        att_res = supabase.table('attendance').select('id').eq('employee_id', emp_id) \
            .eq('attendance_date', str(today)).execute()
        if not att_res.data:
            return jsonify({"success": False, "message": "No time-in record found today"}), 400

        now = datetime.now()
        farm_settings = {}
        try:
            fs_res = supabase.table('farm_settings').select('overtime_enabled') \
                .eq('farm_id', employee_farm_id).execute()
            if fs_res.data:
                farm_settings = fs_res.data[0]
        except Exception as fs_err:
            print(f"⚠️ Farm settings error: {fs_err}")

        overtime_enabled = bool(farm_settings.get('overtime_enabled', False))
        overtime_minutes = 0
        if overtime_enabled:
            try:
                if employee.get('schedule_out'):
                    schedule_out_time = datetime.strptime(str(employee['schedule_out']), "%H:%M:%S").time()
                    schedule_out_datetime = datetime.combine(today, schedule_out_time)
                    ot_diff = (now - schedule_out_datetime).total_seconds() / 60
                    overtime_minutes = int(max(0, ot_diff))
            except Exception as ot_err:
                print(f"⚠️ Overtime compute error: {ot_err}")

        photo_url_out = None
        if photo_data and photo_data.startswith('data:image'):
            try:
                header, encoded = photo_data.split(',', 1)
                file_bytes = base64.b64decode(encoded)
                timestamp = now.strftime("%Y%m%d_%H%M%S")
                unique_filename = f"attendance/{employee_farm_id}/{emp_id}_{today}_out_{timestamp}.jpg"
                supabase.storage.from_('attendance_photos').upload(unique_filename, file_bytes,
                    file_options={"content-type": "image/jpeg", "upsert": "true"})
                photo_url_out = supabase.storage.from_('attendance_photos').get_public_url(unique_filename)
            except Exception as photo_err:
                print(f"⚠️ Photo (out) upload failed: {photo_err}")

        supabase.table('attendance').update({
            "time_out": now.isoformat(),
            "overtime_minutes": overtime_minutes,
            "photo_url_out": photo_url_out,
            "device_info": device_info
        }).eq('id', att_res.data[0]['id']).execute()

        return jsonify({"success": True,
                        "message": f"Time-Out recorded for {employee_name}",
                        "employee_name": employee_name,
                        "overtime_minutes": overtime_minutes,
                        "overtime_applied": overtime_enabled}), 200
    except Exception as e:
        print("TIME-OUT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# AUTO-TIMEOUT JOB
# ==========================================
def auto_timeout_job():
    print("⏰ [AUTO-TIMEOUT] Checking for unclosed attendance records...")
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    try:
        q = supabase.table('attendance').select('id, employee_id, farm_id, attendance_date, time_in') \
            .is_('time_out', 'null').lte('attendance_date', yesterday.isoformat()).execute()
        records = q.data or []
        if not records:
            print("⏰ [AUTO-TIMEOUT] No unclosed records.")
            return

        farm_ids = list({r['farm_id'] for r in records if r.get('farm_id')})
        farm_settings_map = {}
        for fid in farm_ids:
            try:
                fs = supabase.table('farm_settings').select('end_of_day').eq('farm_id', fid).execute()
                if fs.data:
                    farm_settings_map[fid] = fs.data[0]
            except Exception as e:
                print(f"⚠️ Settings load error farm {fid}: {e}")

        closed_count = 0
        for rec in records:
            try:
                rec_date = datetime.fromisoformat(rec['attendance_date']).date()
                farm_id = rec.get('farm_id')
                end_of_day_str = farm_settings_map.get(farm_id, {}).get('end_of_day', '23:59')
                try:
                    end_time = datetime.strptime(end_of_day_str, "%H:%M").time() if len(end_of_day_str.split(':')) == 2 \
                        else datetime.strptime(end_of_day_str, "%H:%M:%S").time()
                except Exception:
                    end_time = datetime.strptime('23:59', "%H:%M").time()
                auto_timeout_dt = datetime.combine(rec_date, end_time)
                supabase.table('attendance').update({
                    "time_out": auto_timeout_dt.isoformat(),
                    "device_info": "AUTO-CLOSED (no manual time-out)"
                }).eq('id', rec['id']).execute()
                closed_count += 1
            except Exception as inner:
                print(f"⚠️ Auto-timeout record #{rec.get('id')} error: {inner}")
        print(f"⏰ [AUTO-TIMEOUT] Done. Closed {closed_count} record(s).")
    except Exception as e:
        print(f"⚠️ [AUTO-TIMEOUT] Job error: {e}")


# ==========================================
# PAYROLL HELPERS
# ==========================================
def _csv_safe(value):
    """
    Escape values that could be interpreted as Excel formulas.
    Prepend a single quote if value starts with =, +, -, @, tab, or CR.
    """
    if value is None:
        return ''
    s = str(value)
    if s and s[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return s

def get_current_cutoff(reference_date=None):
    if reference_date is None:
        reference_date = datetime.now().date()
    elif isinstance(reference_date, str):
        reference_date = datetime.fromisoformat(reference_date).date()
    y, m, d = reference_date.year, reference_date.month, reference_date.day
    if d <= 15:
        start = reference_date.replace(day=1)
        end = reference_date.replace(day=15)
    else:
        start = reference_date.replace(day=16)
        last_day = calendar.monthrange(y, m)[1]
        end = reference_date.replace(day=last_day)
    month_name = start.strftime('%B')
    label = f"{month_name} {start.day}–{end.day}, {y}"
    return {'start': start.isoformat(), 'end': end.isoformat(), 'label': label}


def get_cutoff_options(months_back=3):
    options = []
    today = datetime.now().date()
    options.append(get_current_cutoff(today))
    ref = today
    for i in range(months_back * 2):
        if ref.day <= 15:
            prev_month = ref.replace(day=1) - timedelta(days=1)
            ref = prev_month.replace(day=16)
        else:
            ref = ref.replace(day=1)
        cutoff = get_current_cutoff(ref)
        if cutoff not in options:
            options.append(cutoff)
    return options[:months_back * 2 + 1]


def calculate_working_days_in_period(period_start, period_end, rest_days='sat-sun'):
    start = datetime.fromisoformat(period_start).date()
    end = datetime.fromisoformat(period_end).date()
    if end < start:
        return 0
    excluded = set()
    if rest_days == 'sat-sun':
        excluded = {5, 6}
    elif rest_days == 'sun':
        excluded = {6}
    working_days = 0
    current = start
    while current <= end:
        if current.weekday() not in excluded:
            working_days += 1
        current += timedelta(days=1)
    return working_days


def get_cutoff_days_for_month(cutoff_day_config, year, month):
    last_day = calendar.monthrange(year, month)[1]
    parts = [int(p) for p in str(cutoff_day_config).split('-') if p.strip().isdigit()]
    if not parts:
        parts = [15, 30]
    return sorted(set(min(p, last_day) for p in parts))


def get_auto_generate_period_for_date(cutoff_day_config, target_date):
    year, month = target_date.year, target_date.month
    days_this_month = get_cutoff_days_for_month(cutoff_day_config, year, month)
    if target_date.day not in days_this_month:
        return None
    idx = days_this_month.index(target_date.day)
    period_end = target_date
    if idx == 0:
        prev_month_last_date = target_date.replace(day=1) - timedelta(days=1)
        prev_days = get_cutoff_days_for_month(cutoff_day_config, prev_month_last_date.year, prev_month_last_date.month)
        period_start = prev_month_last_date.replace(day=prev_days[-1]) + timedelta(days=1)
    else:
        period_start = target_date.replace(day=days_this_month[idx - 1]) + timedelta(days=1)
    return (period_start.isoformat(), period_end.isoformat())


def auto_generate_payroll_job():
    today = datetime.now().date()
    print(f"⏰ [AUTO-GENERATE] Checking farms for cutoffs ({today})...")
    try:
        farms_res = supabase.table('farm_settings').select('*').eq('auto_generate', True).execute()
        for settings in (farms_res.data or []):
            farm_id = settings.get('farm_id')
            cutoff_config = settings.get('cutoff_day', '15-30')
            period = get_auto_generate_period_for_date(cutoff_config, today)
            if not period:
                continue
            period_start, period_end = period
            try:
                result = _generate_payroll_for_farm(farm_id, period_start, period_end)
                print(f"⏰ [AUTO-GENERATE] Farm {farm_id}: {result.get('message')}")
            except Exception as gen_err:
                print(f"⚠️ [AUTO-GENERATE] Farm {farm_id} failed: {gen_err}")
    except Exception as e:
        print("AUTO-GENERATE PAYROLL ERROR:", str(e))


def build_absentee_digest(farm_id):
    today = datetime.now().date()
    ABSENT_THRESHOLD_DAYS = 14
    emp_query = supabase.table('employees').select('*').eq('is_active', True)
    if farm_id and farm_id != 0:
        emp_query = emp_query.eq('farm_id', farm_id)
    emp_res = emp_query.execute()
    employees = [e for e in (emp_res.data or [])
                 if e.get('position') not in ('Farm Owner', 'Owner', 'Business Owner')]

    flagged_14d, never_attended, already_flagged = [], [], []

    for emp in employees:
        att_res = supabase.table('attendance').select('attendance_date') \
            .eq('employee_id', emp['id']).order('attendance_date', desc=True).limit(1).execute()
        if att_res.data:
            last_str = att_res.data[0]['attendance_date']
            try:
                last_date = datetime.strptime(last_str, '%Y-%m-%d').date()
            except Exception:
                continue
            days_since = (today - last_date).days
            if days_since >= ABSENT_THRESHOLD_DAYS:
                entry = {'code': emp.get('employee_code', ''),
                         'name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
                         'position': emp.get('position', ''),
                         'last_date': last_str, 'days_since': days_since}
                if days_since >= 30:
                    already_flagged.append(entry)
                else:
                    flagged_14d.append(entry)
        else:
            never_attended.append({
                'code': emp.get('employee_code', ''),
                'name': f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip(),
                'position': emp.get('position', '')
            })
    return {'flagged_14d': flagged_14d, 'never_attended': never_attended,
            'already_flagged': already_flagged}


def weekly_absentee_report_job():
    print("📧 [WEEKLY REPORT] Building absentee digests...")
    try:
        farms_res = supabase.table('farms').select('id, display_name, farm_name, owner_email').execute()
        for farm in (farms_res.data or []):
            farm_id = farm.get('id')
            owner_email = farm.get('owner_email')
            farm_name = farm.get('display_name') or farm.get('farm_name') or 'Dutipay'
            if not owner_email:
                continue
            try:
                digest = build_absentee_digest(farm_id)
                total = len(digest['flagged_14d']) + len(digest['never_attended']) + len(digest['already_flagged'])
                if total == 0:
                    continue
                details = []
                if digest['already_flagged']:
                    details.append(("⚠️ Already 30+ days absent", f"{len(digest['already_flagged'])} employee(s)"))
                if digest['flagged_14d']:
                    details.append(("🟡 14–29 days absent", f"{len(digest['flagged_14d'])} employee(s)"))
                if digest['never_attended']:
                    details.append(("🔴 Never attended", f"{len(digest['never_attended'])} employee(s)"))
                lines = [f"Here is your weekly attendance digest for {farm_name}.", f"Total concerns: {total}."]
                send_email(owner_email, f"Weekly attendance digest — {total} concern(s)",
                    "Weekly Attendance Digest", lines,
                    farm_name=farm_name, details=details, button=login_button())
            except Exception as inner:
                print(f"⚠️ [WEEKLY REPORT] Farm {farm_id} failed: {inner}")
    except Exception as e:
        print(f"WEEKLY REPORT ERROR: {e}")


# ==========================================
# PAYROLL
# ==========================================
@app.route('/payroll')
def payroll_page():
    if not is_logged_in():
        return redirect('/login')
    role = session.get('role', 'employee')
    full_name = session.get('full_name', 'User')
    if role == 'admin':
        return render_template('payroll_admin.html', role=role, full_name=full_name, active_page='payroll')
    elif role == 'manager':
        return render_template('payroll_manager.html', role=role, full_name=full_name, active_page='payroll')
    return render_template('payroll_staff.html', role=role, full_name=full_name, active_page='payroll')


@app.route('/payroll/manager')
def payroll_manager():
    if not is_logged_in():
        return redirect('/login')
    return render_template('payroll_manager.html', role=session.get('role', 'manager'),
                           full_name=session.get('full_name', 'manager User'), active_page='payroll')


@app.route('/payroll/staff')
def payroll_staff():
    if not is_logged_in():
        return redirect('/login')
    return render_template('payroll_staff.html', role=session.get('role', 'Staff'),
                           full_name=session.get('full_name', 'Staff User'), active_page='payroll')


def _generate_payroll_for_farm(farm_id, period_start, period_end):
    settings_res = supabase.table('farm_settings').select('*').eq('farm_id', farm_id).execute()
    settings = settings_res.data[0] if settings_res.data else {}

    late_enabled = settings.get('late_deduction_enabled', False)
    late_rate = float(settings.get('late_deduction_rate', 0))
    absent_enabled = settings.get('absent_deduction_enabled', False)
    absent_rate = float(settings.get('absent_deduction_rate', 0))
    custom_enabled = settings.get('custom_deduction_enabled', False)
    custom_label = settings.get('custom_deduction_label', '') or ''
    custom_amount = float(settings.get('custom_deduction_amount', 0))
    bonus_enabled = settings.get('bonus_enabled', False)
    bonus_amount = float(settings.get('bonus_amount', 0))
    allowance_enabled = settings.get('allowance_enabled', False)
    allowance_amount = float(settings.get('allowance_amount', 0))
    overtime_enabled = settings.get('overtime_enabled', False)
    overtime_rate = float(settings.get('overtime_rate', 1.25))
    working_days = int(settings.get('payroll_working_days', 22))
    rest_days = settings.get('rest_days', 'sat-sun')
    include_owner = bool(settings.get('include_owner', False))

    working_days_in_period = calculate_working_days_in_period(period_start, period_end, rest_days)

    emp_query = supabase.table('employees').select('id, employee_code, first_name, last_name, basic_salary, position, schedule_in, schedule_out').eq('is_active', True)
    if farm_id and farm_id != 0:
        emp_query = emp_query.eq('farm_id', farm_id)
    emp_res = emp_query.execute()
    if not emp_res.data:
        return {"success": False, "message": "No active employees", "http_status": 404}

    employees = emp_res.data if include_owner else [emp for emp in emp_res.data if emp.get('position') != 'Farm Owner']

    generated_count = 0
    results = []
    for emp in employees:
        emp_id = emp['id']
        att_res = supabase.table('attendance').select('*').eq('employee_id', emp_id) \
            .gte('attendance_date', period_start).lte('attendance_date', period_end).execute()
        attendance = att_res.data or []
        days_worked = len([a for a in attendance if a.get('time_in')])
        total_late_minutes = sum([a.get('late_minutes', 0) for a in attendance])
        total_overtime_minutes = sum([a.get('overtime_minutes', 0) or 0 for a in attendance])

        daily_rate = float(emp['basic_salary']) / working_days if emp['basic_salary'] else 0
        basic_pay = daily_rate * days_worked

        overtime_pay = 0
        if overtime_enabled and total_overtime_minutes > 0:
            try:
                sched_in = datetime.strptime(str(emp.get('schedule_in') or '08:00:00'), "%H:%M:%S")
                sched_out = datetime.strptime(str(emp.get('schedule_out') or '17:00:00'), "%H:%M:%S")
                shift_hours = (sched_out - sched_in).total_seconds() / 3600
                if shift_hours <= 0:
                    shift_hours = 8
            except Exception:
                shift_hours = 8
            hourly_rate = daily_rate / shift_hours if shift_hours else 0
            overtime_pay = (total_overtime_minutes / 60) * hourly_rate * overtime_rate

        late_deduction = total_late_minutes * late_rate if (late_enabled and late_rate > 0) else 0
        absent_deduction = 0
        if absent_enabled and absent_rate > 0:
            absent_days = max(0, working_days_in_period - days_worked)
            absent_deduction = absent_days * absent_rate

        custom_deduction = custom_amount if (custom_enabled and custom_amount > 0) else 0
        bonus = bonus_amount if bonus_enabled else 0
        allowance = allowance_amount if allowance_enabled else 0

        gross_pay = basic_pay + bonus + allowance + overtime_pay
        total_deductions = late_deduction + absent_deduction + custom_deduction
        net_pay = max(0, gross_pay - total_deductions)

        existing = supabase.table('payroll').select('id').eq('employee_id', emp_id) \
            .eq('pay_period_start', period_start).eq('pay_period_end', period_end).execute()
        if existing.data:
            continue

        payroll_data = {
            "employee_id": emp_id, "pay_period_start": period_start, "pay_period_end": period_end,
            "total_days_worked": days_worked, "total_late_minutes": total_late_minutes,
            "basic_pay": round(basic_pay, 2), "late_deduction": round(late_deduction, 2),
            "gross_pay": round(gross_pay, 2), "total_deductions": round(total_deductions, 2),
            "net_pay": round(net_pay, 2), "received_at": None, "received_by": None, "farm_id": farm_id,
            "bonus": round(bonus, 2), "allowance": round(allowance, 2),
            "absent_deduction": round(absent_deduction, 2),
            "custom_deduction_label": custom_label, "custom_deduction": round(custom_deduction, 2),
            "total_overtime_minutes": total_overtime_minutes, "overtime_pay": round(overtime_pay, 2)
        }
        insert_res = supabase.table('payroll').insert(payroll_data).execute()
        if insert_res.data:
            generated_count += 1
            results.append({"employee": f"{emp['first_name']} {emp['last_name']}",
                            "days_worked": days_worked, "gross_pay": round(gross_pay, 2),
                            "deductions": round(total_deductions, 2), "net_pay": round(net_pay, 2)})

    return {"success": True, "message": f"Payroll generated for {generated_count} employees",
            "count": generated_count, "data": results, "http_status": 200}


@app.route('/api/payroll/generate', methods=['POST'])
def generate_payroll():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    if session.get('role') != 'admin':
        return jsonify({"success": False, "message": "Only an admin can generate payroll"}), 403
    try:
        data = request.json or {}
        period_start = data.get('period_start')
        period_end = data.get('period_end')
        regenerate = bool(data.get('regenerate', False))
        farm_id = session.get('farm_id')
        if not period_start or not period_end:
            return jsonify({"success": False, "message": "Missing dates"}), 400
        if period_end < period_start:
            return jsonify({"success": False, "message": "End date must not be earlier than start date"}), 400

        if regenerate:
            # ✅ Check muna kung may received records sa period
            check_received = supabase.table('payroll') \
                .select('id', count='exact') \
                .eq('pay_period_start', period_start) \
                .eq('pay_period_end', period_end) \
                .not_.is_('received_at', 'null')
            if farm_id and farm_id != 0:
                check_received = check_received.eq('farm_id', farm_id)
            check_res = check_received.execute()

            if check_res.count and check_res.count > 0:
                return jsonify({
                    "success": False,
                    "message": f"Cannot regenerate — {check_res.count} record(s) are already marked as received. "
                               f"Use 'Unlock' first to allow changes.",
                    "received_count": check_res.count
                }), 409  # 409 Conflict

            # Safe to delete unpaid records
            del_q = supabase.table('payroll').delete() \
                .eq('pay_period_start', period_start).eq('pay_period_end', period_end) \
                .is_('received_at', 'null')
            if farm_id and farm_id != 0:
                del_q = del_q.eq('farm_id', farm_id)
            del_q.execute()

        result = _generate_payroll_for_farm(farm_id, period_start, period_end)
        http_status = result.pop('http_status', 200)

        # ✅ Audit log
        if result.get('success'):
            log_action('payroll.generated', 'payroll_period', None, {
                'period_start': period_start,
                'period_end': period_end,
                'count': result.get('count', 0),
                'regenerate': regenerate
            })

        return jsonify(result), http_status
    except Exception as e:
        print("PAYROLL ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# PAYROLL — LIST / VIEW / MARK PAID / EXPORT
# ==========================================
def _current_employee_id(farm_id):
    code = session.get('employee_code')
    if not code:
        return None
    q = supabase.table('employees').select('id').eq('employee_code', code)
    if farm_id and farm_id != 0:
        q = q.eq('farm_id', farm_id)
    res = q.execute()
    return res.data[0]['id'] if res.data else None


def _attach_employee_info(rows):
    ids = list({r['employee_id'] for r in rows if r.get('employee_id') is not None})
    emp_map = {}
    if ids:
        emp_res = supabase.table('employees').select('id, first_name, last_name, employee_code, position') \
            .in_('id', ids).execute()
        emp_map = {e['id']: e for e in (emp_res.data or [])}
    for r in rows:
        e = emp_map.get(r.get('employee_id'), {})
        r['employee_name'] = f"{e.get('first_name', '')} {e.get('last_name', '')}".strip() or 'Unknown'
        r['employee_code'] = e.get('employee_code', '')
        r['position'] = e.get('position', '')
    return rows

def get_farm_settings(farm_id):
    """Fetch farm settings for permission checks. Returns dict with defaults."""
    defaults = {
        'manager_can_view_salary': False,
        'manager_can_view_payslip': False,
        'manager_can_print_payslip': False,
    }
    if not farm_id:
        return defaults
    try:
        res = supabase.table('farm_settings').select(
            'manager_can_view_salary, manager_can_view_payslip, manager_can_print_payslip'
        ).eq('farm_id', farm_id).execute()

        if res.data:
            row = res.data[0]
            for key in defaults:
                if row.get(key) is not None:
                    defaults[key] = bool(row.get(key))
    except Exception as e:
        print(f"⚠️ get_farm_settings error: {e}")
    return defaults

def check_manager_permission(role, settings, permission_key):
    """
    Check if the current role has permission for an action.
    Admins always pass. Managers checked against settings. Others denied.
    """
    if role == 'admin':
        return True
    if role == 'manager':
        return bool(settings.get(permission_key, False))
    return False
def _payroll_summary(rows):
    """✅ FIX: Include total_net + paid_count for manager dashboard."""
    total_net = sum(float(r.get('net_pay') or 0) for r in rows)
    received_count = len([r for r in rows if r.get('received_at')])
    return {
        "count": len(rows),
        "received_count": received_count,
        "pending_count": len(rows) - received_count,
        "paid_count": received_count,  # alias for manager dashboard
        "total_net": round(total_net, 2)
    }

@app.route('/api/payroll', methods=['GET'])
def list_payroll():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')
        period_start = request.args.get('period_start')
        period_end = request.args.get('period_end')

        # ✅ Load permission settings
        settings = get_farm_settings(farm_id)
        can_view_salary = check_manager_permission(role, settings, 'manager_can_view_salary')

        def base_query():
            q = supabase.table('payroll').select('*')
            if farm_id and farm_id != 0:
                q = q.eq('farm_id', farm_id)
            return q

        period = None
        if role in ('admin', 'manager'):
            if not (period_start and period_end):
                latest = base_query().order('pay_period_start', desc=True).limit(1).execute()
                if not latest.data:
                    return jsonify({
                        "success": True,
                        "data": [],
                        "summary": _payroll_summary([]),
                        "period": None,
                        "permissions": {
                            "can_view_salary": can_view_salary,
                            "can_view_payslip": check_manager_permission(role, settings, 'manager_can_view_payslip'),
                            "can_print_payslip": check_manager_permission(role, settings, 'manager_can_print_payslip')
                        }
                    }), 200
                period_start = latest.data[0]['pay_period_start']
                period_end = latest.data[0]['pay_period_end']
            rows = base_query().eq('pay_period_start', period_start).eq('pay_period_end', period_end).execute().data or []
            period = {"start": period_start, "end": period_end}
        else:
            emp_id = _current_employee_id(farm_id)
            if not emp_id:
                return jsonify({
                    "success": True,
                    "data": [],
                    "summary": _payroll_summary([]),
                    "period": None,
                    "permissions": {
                        "can_view_salary": can_view_salary,
                        "can_view_payslip": check_manager_permission(role, settings, 'manager_can_view_payslip'),
                        "can_print_payslip": check_manager_permission(role, settings, 'manager_can_print_payslip')
                    }
                }), 200
            rows = base_query().eq('employee_id', emp_id).order('pay_period_start', desc=True).execute().data or []

        rows = _attach_employee_info(rows)
        if role in ('admin', 'manager'):
            rows.sort(key=lambda r: r['employee_name'].lower())

        # ✅ Redact salary fields kung walang permission
        if not can_view_salary:
            for r in rows:
                r['basic_pay'] = None
                r['overtime_pay'] = None
                r['bonus'] = None
                r['allowance'] = None
                r['gross_pay'] = None
                r['late_deduction'] = None
                r['absent_deduction'] = None
                r['custom_deduction'] = None
                r['total_deductions'] = None
                r['net_pay'] = None
                r['salary_hidden'] = True

        # ✅ Remove total_net for managers (privacy)
        summary = _payroll_summary(rows)
        if role == 'manager':
            # ✅ Managers never see total net pay
            summary['total_net'] = None
            summary['total_hidden'] = True

        return jsonify({
            "success": True,
            "data": rows,
            "summary": summary,
            "period": period,
            "permissions": {
                "can_view_salary": can_view_salary,
                "can_view_payslip": check_manager_permission(role, settings, 'manager_can_view_payslip'),
                "can_print_payslip": check_manager_permission(role, settings, 'manager_can_print_payslip')
            }
        }), 200

    except Exception as e:
        print("LIST PAYROLL ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/payroll/<int:payroll_id>', methods=['GET'])
def get_payroll_detail(payroll_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')

        # ✅ NEW: Permission check
        settings = get_farm_settings(farm_id)
        can_view_salary = check_manager_permission(role, settings, 'manager_can_view_salary')

        q = supabase.table('payroll').select('*').eq('id', payroll_id)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()
        if not res.data:
            return jsonify({"success": False, "message": "Payroll record not found"}), 404
        record = res.data[0]

        # Staff restriction — own record lang
        if role not in ('admin', 'manager'):
            if record.get('employee_id') != _current_employee_id(farm_id):
                return jsonify({"success": False, "message": "Not allowed"}), 403

        # ✅ NEW: Manager without salary view permission — block
        if role == 'manager' and not can_view_salary:
            return jsonify({
                "success": False,
                "message": "You don't have permission to view salary details. Contact your administrator."
            }), 403

        record = _attach_employee_info([record])[0]
        return jsonify({"success": True, "data": record}), 200
    except Exception as e:
        print("PAYROLL DETAIL ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
@app.route('/api/payroll/<int:payroll_id>/unlock', methods=['POST'])
def unlock_payroll(payroll_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401

    role = session.get('role')
    farm_id = session.get('farm_id')

    # ✅ NEW: Permission check via settings
    settings = get_farm_settings(farm_id)
    if not check_manager_permission(role, settings, 'manager_can_unlock_payroll'):
        return jsonify({
            "success": False,
            "message": "You don't have permission to unlock payroll."
        }), 403

    try:
        data = request.json or {}
        reason = (data.get('reason') or '').strip()

        if not reason or len(reason) < 5:
            return jsonify({
                "success": False,
                "message": "Please provide a reason (at least 5 characters) for unlocking."
            }), 400

        q = supabase.table('payroll').select('id, received_at, employee_id, pay_period_start, pay_period_end') \
            .eq('id', payroll_id)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()

        if not res.data:
            return jsonify({"success": False, "message": "Payroll record not found"}), 404

        record = res.data[0]
        if not record.get('received_at'):
            return jsonify({"success": False, "message": "This payroll is not locked."}), 400

        supabase.table('payroll').update({
            "received_at": None,
            "received_by": None
        }).eq('id', payroll_id).execute()

        log_action('payroll.unlocked', 'payroll', payroll_id, {
            'reason': reason,
            'unlocked_by': session.get('username'),
            'role': role,
            'employee_id': record.get('employee_id'),
            'period_start': record.get('pay_period_start'),
            'period_end': record.get('pay_period_end'),
            'previous_received_at': record.get('received_at')
        })

        return jsonify({
            "success": True,
            "message": "Payroll unlocked. You can now regenerate or edit."
        }), 200

    except Exception as e:
        print("UNLOCK PAYROLL ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/payroll/<int:payroll_id>/mark-received', methods=['POST'])
def mark_payroll_received(payroll_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401

    role = session.get('role')
    farm_id = session.get('farm_id')

    # ✅ NEW: Permission check via settings
    settings = get_farm_settings(farm_id)
    if not check_manager_permission(role, settings, 'manager_can_mark_received'):
        return jsonify({
            "success": False,
            "message": "You don't have permission to mark payroll as received."
        }), 403

    try:
        q = supabase.table('payroll').select('id, received_at').eq('id', payroll_id)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()
        if not res.data:
            return jsonify({"success": False, "message": "Payroll record not found"}), 404
        if res.data[0].get('received_at'):
            return jsonify({"success": False, "message": "Already marked as received"}), 400

        supabase.table('payroll').update({
            "received_at": datetime.now().isoformat(),
            "received_by": session.get('user_id')
        }).eq('id', payroll_id).execute()

        # ✅ Audit log
        log_action('payroll.received', 'payroll', payroll_id, {
            'marked_by': session.get('username'),
            'role': role
        })

        return jsonify({"success": True, "message": "Marked as received"}), 200
    except Exception as e:
        print("MARK RECEIVED ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/payroll/export', methods=['GET'])
def export_payroll():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401

    role = session.get('role')
    farm_id = session.get('farm_id')

    # ✅ NEW: Permission check — export requires view salary permission
    settings = get_farm_settings(farm_id)
    if not check_manager_permission(role, settings, 'manager_can_view_salary'):
        return jsonify({
            "success": False,
            "message": "You don't have permission to export payroll data."
        }), 403

    try:
        import csv
        from io import StringIO
        from flask import Response

        period_start = request.args.get('period_start')
        period_end = request.args.get('period_end')
        if not period_start or not period_end:
            return jsonify({"success": False, "message": "Missing dates"}), 400

        q = supabase.table('payroll').select('*') \
            .eq('pay_period_start', period_start).eq('pay_period_end', period_end)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        rows = _attach_employee_info(q.execute().data or [])
        rows.sort(key=lambda r: r['employee_name'].lower())

        out = StringIO()
        writer = csv.writer(out)
        writer.writerow(['Employee Code', 'Employee', 'Position', 'Period Start', 'Period End',
                         'Days Worked', 'Late Minutes', 'Overtime Minutes',
                         'Basic Pay', 'Overtime Pay', 'Bonus', 'Allowance', 'Gross Pay',
                         'Late Deduction', 'Absent Deduction', 'Custom Deduction Label', 'Custom Deduction',
                         'Total Deductions', 'Net Pay', 'Status', 'Paid At'])
        for r in rows:
            writer.writerow([
                _csv_safe(r.get('employee_code', '')),
                _csv_safe(r.get('employee_name', '')),
                _csv_safe(r.get('position', '')),
                _csv_safe(r.get('pay_period_start', '')),
                _csv_safe(r.get('pay_period_end', '')),
                r.get('total_days_worked', 0),
                r.get('total_late_minutes', 0),
                r.get('total_overtime_minutes', 0) or 0,
                r.get('basic_pay', 0),
                r.get('overtime_pay', 0) or 0,
                r.get('bonus', 0) or 0,
                r.get('allowance', 0) or 0,
                r.get('gross_pay', 0),
                r.get('late_deduction', 0),
                r.get('absent_deduction', 0) or 0,
                _csv_safe(r.get('custom_deduction_label', '') or ''),
                r.get('custom_deduction', 0) or 0,
                r.get('total_deductions', 0),
                r.get('net_pay', 0),
                _csv_safe('Paid' if bool(r.get('received_at')) else 'Pending'),
                _csv_safe(r.get('paid_at', '') or '')
            ])
        csv_bytes = out.getvalue().encode('utf-8-sig')
        filename = f"payroll_{period_start}_to_{period_end}.csv"
        return Response(csv_bytes, mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        print("EXPORT PAYROLL ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500

# ==========================================
# REPORTS
# ==========================================
@app.route('/reports')
def reports_page():
    if not is_logged_in():
        return redirect('/login')
    return render_template('reports.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='reports')


@app.route('/api/reports/summary', methods=['GET'])
def report_summary():
    """✅ FIX: Uses received_at instead of non-existent is_paid column."""
    try:
        farm_id = session.get('farm_id')

        emp_query = supabase.table('employees').select('*').eq('is_active', True)
        if farm_id and farm_id != 0:
            emp_query = emp_query.eq('farm_id', farm_id)
        emp_res = emp_query.execute()
        employees_only = [emp for emp in emp_res.data
                          if emp.get('position') not in ('Farm Owner', 'Owner', 'Business Owner')]
        total_employees = len(employees_only)

        today = datetime.now().date().isoformat()
        att_query = supabase.table('attendance').select('*').eq('attendance_date', today)
        if farm_id and farm_id != 0:
            att_query = att_query.eq('farm_id', farm_id)
        att_res = att_query.execute()
        present = len([a for a in att_res.data if a.get('time_in')])
        late = len([a for a in att_res.data if a.get('late_minutes', 0) > 0])

        range_start = request.args.get('date_from') or datetime.now().replace(day=1).date().isoformat()
        range_end = request.args.get('date_to') or today

        settings_res = supabase.table('farm_settings').select('rest_days').eq('farm_id', farm_id).execute()
        rest_days = settings_res.data[0].get('rest_days', 'sat-sun') if settings_res.data else 'sat-sun'

        range_att_query = supabase.table('attendance').select('*') \
            .gte('attendance_date', range_start).lte('attendance_date', range_end)
        if farm_id and farm_id != 0:
            range_att_query = range_att_query.eq('farm_id', farm_id)
        range_att_res = range_att_query.execute()
        range_attendance = range_att_res.data or []

        days_logged = len([a for a in range_attendance if a.get('time_in')])
        late_incidents = len([a for a in range_attendance if a.get('late_minutes', 0) > 0])

        working_days_in_range = calculate_working_days_in_period(range_start, range_end, rest_days)
        expected_days = total_employees * working_days_in_range
        attendance_rate = round((days_logged / expected_days) * 100, 1) if expected_days > 0 else 0
        total_absences = max(0, expected_days - days_logged)

        # ✅ FIX: Use received_at instead of is_paid
        range_payroll_query = supabase.table('payroll').select('net_pay, received_at') \
            .gte('pay_period_start', range_start).lte('pay_period_start', range_end)
        if farm_id and farm_id != 0:
            range_payroll_query = range_payroll_query.eq('farm_id', farm_id)
        range_payroll_res = range_payroll_query.execute()
        range_payroll = range_payroll_res.data or []
        total_paid = sum(float(p.get('net_pay', 0)) for p in range_payroll if p.get('received_at'))
        total_pending_payroll = sum(float(p.get('net_pay', 0)) for p in range_payroll if not p.get('received_at'))

        try:
            pending_req_query = supabase.table('worker_requests').select('id', count='exact').eq('status', 'Pending')
            if farm_id and farm_id != 0:
                pending_req_query = pending_req_query.eq('farm_id', farm_id)
            pending_req_res = pending_req_query.execute()
            pending_applications = pending_req_res.count or 0
        except Exception as e:
            print(f"⚠️ pending worker requests count failed: {e}")
            pending_applications = 0

        try:
            interview_query = supabase.table('worker_requests').select('id', count='exact').eq('status', 'For Interview')
            if farm_id and farm_id != 0:
                interview_query = interview_query.eq('farm_id', farm_id)
            interview_res = interview_query.execute()
            for_interview_count = interview_res.count or 0
        except Exception as e:
            print(f"⚠️ for-interview count failed: {e}")
            for_interview_count = 0

        return jsonify({"success": True, "data": {
            "total_employees": total_employees,
            "present_today": present, "late_today": late,
            "absent_today": max(0, total_employees - present),
            "range_start": range_start, "range_end": range_end,
            "range_attendance_rate": attendance_rate,
            "range_late_incidents": late_incidents,
            "range_absences": total_absences,
            "range_payroll_paid": round(total_paid, 2),
            "range_payroll_pending": round(total_pending_payroll, 2),
            "pending_applications": pending_applications,
            "open_job_postings": for_interview_count
        }}), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/reports/attendance-trend', methods=['GET'])
def report_attendance_trend():
    try:
        farm_id = session.get('farm_id')
        emp_query = supabase.table('employees').select('id').eq('is_active', True)
        if farm_id and farm_id != 0:
            emp_query = emp_query.eq('farm_id', farm_id)
        emp_res = emp_query.execute()
        total_employees = len(emp_res.data) if emp_res.data else 0

        default_start = datetime.now().replace(day=1).date().isoformat()
        default_end = datetime.now().date().isoformat()
        range_start = request.args.get('date_from') or default_start
        range_end = request.args.get('date_to') or default_end

        start_date = datetime.fromisoformat(range_start).date()
        end_date = datetime.fromisoformat(range_end).date()
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        att_query = supabase.table('attendance').select('attendance_date, time_in, late_minutes') \
            .gte('attendance_date', start_date.isoformat()).lte('attendance_date', end_date.isoformat())
        if farm_id and farm_id != 0:
            att_query = att_query.eq('farm_id', farm_id)
        att_res = att_query.execute()
        attendance = att_res.data or []

        by_date = {}
        current = start_date
        while current <= end_date:
            by_date[current.isoformat()] = {"present": 0, "late": 0}
            current += timedelta(days=1)

        for a in attendance:
            d = a.get('attendance_date')
            if d not in by_date:
                continue
            if a.get('time_in'):
                by_date[d]["present"] += 1
            if a.get('late_minutes', 0) and a.get('late_minutes', 0) > 0:
                by_date[d]["late"] += 1

        labels, present_series, late_series, absent_series = [], [], [], []
        for d, counts in sorted(by_date.items()):
            labels.append(datetime.fromisoformat(d).strftime("%b %d"))
            present_series.append(counts["present"])
            late_series.append(counts["late"])
            absent_series.append(max(0, total_employees - counts["present"]))

        return jsonify({"success": True, "data": {
            "labels": labels, "present": present_series,
            "late": late_series, "absent": absent_series
        }}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# SCANNER
# ==========================================
@app.route('/scanner')
def scanner_page():
    return send_file('scanner.html')


@app.route('/scanner/download')
@role_required(['admin'])
def scanner_download():
    farm_id = session.get('farm_id')
    settings = {}
    try:
        res = supabase.table('farm_settings').select(
            'scanner_mode, qr_format, sound_feedback, scan_cooldown, show_employee_photo'
        ).eq('farm_id', farm_id).execute()
        if res.data:
            settings = res.data[0]
    except Exception as e:
        print("LOAD SCANNER SETTINGS ERROR:", str(e))
    return render_template('scanner_download.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='scanner',
                           settings=settings)


@app.route('/scanner/download/file')
@role_required(['admin'])
def scanner_download_file():
    try:
        farm_id = session.get('farm_id')
        settings_res = supabase.table('farm_settings').select('scanner_mode').eq('farm_id', farm_id).execute()
        scanner_mode = 'camera'
        if settings_res.data:
            scanner_mode = settings_res.data[0].get('scanner_mode', 'camera')

        if scanner_mode == 'flexible':
            default_mode = 'camera'
            locked_mode = 'null'
            filename_suffix = 'flexible'  # ✅ Clear naming
        else:
            default_mode = scanner_mode
            locked_mode = f"'{scanner_mode}'"
            filename_suffix = scanner_mode

        with open('scanner.html', 'r', encoding='utf-8') as f:
            scanner_content = f.read()

        scanner_content = scanner_content.replace(
            "let currentScannerType = 'camera';",
            f"let currentScannerType = '{default_mode}';")
        scanner_content = scanner_content.replace(
            "let lockedScannerType = null;",
            f"let lockedScannerType = {locked_mode};")

        return send_file(BytesIO(scanner_content.encode('utf-8')),
                         mimetype='text/html', as_attachment=True,
                         download_name=f'Dutipay-scanner-{filename_suffix}.html')
    except Exception as e:
        print("DOWNLOAD SCANNER ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500

# ==========================================
# SETTINGS
# ==========================================
@app.route('/settings')
def settings_page():
    if not is_logged_in():
        return redirect('/login')
    farm_id = session.get('farm_id')
    settings = {}
    try:
        res = supabase.table('farm_settings').select('*').eq('farm_id', farm_id).execute()
        if res.data:
            settings = res.data[0]
        farm_res = supabase.table('farms').select('business_code').eq('id', farm_id).execute()
        if farm_res.data:
            settings['business_code'] = farm_res.data[0].get('business_code')
    except Exception as e:
        print("LOAD SETTINGS ERROR:", str(e))
    return render_template('settings.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='settings',
                           settings=settings)


@app.route('/api/settings', methods=['GET'])
def get_settings():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": True, "data": None}), 200
        farm_res = supabase.table('farms').select(
            'farm_name, display_name, owner_email, logo_url, tagline, business_code'
        ).eq('id', farm_id).execute()
        farm_info = farm_res.data[0] if farm_res.data else {}
        settings_res = supabase.table('farm_settings').select('*').eq('farm_id', farm_id).execute()
        settings_data = settings_res.data[0] if settings_res.data else {}
        combined = {**settings_data}
        if farm_info:
            combined['farm_name'] = farm_info.get('display_name') or farm_info.get('farm_name')
            combined['farm_owner'] = farm_info.get('display_name') or farm_info.get('farm_name')
            combined['farm_email'] = farm_info.get('owner_email')
            combined['logo_url'] = farm_info.get('logo_url')
            combined['tagline'] = farm_info.get('tagline')
            combined['business_code'] = farm_info.get('business_code')
        return jsonify({"success": True, "data": combined}), 200
    except Exception as e:
        print("GET SETTINGS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/settings/rate-limiter-status', methods=['GET'])
def rate_limiter_status():
    if not is_logged_in() or session.get('role') != 'admin':
        return jsonify({"success": False, "message": "Access denied"}), 403
    now = time.time()
    active_limits = {}
    for key, timestamps in _rate_limit_store.items():
        active = [t for t in timestamps if t > now - 3600]
        if active:
            active_limits[key] = {"count": len(active),
                                  "oldest_age_seconds": int(now - min(active))}
    return jsonify({"success": True, "active_limits": active_limits,
                    "total_keys": len(_rate_limit_store)}), 200


@app.route('/api/settings/regenerate-business-code', methods=['POST'])
def regenerate_business_code():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    if session.get('role') != 'admin':
        return jsonify({"success": False, "message": "Only Admin can regenerate"}), 403
    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": False, "message": "No business assigned"}), 403
        farm_res = supabase.table('farms').select('farm_name, display_name, owner_email, business_code') \
            .eq('id', farm_id).execute()
        if not farm_res.data:
            return jsonify({"success": False, "message": "Business not found"}), 404
        farm = farm_res.data[0]
        old_code = farm.get('business_code')
        business_name = farm.get('display_name') or farm.get('farm_name') or ''

        new_code = None
        for _ in range(10):
            candidate = generate_business_code(business_name)
            check = supabase.table('farms').select('id').eq('business_code', candidate).execute()
            if not check.data:
                new_code = candidate
                break
        if not new_code:
            return jsonify({"success": False, "message": "Could not generate unique code."}), 500

        supabase.table('farms').update({"business_code": new_code}).eq('id', farm_id).execute()
                # ✅ Audit log
        log_action('business.code_regenerated', 'farm', farm_id, {
            'old_code': old_code,
            'new_code': new_code
        })
        try:
            owner_email = farm.get('owner_email')
            if owner_email:
                send_email(owner_email, "Your business code has been regenerated",
                    "Business Code Updated",
                    ["Your business code has been regenerated for security reasons.",
                     "Please share the new code with your employees.",
                     "The old code is no longer valid."],
                    farm_name=farm.get('display_name') or 'Dutipay')
        except Exception as mail_err:
            print(f"⚠️ Code regen email error: {mail_err}")
        return jsonify({"success": True,
                        "message": "✅ New code generated!",
                        "new_code": new_code, "old_code": old_code}), 200
    except Exception as e:
        print("REGENERATE CODE ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/settings', methods=['POST'])
def save_settings():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    role = session.get('role')
    if role != 'admin':
        return jsonify({"success": False, "message": "Only admin can edit settings."}), 403
    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": False, "message": "No company assigned"}), 403

        if request.content_type and 'multipart/form-data' in request.content_type:
            data = request.form.to_dict()
        else:
            data = request.get_json() or {}

        # ✅ DEBUG: Log kung ano ang nareceive
        print(f"💾 SAVE SETTINGS: farm_id={farm_id}, keys={list(data.keys())}")
        print(f"   manager_can_view_salary: {data.get('manager_can_view_salary')}")
        print(f"   manager_can_view_payslip: {data.get('manager_can_view_payslip')}")
        print(f"   manager_can_print_payslip: {data.get('manager_can_print_payslip')}")

        payload = {"farm_id": farm_id, "updated_at": datetime.now().isoformat()}

        if 'logo' in request.files:
            file = request.files['logo']
            if file and file.filename:
                allowed_ext = ['png', 'jpg', 'jpeg', 'svg', 'webp']
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
                if ext not in allowed_ext:
                    return jsonify({"success": False, "message": f"Invalid logo format. Allowed: {', '.join(allowed_ext).upper()}"}), 400
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)
                if size > 2 * 1024 * 1024:
                    return jsonify({"success": False, "message": "Logo too large. Max 2MB."}), 400
                try:
                    file_bytes = file.read()
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    unique_filename = f"logo_{farm_id}_{timestamp}.{ext}"
                    supabase.storage.from_('farm_logos').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": file.content_type, "upsert": "true"})
                    logo_url = supabase.storage.from_('farm_logos').get_public_url(unique_filename)
                    supabase.table('farms').update({
                        "logo_url": logo_url,
                        "display_name": data.get('farm_name') or data.get('display_name'),
                        "tagline": data.get('tagline')
                    }).eq('id', farm_id).execute()
                except Exception as e:
                    print(f"❌ Logo upload error: {e}")
                    return jsonify({"success": False, "message": f"Logo upload failed: {str(e)}"}), 500

        payload.update({
            "farm_name": data.get('farm_name'),
            "farm_owner": data.get('farm_owner'),
            "farm_email": data.get('farm_email'),
            "farm_phone": data.get('farm_phone'),
            "farm_address": data.get('farm_address'),
            "currency": data.get('currency', 'PHP'),
            "timezone": data.get('timezone', 'Asia/Manila'),
            "default_time_in": data.get('default_time_in', '08:00'),
            "default_time_out": data.get('default_time_out', '17:00'),
            "lunch_break": int(data.get('lunch_break', 60)),
            "working_days": int(data.get('working_days', 22)),
            "rest_days": data.get('rest_days', 'sat-sun'),
            "shift_type": data.get('shift_type', 'day'),
            "grace_period": int(data.get('grace_period', 5)),
            "late_threshold": int(data.get('late_threshold', 5)),
            "auto_timeout": data.get('auto_timeout', 'disabled'),
            "end_of_day": data.get('end_of_day', '23:59'),
            "require_photo": bool(data.get('require_photo', True)),
            "block_duplicate": bool(data.get('block_duplicate', True)),
            "allow_manual_entry": bool(data.get('allow_manual_entry', False)),
            "pay_frequency": data.get('pay_frequency', 'semi-monthly'),
            "salary_basis": data.get('salary_basis', 'monthly'),
            "payroll_working_days": int(data.get('payroll_working_days', 22)),
            "cutoff_day": data.get('cutoff_day', '15-30'),
            "include_owner": bool(data.get('include_owner', False)),
            "auto_generate": bool(data.get('auto_generate', True)),
            "qr_format": data.get('qr_format', 'standard'),
            "scanner_mode": data.get('scanner_mode', 'camera'),
            "scan_cooldown": int(data.get('scan_cooldown', 3)),
            "sound_feedback": data.get('sound_feedback', 'enabled'),
            "show_employee_photo": bool(data.get('show_employee_photo', True)),
            "offline_mode": bool(data.get('offline_mode', False)),
            "late_deduction_enabled": bool(data.get('late_deduction_enabled', False)),
            "late_deduction_rate": float(data.get('late_deduction_rate', 0)),
            "absent_deduction_enabled": bool(data.get('absent_deduction_enabled', False)),
            "absent_deduction_rate": float(data.get('absent_deduction_rate', 0)),
            "bonus_enabled": bool(data.get('bonus_enabled', False)),
            "bonus_amount": float(data.get('bonus_amount', 0)),
            "allowance_enabled": bool(data.get('allowance_enabled', False)),
            "allowance_amount": float(data.get('allowance_amount', 0)),
            "overtime_enabled": bool(data.get('overtime_enabled', False)),
            "overtime_rate": float(data.get('overtime_rate', 1.25)),
            "custom_deduction_enabled": bool(data.get('custom_deduction_enabled', False)),
            "custom_deduction_label": data.get('custom_deduction_label', ''),
            "custom_deduction_amount": float(data.get('custom_deduction_amount', 0)),
            
            # ✅ Manager Access Control (3 settings)
            "manager_can_view_salary": bool(data.get('manager_can_view_salary', False)),
            "manager_can_view_payslip": bool(data.get('manager_can_view_payslip', False)),
            "manager_can_print_payslip": bool(data.get('manager_can_print_payslip', False)),
        })

        supabase.table('farms').update({
            "display_name": data.get('farm_name') or data.get('display_name'),
            "tagline": data.get('tagline')
        }).eq('id', farm_id).execute()

        existing = supabase.table('farm_settings').select('id').eq('farm_id', farm_id).execute()
        if existing.data:
            res = supabase.table('farm_settings').update(payload).eq('farm_id', farm_id).execute()
        else:
            res = supabase.table('farm_settings').insert(payload).execute()

        if not res.data:
            return jsonify({"success": False, "message": "Failed to save settings"}), 500

        print(f"✅ Settings saved for farm_id={farm_id}")
        return jsonify({"success": True, "message": "✅ Settings saved!", "data": res.data[0]}), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("SAVE SETTINGS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/attendance/photos')
@role_required(['admin', 'manager'])
def attendance_photos_page():
    return redirect('/attendance')


def auto_expire_interview_requests(farm_id=None, grace_days=3):
    try:
        today = datetime.now().date()
        cutoff = (today - timedelta(days=grace_days)).isoformat()
        q = supabase.table('worker_requests') \
            .select('id, first_name, last_name, interview_date, farm_id') \
            .eq('status', 'For Interview').lte('interview_date', cutoff)
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)
        res = q.execute()
        expired = res.data or []
        if not expired:
            return {"success": True, "expired_count": 0, "details": []}
        details = []
        for req in expired:
            try:
                supabase.table('worker_requests').update({
                    "status": "Rejected",
                    "rejection_reason": f"Auto-rejected: Interview date passed (grace: {grace_days} days).",
                    "reviewed_at": datetime.now().isoformat(),
                    "final_decision_at": datetime.now().isoformat()
                }).eq('id', req['id']).execute()
                details.append({"id": req['id'],
                                "applicant": f"{req.get('first_name', '')} {req.get('last_name', '')}".strip(),
                                "interview_date": req.get('interview_date')})
            except Exception as inner:
                print(f"⚠️ Auto-expire error: {inner}")
        return {"success": True, "expired_count": len(details), "details": details}
    except Exception as e:
        print(f"⚠️ AUTO-EXPIRE ERROR: {e}")
        return {"success": False, "message": str(e), "expired_count": 0, "details": []}


# ==========================================
# WORKER REQUESTS
# ==========================================
def _attach_requester_info(rows):
    if not rows:
        return rows
    ids = list({r.get('requested_by') for r in rows if r.get('requested_by') is not None})
    user_map = {}
    if ids:
        try:
            u_res = supabase.table('users').select('id, username, employee_code').in_('id', ids).execute()
            for u in (u_res.data or []):
                name = u.get('username', '') or 'Unknown'
                code = u.get('employee_code', '')
                if code:
                    try:
                        emp_res = supabase.table('employees').select('first_name, last_name') \
                            .eq('employee_code', code).execute()
                        if emp_res.data:
                            e = emp_res.data[0]
                            name = f"{e.get('first_name', '')} {e.get('last_name', '')}".strip() or name
                    except Exception as inner:
                        print(f"⚠️ Employee lookup: {inner}")
                user_map[u['id']] = name
        except Exception as e:
            print(f"⚠️ _attach_requester_info error: {e}")
    for r in rows:
        r['requester_name'] = user_map.get(r.get('requested_by'),
                                            'Self-registered' if r.get('source') == 'self_registration' else 'Unknown')
    return rows


@app.route('/api/worker-requests', methods=['GET'])
@role_required(['admin', 'manager'])
def list_worker_requests():
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')
        user_id = session.get('user_id')
        if not farm_id:
            return jsonify({"success": True, "data": []}), 200
        try:
            auto_expire_interview_requests(farm_id=farm_id, grace_days=3)
        except Exception as exp_err:
            print(f"⚠️ Auto-expire check failed: {exp_err}")
        q = supabase.table('worker_requests').select('*').eq('farm_id', farm_id).order('created_at', desc=True)
        if role == 'manager':
            q = q.eq('requested_by', user_id)
        res = q.execute()
        rows = _attach_requester_info(res.data or [])
        return jsonify({"success": True, "data": rows}), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("LIST WORKER REQUESTS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/count', methods=['GET'])
@role_required(['admin'])
def worker_requests_count():
    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": True, "count": 0}), 200
        res = supabase.table('worker_requests').select('id', count='exact') \
            .eq('farm_id', farm_id).in_('status', ['Pending', 'For Interview']).execute()
        return jsonify({"success": True, "count": res.count or 0}), 200
    except Exception as e:
        print("WORKER REQUESTS COUNT ERROR:", str(e))
        return jsonify({"success": True, "count": 0}), 200


@app.route('/api/worker-requests', methods=['POST'])
@role_required(['manager'])
def submit_worker_request():
    try:
        farm_id = session.get('farm_id')
        user_id = session.get('user_id')
        if not farm_id:
            return jsonify({"success": False, "message": "No company assigned"}), 403

        first_name = (request.form.get('first_name') or '').strip()
        last_name = (request.form.get('last_name') or '').strip()
        email = (request.form.get('email') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        position = (request.form.get('position') or '').strip()
        basic_salary = request.form.get('basic_salary', 0)
        schedule_in = request.form.get('schedule_in', '08:00:00')
        schedule_out = request.form.get('schedule_out', '17:00:00')
        notes = (request.form.get('notes') or '').strip()

        if not first_name or not last_name or not position:
            return jsonify({"success": False, "message": "Missing required fields"}), 400

        resume_url = resume_filename = None
        if 'resume' in request.files:
            f = request.files['resume']
            if f and f.filename:
                try:
                    file_bytes = f.read()
                    resume_filename = f.filename
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = f.filename.replace(' ', '_')
                    unique_filename = f"worker_req/{farm_id}/{ts}_resume_{safe_name}"
                    supabase.storage.from_('worker_documents').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": f.content_type})
                    resume_url = unique_filename
                except Exception as e:
                    print(f"❌ Resume upload error: {e}")
                    return jsonify({"success": False, "message": f"Resume upload failed: {str(e)}"}), 500

        bio_data_url = bio_data_filename = None
        if 'bio_data' in request.files:
            f = request.files['bio_data']
            if f and f.filename:
                try:
                    file_bytes = f.read()
                    bio_data_filename = f.filename
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = f.filename.replace(' ', '_')
                    unique_filename = f"worker_req/{farm_id}/{ts}_bio_{safe_name}"
                    supabase.storage.from_('worker_documents').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": f.content_type})
                    bio_data_url = unique_filename
                except Exception as e:
                    print(f"❌ Bio data upload error: {e}")
                    return jsonify({"success": False, "message": f"Bio data upload failed: {str(e)}"}), 500

        payload = {
            "farm_id": farm_id, "requested_by": user_id,
            "first_name": first_name, "last_name": last_name,
            "email": email or None, "phone": phone or None,
            "position": position,
            "basic_salary": float(basic_salary) if basic_salary else 0,
            "schedule_in": normalize_time_str(schedule_in, '08:00:00'),
            "schedule_out": normalize_time_str(schedule_out, '17:00:00'),
            "notes": notes or None,
            "resume_url": resume_url, "resume_filename": resume_filename,
            "bio_data_url": bio_data_url, "bio_data_filename": bio_data_filename,
            "status": "Pending"
        }
        res = supabase.table('worker_requests').insert(payload).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to save request"}), 500

        farm_info = get_farm_mail_info(farm_id)
        try:
            send_email(farm_info["owner_email"],
                f"New employee request from {session.get('full_name', 'Admin')}",
                "A new worker request is waiting for review",
                [f"{session.get('full_name', 'A manager')} submitted a new worker request."],
                farm_name=farm_info["name"],
                details=[("Applicant", f"{first_name} {last_name}"), ("Position", position),
                         ("Email", email or "—"), ("Phone", phone or "—"),
                         ("Proposed salary", f"₱{float(basic_salary) if basic_salary else 0:,.2f}")],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ Employee admin notify error: {mail_err}")

        try:
            notify_applicant_application_received(res.data[0], farm_info)
        except Exception as mail_err:
            print(f"⚠️ Applicant acknowledgment error: {mail_err}")

        return jsonify({"success": True, "message": "✅ Employee submitted!",
                        "data": res.data}), 201
    except Exception as e:
        print("SUBMIT WORKER REQUEST ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/<int:req_id>/schedule-interview', methods=['POST'])
@role_required(['admin'])
def schedule_worker_interview(req_id):
    try:
        farm_id = session.get('farm_id')
        data = request.json or {}
        interview_date = (data.get('interview_date') or '').strip()
        interview_time = (data.get('interview_time') or '').strip()
        interview_location = (data.get('interview_location') or '').strip()
        interview_notes = (data.get('interview_notes') or '').strip()
        if not interview_date or not interview_time:
            return jsonify({"success": False, "message": "Interview date and time required."}), 400
        if len(interview_time.split(':')) == 2:
            interview_time += ':00'

        req_res = supabase.table('worker_requests').select('*').eq('id', req_id).execute()
        if not req_res.data:
            return jsonify({"success": False, "message": "Request not found"}), 404
        req = req_res.data[0]
        if req.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if req.get('status') not in ('Pending', 'For Interview'):
            return jsonify({"success": False, "message": f"Cannot schedule — status is {req.get('status')}"}), 400

        is_reschedule = req.get('status') == 'For Interview'
        update_data = {
            "interview_date": interview_date, "interview_time": interview_time,
            "interview_location": interview_location, "interview_notes": interview_notes,
            "status": "For Interview", "interview_sent_at": datetime.now().isoformat()
        }
        res = supabase.table('worker_requests').update(update_data).eq('id', req_id).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to update request"}), 500
        updated = res.data[0]
        farm_info = get_farm_mail_info(farm_id)
        _notify_applicant_and_manager_about_interview(updated, farm_info, is_reschedule=is_reschedule)
        return jsonify({"success": True,
                        "message": "Interview rescheduled!" if is_reschedule else "Interview scheduled!",
                        "data": updated}), 200
    except Exception as e:
        print("SCHEDULE INTERVIEW ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/<int:req_id>/hire-direct', methods=['POST'])
@role_required(['admin'])
def hire_worker_directly(req_id):
    """✅ FIX: Uses hashed_password"""
    try:
        farm_id = session.get('farm_id')
        admin_id = session.get('user_id')
        req_res = supabase.table('worker_requests').select('*').eq('id', req_id).execute()
        if not req_res.data:
            return jsonify({"success": False, "message": "Request not found"}), 404
        req = req_res.data[0]
        if req.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if req.get('status') == 'Approved':
            return jsonify({"success": False, "message": "Already hired"}), 400
        if req.get('status') == 'Rejected':
            return jsonify({"success": False, "message": "Already rejected"}), 400

        count_res = supabase.table('employees').select('id', count='exact').eq('farm_id', farm_id).execute()
        total_count = count_res.count if count_res.count else 0
        owner_count = supabase.table('employees').select('id', count='exact') \
            .eq('farm_id', farm_id).eq('position', 'Farm Owner').execute()
        owner_total = owner_count.count if owner_count.count else 0
        employee_number = (total_count - owner_total) + 1
        new_employee_code = f"EMP{employee_number:03d}"
        while True:
            existing = supabase.table('employees').select('id').eq('employee_code', new_employee_code).execute()
            if not existing.data:
                break
            employee_number += 1
            new_employee_code = f"EMP{employee_number:03d}"

        position = req.get('position') or 'Staff'
        qr_string = f"EMP:{new_employee_code}|NAME:{req['first_name']} {req['last_name']}|ROLE:{position}"
        employee_data = {
            "employee_code": new_employee_code,
            "first_name": req['first_name'], "last_name": req['last_name'],
            "email": req.get('email'), "position": position,
            "basic_salary": float(req.get('basic_salary') or 0),
            "schedule_in": normalize_time_str(req.get('schedule_in'), '08:00:00'),
            "schedule_out": normalize_time_str(req.get('schedule_out'), '17:00:00'),
            "qr_code_data": qr_string, "is_active": True, "farm_id": farm_id
        }
        emp_res = supabase.table('employees').insert(employee_data).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Failed to create employee"}), 500

        username = new_employee_code.lower()
        default_password = req.get('applicant_password') or "password123"
        hashed_password = hash_password(default_password)

        user_data = {
            "username": username,
            "password_hash": hashed_password,  # ✅ FIXED
            "role": "employee",
            "employee_code": new_employee_code,
            "is_active": True, "farm_id": farm_id
        }
        existing_user = supabase.table('users').select('id').eq('username', username).execute()
        if not existing_user.data:
            user_res = supabase.table('users').insert(user_data).execute()
            if not user_res.data:
                supabase.table('employees').delete().eq('id', emp_res.data[0]['id']).execute()
                return jsonify({"success": False, "message": "Failed to create user"}), 500

        supabase.table('worker_requests').update({
            "status": "Approved", "reviewed_by": admin_id,
            "reviewed_at": datetime.now().isoformat(),
            "final_decision_at": datetime.now().isoformat(),
            "interviewed_at": None
        }).eq('id', req_id).execute()

        farm_info = get_farm_mail_info(farm_id)
        _notify_hire(req, farm_info, employee_code=new_employee_code)
        return jsonify({"success": True,
                        "message": f"✅ Hired! Employee {new_employee_code} created.",
                        "employee_code": new_employee_code,
                        "username": username, "password": default_password}), 200
    except Exception as e:
        print("HIRE DIRECT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/<int:req_id>/approve', methods=['POST'])
@role_required(['admin'])
def approve_worker_request(req_id):
    """✅ FIX: Uses hashed_password"""
    try:
        farm_id = session.get('farm_id')
        admin_id = session.get('user_id')
        req_res = supabase.table('worker_requests').select('*').eq('id', req_id).execute()
        if not req_res.data:
            return jsonify({"success": False, "message": "Request not found"}), 404
        req = req_res.data[0]
        if req.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if req.get('status') not in ('Pending', 'For Interview'):
            return jsonify({"success": False, "message": f"Cannot approve — status is {req.get('status')}"}), 400

        count_res = supabase.table('employees').select('id', count='exact').eq('farm_id', farm_id).execute()
        total_count = count_res.count if count_res.count else 0
        owner_count = supabase.table('employees').select('id', count='exact') \
            .eq('farm_id', farm_id).eq('position', 'Farm Owner').execute()
        owner_total = owner_count.count if owner_count.count else 0
        employee_number = (total_count - owner_total) + 1
        new_employee_code = f"EMP{employee_number:03d}"
        while True:
            existing = supabase.table('employees').select('id').eq('employee_code', new_employee_code).execute()
            if not existing.data:
                break
            employee_number += 1
            new_employee_code = f"EMP{employee_number:03d}"

        position = req.get('position') or 'Staff'
        qr_string = f"EMP:{new_employee_code}|NAME:{req['first_name']} {req['last_name']}|ROLE:{position}"
        employee_data = {
            "employee_code": new_employee_code,
            "first_name": req['first_name'], "last_name": req['last_name'],
            "email": req.get('email'), "position": position,
            "basic_salary": float(req.get('basic_salary') or 0),
            "schedule_in": normalize_time_str(req.get('schedule_in'), '08:00:00'),
            "schedule_out": normalize_time_str(req.get('schedule_out'), '17:00:00'),
            "qr_code_data": qr_string, "is_active": True, "farm_id": farm_id
        }
        emp_res = supabase.table('employees').insert(employee_data).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Failed to create employee"}), 500

        username = new_employee_code.lower()
        default_password = req.get('applicant_password') or "password123"
        hashed_password = hash_password(default_password)

        user_data = {
            "username": username,
            "password_hash": hashed_password,  # ✅ FIXED
            "role": "employee",
            "employee_code": new_employee_code,
            "is_active": True, "farm_id": farm_id
        }
        existing_user = supabase.table('users').select('id').eq('username', username).execute()
        if not existing_user.data:
            user_res = supabase.table('users').insert(user_data).execute()
            if not user_res.data:
                supabase.table('employees').delete().eq('id', emp_res.data[0]['id']).execute()
                return jsonify({"success": False, "message": "Failed to create user"}), 500

        supabase.table('worker_requests').update({
            "status": "Approved", "reviewed_by": admin_id,
            "reviewed_at": datetime.now().isoformat(),
            "final_decision_at": datetime.now().isoformat(),
            "interviewed_at": datetime.now().isoformat() if req.get('status') == 'For Interview' else None
        }).eq('id', req_id).execute()

        # ✅ Audit log
        log_action('worker_request.approved', 'worker_request', req_id, {
            'applicant': f"{req.get('first_name', '')} {req.get('last_name', '')}".strip(),
            'new_employee_code': new_employee_code,
            'position': position
        })

        try:
            farm_info = get_farm_mail_info(farm_id)
            base_url = APP_URL or request.host_url.rstrip('/')
            login_link = f"{base_url}/login"
            qr_link = f"{base_url}/my-qr"

            send_email(req.get('email'),
                f"Welcome to {farm_info['name']} — Your account is active!",
                "Your account is now active!",
                [f"Hi {req.get('first_name', '')}, your account has been approved.",
                 "You can now log in to Dutipay.",
                 f"Use the password you chose during registration.",
                 f"If you forgot it, use 'Forgot Password' on the login page."],
                farm_name=farm_info["name"],
                details=[("Business", farm_info['name']),
                         ("Employee Code", new_employee_code),
                         ("Username", username),
                         ("Position", position or "Employee")],
                button=("Log In Now", login_link))

            send_email(req.get('email'),
                f"Your Attendance QR Code — {farm_info['name']}",
                "Your QR Code is Ready",
                [f"Hi {req.get('first_name', '')}, your personal attendance QR code is ready.",
                 "You can view, download, and print it from your account.",
                 "Show this QR code to the scanner to record your time-in and time-out."],
                farm_name=farm_info["name"],
                details=[("Employee Code", new_employee_code)],
                button=("View My QR Code", qr_link))
        except Exception as mail_err:
            print(f"⚠️ Hire email error: {mail_err}")

        return jsonify({"success": True,
                        "message": f"✅ Approved! Employee {new_employee_code} created.",
                        "employee_code": new_employee_code,
                        "username": username, "password": default_password}), 200
    except Exception as e:
        print("APPROVE WORKER REQUEST ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/<int:req_id>/reject', methods=['POST'])
@role_required(['admin'])
def reject_worker_request(req_id):
    try:
        farm_id = session.get('farm_id')
        admin_id = session.get('user_id')
        data = request.json or {}
        reason = (data.get('reason') or '').strip() or 'No reason provided'

        req_res = supabase.table('worker_requests').select('*').eq('id', req_id).execute()
        if not req_res.data:
            return jsonify({"success": False, "message": "Request not found"}), 404
        req = req_res.data[0]
        if req.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if req.get('status') not in ('Pending', 'For Interview'):
            return jsonify({"success": False, "message": f"Cannot reject — status is {req.get('status')}"}), 400

        supabase.table('worker_requests').update({
            "status": "Rejected", "rejection_reason": reason,
            "reviewed_by": admin_id,
            "reviewed_at": datetime.now().isoformat(),
            "final_decision_at": datetime.now().isoformat()
        }).eq('id', req_id).execute()

        # ✅ Audit log
        log_action('worker_request.rejected', 'worker_request', req_id, {
            'applicant': f"{req.get('first_name', '')} {req.get('last_name', '')}".strip(),
            'reason': reason
        })

        try:
            farm_info = get_farm_mail_info(farm_id)
            _notify_rejection(req, farm_info, reason=reason)
        except Exception as mail_err:
            print(f"⚠️ Rejection email error: {mail_err}")

        return jsonify({"success": True, "message": "Request rejected."}), 200
    except Exception as e:
        print("REJECT WORKER REQUEST ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/worker-requests/<int:req_id>/documents', methods=['GET'])
@role_required(['admin'])
def get_worker_request_documents(req_id):
    try:
        farm_id = session.get('farm_id')
        req_res = supabase.table('worker_requests').select(
            'farm_id, resume_url, resume_filename, bio_data_url, bio_data_filename'
        ).eq('id', req_id).execute()
        if not req_res.data:
            return jsonify({"success": False, "message": "Request not found"}), 404
        req = req_res.data[0]
        if req.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403

        def signed(path, filename):
            if not path:
                return None
            try:
                res = supabase.storage.from_('worker_documents').create_signed_url(path, 86400)
                url = res.get('signedURL') or res.get('signed_url') or res.get('signedUrl')
                return {"url": url, "filename": filename} if url else None
            except Exception as e:
                print(f"⚠️ Signed URL error: {e}")
                return None
        return jsonify({"success": True, "data": {
            "resume": signed(req.get('resume_url'), req.get('resume_filename')),
            "bio_data": signed(req.get('bio_data_url'), req.get('bio_data_filename'))
        }}), 200
    except Exception as e:
        print("DOCUMENTS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# CUTOFF HELPERS
# ==========================================
def check_manager_permission(role, settings, permission_key):
    """Check if the current role has permission for an action."""
    if role == 'admin':
        return True
    if role == 'manager':
        return bool(settings.get(permission_key, False))
    return False
def log_action(action, target_type=None, target_id=None, details=None):
    """Log a sensitive action to audit_logs."""
    try:
        supabase.table('audit_logs').insert({
            "farm_id": session.get('farm_id'),
            "user_id": session.get('user_id'),
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": details or {},
            "ip_address": get_client_ip(),
            "user_agent": request.headers.get('User-Agent', '')[:500]
        }).execute()
    except Exception as e:
        print(f"⚠️ Audit log error: {e}")
@app.route('/api/cutoffs', methods=['GET'])
def api_get_cutoffs():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        months_back = int(request.args.get('months_back', 3))
        cutoffs = get_cutoff_options(months_back=months_back)
        current = get_current_cutoff()
        return jsonify({"success": True, "current": current, "data": cutoffs}), 200
    except Exception as e:
        print("GET CUTOFFS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/cutoff/current', methods=['GET'])
def api_current_cutoff():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        return jsonify({"success": True, "data": get_current_cutoff()}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ==========================================
# SUPER ADMIN
# ==========================================
# ==========================================
# FEEDBACK MANAGEMENT (Super Admin Only)
# ==========================================
@app.route('/api/super-admin/feedbacks', methods=['GET'])
@super_admin_required
def get_app_feedbacks():
    try:
        # Kunin lahat ng feedback, pinakabago muna
        res = supabase.table('app_feedback') \
            .select('*, users(username, employee_code)') \
            .order('created_at', desc=True) \
            .execute()
        
        feedbacks = res.data or []
        return jsonify({"success": True, "data": feedbacks}), 200
    except Exception as e:
        print("GET FEEDBACKS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
@app.route('/api/super-admin/feedbacks/<int:feedback_id>/reply', methods=['POST'])
@super_admin_required
def reply_to_feedback(feedback_id):
    try:
        data = request.json or {}
        reply_text = (data.get('reply') or '').strip()

        if not reply_text:
            return jsonify({"success": False, "message": "Reply cannot be empty."}), 400

        # I-update ang feedback record
        res = supabase.table('app_feedback').update({
            "admin_reply": reply_text,
            "replied_at": datetime.now().isoformat()
        }).eq('id', feedback_id).execute()

        if not res.data:
            return jsonify({"success": False, "message": "Feedback not found."}), 404

        # ✅ Optional: Kung gusto mo mag-send ng Email Notification sa user
        # try:
        #     user_id = res.data[0].get('user_id')
        #     user_res = supabase.table('users').select('email').eq('id', user_id).execute()
        #     if user_res.data and user_res.data[0].get('email'):
        #         send_email(user_res.data[0]['email'], "Reply to your Dutipay feedback", "We replied!", [reply_text])
        # except Exception as e:
        #     print(f"Email error: {e}")

        return jsonify({"success": True, "message": "Reply sent successfully!"}), 200

    except Exception as e:
        print("REPLY FEEDBACK ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
@app.route('/super-admin')
@super_admin_required
def super_admin_page():
    return render_template('super_admin.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='super_admin')


@app.route('/super-admin/businesses')
@super_admin_required
def super_admin_businesses_page():
    return render_template('super_admin_businesses.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='super_admin_businesses')


@app.route('/super-admin/registrations')
@super_admin_required
def super_admin_registrations_page():
    return render_template('super_admin_registrations.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='super_admin_registrations')


@app.route('/api/super-admin/business-registrations', methods=['GET'])
@super_admin_required
def api_list_business_registrations():
    try:
        status_filter = request.args.get('status')
        q = supabase.table('business_registrations').select('*').order('created_at', desc=True)
        if status_filter:
            q = q.eq('status', status_filter)
        res = q.execute()
        return jsonify({"success": True, "data": res.data or []}), 200
    except Exception as e:
        print("LIST REGISTRATIONS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/super-admin/business-registrations/<int:reg_id>/approve', methods=['POST'])
@super_admin_required
def api_approve_business_registration(reg_id):
    try:
        data = request.json or {}
        admin_username = (data.get('admin_username') or '').strip().lower()
        reg_res = supabase.table('business_registrations').select('*').eq('id', reg_id).execute()
        if not reg_res.data:
            return jsonify({"success": False, "message": "Registration not found"}), 404
        reg = reg_res.data[0]
        if reg.get('status') != 'Pending':
            return jsonify({"success": False, "message": f"Already {reg.get('status')}"}), 400

        if not admin_username:
            base = reg['business_name'].lower().replace(' ', '')[:12]
            base = ''.join(c for c in base if c.isalnum())
            admin_username = f"{base}_admin" if base else f"admin{reg_id}"

        suffix = 0
        original = admin_username
        while True:
            check = supabase.table('users').select('id').eq('username', admin_username).execute()
            if not check.data:
                break
            suffix += 1
            admin_username = f"{original}{suffix}"

        default_password = "password123"
        import random
        base = ''.join(c for c in reg['business_name'].upper() if c.isalnum())[:3] or 'BIZ'
        business_code = None
        for _ in range(10):
            candidate = f"{base}-{random.randint(1000, 9999)}"
            check = supabase.table('farms').select('id').eq('business_code', candidate).execute()
            if not check.data:
                business_code = candidate
                break
        if not business_code:
            return jsonify({"success": False, "message": "Could not generate unique business code."}), 500

        try:
            farm_data = {
                "farm_name": reg['business_name'], "display_name": reg['business_name'],
                "owner_email": reg['owner_email'], "business_code": business_code
            }
            farm_res = supabase.table('farms').insert(farm_data).execute()
            if not farm_res.data:
                return jsonify({"success": False, "message": "Failed to create business record."}), 500
            farm_id = farm_res.data[0]['id']
        except Exception as farm_err:
            err_msg = str(farm_err)
            print(f"❌ Farm creation error: {err_msg}")
            return jsonify({"success": False, "message": f"Failed to create business: {err_msg}"}), 500

        try:
            admin_code_base = f"admin{admin_username[:5].upper()}"
            admin_code = admin_code_base
            code_suffix = 0
            while True:
                existing_code = supabase.table('employees').select('id').eq('employee_code', admin_code).execute()
                if not existing_code.data:
                    break
                code_suffix += 1
                admin_code = f"{admin_code_base}{code_suffix}"
                if code_suffix > 100:
                    return jsonify({"success": False, "message": "Could not generate unique employee code."}), 500

            qr_string = f"EMP:{admin_code}|NAME:{reg['owner_first_name']} {reg['owner_last_name']}|ROLE:Company Owner"
            employee_data = {
                "employee_code": admin_code,
                "first_name": reg['owner_first_name'], "last_name": reg['owner_last_name'],
                "email": reg['owner_email'], "position": "Business Owner",
                "schedule_in": "08:00:00", "schedule_out": "17:00:00",
                "qr_code_data": qr_string, "is_active": True, "farm_id": farm_id
            }
            emp_res = supabase.table('employees').insert(employee_data).execute()
            if not emp_res.data:
                supabase.table('farms').delete().eq('id', farm_id).execute()
                return jsonify({"success": False, "message": "Failed to create owner employee record."}), 500
        except Exception as emp_err:
            try:
                supabase.table('farms').delete().eq('id', farm_id).execute()
            except:
                pass
            print(f"❌ Employee creation error: {str(emp_err)}")
            return jsonify({"success": False, "message": f"Failed to create employee: {str(emp_err)}"}), 500

        try:
            user_data = {
                "username": admin_username,
                "password_hash": hash_password(default_password),
                "role": "admin", "employee_code": admin_code,
                "is_active": True, "farm_id": farm_id
            }
            user_res = supabase.table('users').insert(user_data).execute()
            if not user_res.data:
                supabase.table('employees').delete().eq('id', emp_res.data[0]['id']).execute()
                supabase.table('farms').delete().eq('id', farm_id).execute()
                return jsonify({"success": False, "message": "Failed to create user account."}), 500
        except Exception as user_err:
            try:
                supabase.table('employees').delete().eq('id', emp_res.data[0]['id']).execute()
                supabase.table('farms').delete().eq('id', farm_id).execute()
            except:
                pass
            print(f"❌ User creation error: {str(user_err)}")
            return jsonify({"success": False, "message": f"Failed to create user: {str(user_err)}"}), 500

        try:
            supabase.table('business_registrations').update({
                "status": "Approved",
                "reviewed_by": session.get('user_id'),
                "reviewed_at": datetime.now().isoformat()
            }).eq('id', reg_id).execute()
        except Exception as reg_err:
            print(f"⚠️ Failed to update registration status: {reg_err}")

        try:
            send_email(reg['owner_email'],
                f"Welcome to Dutipay — {reg['business_name']} is approved!",
                "Your business is approved!",
                [f"Hi {reg['owner_first_name']}, your business registration has been approved.",
                 "You can now log in to Dutipay using the credentials below.",
                 "Please change your password after first login.",
                 "Share the Business Code with your employees so they can self-register."],
                farm_name="Dutipay",
                details=[("Business", reg['business_name']),
                         ("Business Code", business_code),
                         ("Admin Username", admin_username),
                         ("Temporary Password", default_password)],
                button=login_button())
        except Exception as mail_err:
            print(f"⚠️ Welcome email error: {mail_err}")

        return jsonify({"success": True,
                        "message": f"✅ Business approved! Admin account: {admin_username}",
                        "username": admin_username, "password": default_password,
                        "business_code": business_code, "farm_id": farm_id}), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("APPROVE REGISTRATION ERROR:", str(e))
        return jsonify({"success": False, "message": f"Unexpected error: {str(e)}"}), 500


@app.route('/api/super-admin/business-registrations/<int:reg_id>/reject', methods=['POST'])
@super_admin_required
def api_reject_business_registration(reg_id):
    try:
        data = request.json or {}
        reason = (data.get('reason') or '').strip() or 'No reason provided'
        reg_res = supabase.table('business_registrations').select('*').eq('id', reg_id).execute()
        if not reg_res.data:
            return jsonify({"success": False, "message": "Registration not found"}), 404
        reg = reg_res.data[0]
        if reg.get('status') != 'Pending':
            return jsonify({"success": False, "message": f"Already {reg.get('status')}"}), 400

        supabase.table('business_registrations').update({
            "status": "Rejected", "rejection_reason": reason,
            "reviewed_by": session.get('user_id'),
            "reviewed_at": datetime.now().isoformat()
        }).eq('id', reg_id).execute()

        try:
            send_email(reg['owner_email'], "Update on your Dutipay registration",
                "Registration update",
                [f"Hi {reg['owner_first_name']}, unfortunately we cannot approve your registration at this time.",
                 "If you believe this is a mistake, contact our support team."],
                farm_name="Dutipay", details=[("Business", reg['business_name'])])
        except Exception as mail_err:
            print(f"⚠️ Rejection email error: {mail_err}")
        return jsonify({"success": True, "message": "Registration rejected."}), 200
    except Exception as e:
        print("REJECT REGISTRATION ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/super-admin/businesses', methods=['GET'])
@super_admin_required
def api_list_businesses():
    try:
        res = supabase.table('farms').select('*').order('created_at', desc=True).execute()
        rows = _attach_business_stats(res.data or [])
        return jsonify({"success": True, "data": rows}), 200
    except Exception as e:
        print("LIST BUSINESSES ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/super-admin/users-count', methods=['GET'])
@super_admin_required
def api_super_admin_users_count():
    try:
        res = supabase.table('users').select('id', count='exact').not_.is_('farm_id', 'null').execute()
        return jsonify({"success": True, "count": res.count or 0}), 200
    except Exception as e:
        print("USERS COUNT ERROR:", str(e))
        return jsonify({"success": True, "count": 0}), 200


@app.route('/api/super-admin/businesses/<int:farm_id>/create-admin', methods=['POST'])
@super_admin_required
def api_create_admin_for_business(farm_id):
    """
    ✅ HARDENED:
    - Validates all fields server-side (not relying on readonly fields)
    - Validates email format
    - Validates username format
    - Handles username collisions with clear error
    - Tracks whether email was actually sent
    - Returns fullname + business for correct alert display
    """
    try:
        data = request.json or {}
        first_name = (data.get('first_name') or '').strip()
        last_name = (data.get('last_name') or '').strip()
        email = (data.get('email') or '').strip().lower()
        username = (data.get('username') or '').strip().lower()

        # ============================================
        # ✅ SERVER-SIDE VALIDATION
        # ============================================
        errors = []
        if not first_name or len(first_name) < 2:
            errors.append("First name must be at least 2 characters.")
        if not last_name or len(last_name) < 2:
            errors.append("Last name must be at least 2 characters.")
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            errors.append("Invalid email address.")
        if not username or len(username) < 4:
            errors.append("Username must be at least 4 characters.")
        if username and not all(c.isalnum() or c in '._-' for c in username):
            errors.append("Username can only contain letters, numbers, ., _, -")

        if errors:
            return jsonify({"success": False, "message": " ".join(errors)}), 400

        # ============================================
        # ✅ VERIFY BUSINESS EXISTS
        # ============================================
        farm_res = supabase.table('farms').select('id, farm_name, display_name, owner_email').eq('id', farm_id).execute()
        if not farm_res.data:
            return jsonify({"success": False, "message": "Business not found"}), 404
        farm = farm_res.data[0]
        business_display_name = farm.get('display_name') or farm.get('farm_name') or 'Business'

        # ============================================
        # ✅ USERNAME COLLISION CHECK
        # ============================================
        existing_user = supabase.table('users').select('id, username').eq('username', username).execute()
        if existing_user.data:
            # Suggest alternative
            suggestion = f"{username}2"
            for _ in range(5):
                check = supabase.table('users').select('id').eq('username', suggestion).execute()
                if not check.data:
                    break
                suggestion = f"{suggestion}_1"
            return jsonify({
                "success": False,
                "message": f"Username '{username}' is already taken. Try: '{suggestion}'"
            }), 400

        # ============================================
        # ✅ EMAIL COLLISION CHECK
        # ============================================
        existing_email = supabase.table('employees').select('id, employee_code') \
            .eq('email', email).execute()
        if existing_email.data:
            return jsonify({
                "success": False,
                "message": f"This email is already registered as an employee ({existing_email.data[0].get('employee_code')})."
            }), 400

        # ============================================
        # ✅ GENERATE EMPLOYEE CODE (with suffix fallback)
        # ============================================
        admin_code_base = f"admin{username[:5].upper()}"
        admin_code = admin_code_base
        suffix = 0
        while True:
            existing_code = supabase.table('employees').select('id').eq('employee_code', admin_code).execute()
            if not existing_code.data:
                break
            suffix += 1
            admin_code = f"{admin_code_base}{suffix}"
            if suffix > 100:
                return jsonify({"success": False, "message": "Could not generate unique employee code."}), 500

        # ============================================
        # ✅ RANDOM PASSWORD
        # ============================================
        default_password = generate_random_password(12)
        hashed_password = hash_password(default_password)

        # ============================================
        # ✅ CREATE EMPLOYEE
        # ============================================
        qr_string = f"EMP:{admin_code}|NAME:{first_name} {last_name}|ROLE:admin"
        employee_data = {
            "employee_code": admin_code,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "position": "admin",
            "schedule_in": "08:00:00",
            "schedule_out": "17:00:00",
            "qr_code_data": qr_string,
            "is_active": True,
            "farm_id": farm_id
        }
        emp_res = supabase.table('employees').insert(employee_data).execute()
        if not emp_res.data:
            return jsonify({"success": False, "message": "Failed to create employee record."}), 500

        # ============================================
        # ✅ CREATE USER (with must_change_password flag)
        # ============================================
        user_data = {
            "username": username,
            "password_hash": hashed_password,
            "role": "admin",
            "employee_code": admin_code,
            "is_active": True,
            "farm_id": farm_id,
            "must_change_password": True  # ✅ Flag for login enforcement
        }
        try:
            user_res = supabase.table('users').insert(user_data).execute()
        except Exception as ue:
            # Fallback: some Supabase setups don't have must_change_password column
            err_msg = str(ue)
            if 'must_change_password' in err_msg or 'column' in err_msg.lower():
                print(f"⚠️ must_change_password column not present, retrying without it...")
                user_data.pop('must_change_password', None)
                user_res = supabase.table('users').insert(user_data).execute()
            else:
                raise

        if not user_res.data:
            # Rollback employee
            supabase.table('employees').delete().eq('id', emp_res.data[0]['id']).execute()
            return jsonify({"success": False, "message": "Failed to create user account."}), 500

        # ============================================
        # ✅ TRY TO SEND EMAIL (track status)
        # ============================================
        email_sent = False
        email_error = None
        if mail_is_configured():
            try:
                email_sent = send_email(
                    email,
                    f"You've been added as an admin — {business_display_name}",
                    "Your Admin Account is Ready",
                    [
                        f"Hi {first_name}, you've been granted admin access to {business_display_name}.",
                        "Log in with the credentials below and change your password immediately.",
                        "If you did not expect this email, contact your administrator.",
                    ],
                    farm_name=business_display_name,
                    details=[
                        ("Business", business_display_name),
                        ("Username", username),
                        ("Temporary Password", default_password),
                        ("Employee Code", admin_code),
                    ],
                    button=login_button()
                )
            except Exception as mail_err:
                email_error = str(mail_err)
                print(f"⚠️ admin email error: {mail_err}")
        else:
            email_error = "Email not configured"

        # ============================================
        # ✅ RETURN CLEAR RESPONSE
        # ============================================
        return jsonify({
            "success": True,
            "message": f"✅ Admin account created: {username}",
            "username": username,
            "password": default_password,
            "employee_code": admin_code,
            "full_name": f"{first_name} {last_name}",
            "business_name": business_display_name,
            "email_target": email,
            "email_sent": email_sent,
            "email_error": email_error
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        print("CREATE admin ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500
# ==========================================
# AUDIT LOGS PAGE (admin only)
# ==========================================
@app.route('/audit-logs')
@role_required(['admin'])
def audit_logs_page():
    if not is_logged_in():
        return redirect('/login')
    return render_template('audit_logs.html',
                          role=session.get('role'),
                          full_name=session.get('full_name'),
                          active_page='audit_logs')


@app.route('/api/audit-logs', methods=['GET'])
@role_required(['admin'])
def api_list_audit_logs():
    """Return filtered audit logs for the admin's farm."""
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401

    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": True, "data": []}), 200

        # Filters
        action_filter = request.args.get('action')
        user_filter = request.args.get('user_id')
        target_filter = request.args.get('target_type')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        limit = min(int(request.args.get('limit', 100)), 500)  # Max 500

        q = supabase.table('audit_logs').select('*') \
            .eq('farm_id', farm_id) \
            .order('created_at', desc=True) \
            .limit(limit)

        if action_filter:
            q = q.eq('action', action_filter)
        if user_filter:
            q = q.eq('user_id', int(user_filter))
        if target_filter:
            q = q.eq('target_type', target_filter)
        if date_from:
            q = q.gte('created_at', date_from)
        if date_to:
            q = q.lte('created_at', date_to + 'T23:59:59')

        res = q.execute()
        rows = res.data or []

        # Attach user info
        user_ids = list({r['user_id'] for r in rows if r.get('user_id')})
        user_map = {}
        if user_ids:
            try:
                u_res = supabase.table('users').select('id, username, employee_code') \
                    .in_('id', user_ids).execute()
                for u in (u_res.data or []):
                    user_map[u['id']] = u
            except Exception as e:
                print(f"⚠️ user_map error: {e}")

        for r in rows:
            u = user_map.get(r.get('user_id'), {})
            r['username'] = u.get('username') or 'System'
            r['employee_code'] = u.get('employee_code') or ''

        return jsonify({"success": True, "data": rows}), 200
    except Exception as e:
        print("AUDIT LOGS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/audit-logs/actions', methods=['GET'])
@role_required(['admin'])
def api_audit_log_actions():
    """Return distinct list of actions for filter dropdown."""
    try:
        farm_id = session.get('farm_id')
        if not farm_id:
            return jsonify({"success": True, "data": []}), 200

        res = supabase.table('audit_logs').select('action') \
            .eq('farm_id', farm_id).limit(1000).execute()
        
        actions = sorted(set(r['action'] for r in (res.data or [])))
        return jsonify({"success": True, "data": actions}), 200
    except Exception as e:
        print("AUDIT ACTIONS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/super-admin/businesses/<int:farm_id>/toggle-active', methods=['POST'])
@super_admin_required
def api_toggle_business_active(farm_id):
    return jsonify({"success": False, "message": "Not yet implemented"}), 501


@app.route('/api/email/test', methods=['GET'])
@role_required(['admin'])
def email_test():
    if not mail_is_configured():
        return jsonify({"success": False, "message": "Email not configured."}), 400
    farm_info = get_farm_mail_info(session.get('farm_id'))
    target = farm_info["owner_email"]
    if not target:
        return jsonify({"success": False, "message": "No owner email found."}), 400
    send_email(target, "Dutipay test email", "Email is working",
               ["This is a test message from Dutipay."], farm_name=farm_info["name"])
    return jsonify({"success": True, "message": f"Test email queued to {target}."}), 200


# ==========================================
# DAILY REPORTS
# ==========================================
def _current_employee_id_for_reports(farm_id):
    code = session.get('employee_code')
    if not code:
        return None
    q = supabase.table('employees').select('id').eq('employee_code', code)
    if farm_id and farm_id != 0:
        q = q.eq('farm_id', farm_id)
    res = q.execute()
    return res.data[0]['id'] if res.data else None


def _attach_report_employee_info(rows):
    ids = list({r['employee_id'] for r in rows if r.get('employee_id') is not None})
    emp_map = {}
    if ids:
        emp_res = supabase.table('employees').select('id, first_name, last_name, employee_code, position') \
            .in_('id', ids).execute()
        emp_map = {e['id']: e for e in (emp_res.data or [])}
    for r in rows:
        e = emp_map.get(r.get('employee_id'), {})
        r['employee_name'] = f"{e.get('first_name', '')} {e.get('last_name', '')}".strip() or 'Unknown'
        r['employee_code'] = e.get('employee_code', '')
        r['position'] = e.get('position', '')
    return rows


def _attach_report_reviewer_info(rows):
    ids = list({r['reviewed_by'] for r in rows if r.get('reviewed_by') is not None})
    if not ids:
        for r in rows:
            r['reviewer_name'] = None
        return rows
    user_map = {}
    try:
        u_res = supabase.table('users').select('id, username, employee_code').in_('id', ids).execute()
        for u in (u_res.data or []):
            name = u.get('username', '')
            code = u.get('employee_code', '')
            if code:
                emp_res = supabase.table('employees').select('first_name, last_name') \
                    .eq('employee_code', code).execute()
                if emp_res.data:
                    e = emp_res.data[0]
                    name = f"{e.get('first_name', '')} {e.get('last_name', '')}".strip() or name
            user_map[u['id']] = name
    except Exception as e:
        print(f"⚠️ Reviewer info error: {e}")
    for r in rows:
        r['reviewer_name'] = user_map.get(r.get('reviewed_by'))
    return rows


@app.route('/daily-reports')
def daily_reports_staff_page():
    if not is_logged_in():
        return redirect('/login')
    return render_template('daily_reports_staff.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='daily_reports')


@app.route('/reports/daily')
@role_required(['admin', 'manager'])
def daily_reports_admin_page():
    return render_template('daily_reports_admin.html', role=session.get('role'),
                           full_name=session.get('full_name'), active_page='daily_reports_admin')


@app.route('/api/daily-reports', methods=['GET'])
def list_daily_reports():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')

        # ✅ Build base query
        q = supabase.table('daily_reports').select('*')
        if farm_id and farm_id != 0:
            q = q.eq('farm_id', farm_id)

        if role not in ('admin', 'manager'):
            # Staff sees only own reports
            emp_id = _current_employee_id_for_reports(farm_id)
            if not emp_id:
                return jsonify({"success": True, "data": []}), 200
            q = q.eq('employee_id', emp_id)
        else:
            # Admin/manager filters
            employee_id = request.args.get('employee_id')
            if employee_id:
                try:
                    q = q.eq('employee_id', int(employee_id))
                except (ValueError, TypeError):
                    pass

            reviewed = request.args.get('reviewed')
            if reviewed == 'true':
                q = q.not_.is_('reviewed_at', 'null')
            elif reviewed == 'false':
                q = q.is_('reviewed_at', 'null')

        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        if date_from: q = q.gte('report_date', date_from)
        if date_to: q = q.lte('report_date', date_to)

        q = q.order('report_date', desc=True).order('created_at', desc=True)
        res = sb_execute(q)

        rows = res.data or []

        # ✅ Attach employee info (with safe fallback)
        try:
            rows = _attach_report_employee_info(rows)
        except Exception as e:
            print(f"⚠️ attach_report_employee_info failed: {e}")

        # ✅ Attach reviewer info (with safe fallback)
        try:
            rows = _attach_report_reviewer_info(rows)
        except Exception as e:
            print(f"⚠️ attach_report_reviewer_info failed: {e}")
            # Fallback: set None kung may error
            for r in rows:
                r.setdefault('reviewer_name', None)

        return jsonify({"success": True, "data": rows}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        print("LIST DAILY REPORTS ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/daily-reports', methods=['POST'])
def submit_daily_report():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        emp_id = _current_employee_id_for_reports(farm_id)
        if not emp_id:
            return jsonify({"success": False, "message": "Employee record not found"}), 404

        report_date = (request.form.get('report_date') or '').strip()
        tasks_completed = (request.form.get('tasks_completed') or '').strip()
        tasks_in_progress = (request.form.get('tasks_in_progress') or '').strip()
        issues_blockers = (request.form.get('issues_blockers') or '').strip()
        next_steps = (request.form.get('next_steps') or '').strip()
        hours_worked = request.form.get('hours_worked', 0)

        if not report_date:
            return jsonify({"success": False, "message": "Report date is required."}), 400
        if not tasks_completed:
            return jsonify({"success": False, "message": "Tasks completed is required."}), 400
        try:
            hours_worked = float(hours_worked or 0)
        except Exception:
            hours_worked = 0

        attachment_url = attachment_filename = None
        if 'attachment' in request.files:
            f = request.files['attachment']
            if f and f.filename:
                try:
                    file_bytes = f.read()
                    attachment_filename = f.filename
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = f.filename.replace(' ', '_')
                    unique_filename = f"reports/{farm_id}/{emp_id}_{ts}_{safe_name}"
                    supabase.storage.from_('report_attachments').upload(
                        unique_filename, file_bytes,
                        file_options={"content-type": f.content_type})
                    attachment_url = unique_filename
                except Exception as e:
                    print(f"❌ Attachment upload error: {e}")
                    return jsonify({"success": False, "message": f"Attachment upload failed: {str(e)}"}), 500

        payload = {
            "farm_id": farm_id, "employee_id": emp_id, "report_date": report_date,
            "tasks_completed": tasks_completed,
            "tasks_in_progress": tasks_in_progress or None,
            "issues_blockers": issues_blockers or None,
            "next_steps": next_steps or None, "hours_worked": hours_worked,
            "attachment_url": attachment_url, "attachment_filename": attachment_filename
        }
        res = supabase.table('daily_reports').insert(payload).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Failed to save report"}), 500
        return jsonify({"success": True, "message": "✅ Daily report submitted!", "data": res.data[0]}), 201
    except Exception as e:
        print("SUBMIT DAILY REPORT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/daily-reports/<int:report_id>', methods=['PUT'])
def update_daily_report(report_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        emp_id = _current_employee_id_for_reports(farm_id)
        if not emp_id:
            return jsonify({"success": False, "message": "Employee record not found"}), 404
        check = supabase.table('daily_reports').select('*').eq('id', report_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Report not found"}), 404
        report = check.data[0]
        if report.get('employee_id') != emp_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if farm_id and farm_id != 0 and report.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403

        data = request.json or {}
        update_data = {
            "report_date": data.get('report_date'),
            "tasks_completed": data.get('tasks_completed'),
            "tasks_in_progress": data.get('tasks_in_progress'),
            "issues_blockers": data.get('issues_blockers'),
            "next_steps": data.get('next_steps'),
            "hours_worked": float(data.get('hours_worked') or 0),
            "updated_at": datetime.now().isoformat()
        }
        res = supabase.table('daily_reports').update(update_data).eq('id', report_id).execute()
        if not res.data:
            return jsonify({"success": False, "message": "Update failed"}), 500
        return jsonify({"success": True, "message": "Report updated!", "data": res.data[0]}), 200
    except Exception as e:
        print("UPDATE DAILY REPORT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/daily-reports/<int:report_id>', methods=['DELETE'])
def delete_daily_report(report_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        emp_id = _current_employee_id_for_reports(farm_id)
        if not emp_id:
            return jsonify({"success": False, "message": "Employee record not found"}), 404
        check = supabase.table('daily_reports').select('*').eq('id', report_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Report not found"}), 404
        report = check.data[0]
        if report.get('employee_id') != emp_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        supabase.table('daily_reports').delete().eq('id', report_id).execute()
        return jsonify({"success": True, "message": "Report deleted."}), 200
    except Exception as e:
        print("DELETE DAILY REPORT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/daily-reports/<int:report_id>/review', methods=['POST'])
@role_required(['admin', 'manager'])
def review_daily_report(report_id):
    try:
        farm_id = session.get('farm_id')
        user_id = session.get('user_id')
        data = request.json or {}
        feedback = (data.get('feedback') or '').strip()
        check = supabase.table('daily_reports').select('farm_id').eq('id', report_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Report not found"}), 404
        if farm_id and farm_id != 0 and check.data[0].get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        supabase.table('daily_reports').update({
            "reviewed_by": user_id,
            "reviewed_at": datetime.now().isoformat(),
            "feedback": feedback or None
        }).eq('id', report_id).execute()
        return jsonify({"success": True, "message": "Report marked as reviewed."}), 200
    except Exception as e:
        print("REVIEW DAILY REPORT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/daily-reports/<int:report_id>/attachment', methods=['GET'])
def get_report_attachment(report_id):
    if not is_logged_in():
        return jsonify({"success": False, "message": "Not logged in"}), 401
    try:
        farm_id = session.get('farm_id')
        role = session.get('role')
        check = supabase.table('daily_reports').select('*').eq('id', report_id).execute()
        if not check.data:
            return jsonify({"success": False, "message": "Report not found"}), 404
        report = check.data[0]
        if farm_id and farm_id != 0 and report.get('farm_id') != farm_id:
            return jsonify({"success": False, "message": "Access denied"}), 403
        if role not in ('admin', 'manager'):
            emp_id = _current_employee_id_for_reports(farm_id)
            if report.get('employee_id') != emp_id:
                return jsonify({"success": False, "message": "Access denied"}), 403
        signed = _sign_report_attachment(report.get('attachment_url'), report.get('attachment_filename'))
        if not signed:
            return jsonify({"success": False, "message": "No attachment"}), 404
        return jsonify({"success": True, "data": signed}), 200
    except Exception as e:
        print("GET REPORT ATTACHMENT ERROR:", str(e))
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/email/test-weekly-report', methods=['GET'])
@role_required(['admin'])
def email_test_weekly_report():
    if not mail_is_configured():
        return jsonify({"success": False, "message": "Email not configured."}), 400
    farm_id = session.get('farm_id')
    farm_info = get_farm_mail_info(farm_id)
    if not farm_info['owner_email']:
        return jsonify({"success": False, "message": "No owner email configured."}), 400
    try:
        digest = build_absentee_digest(farm_id)
        total = len(digest['flagged_14d']) + len(digest['never_attended']) + len(digest['already_flagged'])
        details = []
        if digest['already_flagged']:
            details.append(("⚠️ 30+ days absent", f"{len(digest['already_flagged'])}"))
        if digest['flagged_14d']:
            details.append(("🟡 14–29 days", f"{len(digest['flagged_14d'])}"))
        if digest['never_attended']:
            details.append(("🔴 Never attended", f"{len(digest['never_attended'])}"))
        send_email(farm_info['owner_email'],
            f"[TEST] Weekly attendance digest — {total} concern(s)",
            "Weekly Attendance Digest (TEST)",
            [f"[TEST] Manual test of weekly absentee digest.", f"Total concerns: {total}."],
            farm_name=farm_info['name'], details=details, button=login_button())
        return jsonify({"success": True,
                        "message": f"Test digest queued to {farm_info['owner_email']}. Concerns: {total}"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/test/employees', methods=['GET'])
def test_db():
    try:
        farm_id = session.get('farm_id')
        query = supabase.table('employees').select('*').limit(5)
        if farm_id and farm_id != 0:
            query = query.eq('farm_id', farm_id)
        response = query.execute()
        return jsonify({"success": True, "data": response.data, "farm_id": farm_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
def supabase_execute_with_retry(query, max_retries=3):
    """Retry Supabase queries on Windows socket errors."""
    for attempt in range(max_retries):
        try:
            return query.execute()
        except Exception as e:
            error_str = str(e)
            if 'WinError 10035' in error_str and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
# ==========================================
# JINJA TEMPLATE FILTERS
# ==========================================
@app.template_filter('format_time_ph')
def format_time_ph_filter(value):
    """Format a time_in/time_out value (string or datetime) into HH:MM AM/PM in Asia/Manila."""
    if not value:
        return '—'
    try:
        from datetime import datetime as _dt
        # If it's already a datetime object
        if isinstance(value, _dt):
            dt = value
        else:
            # Parse ISO string
            s = str(value).replace('Z', '+00:00')
            dt = _dt.fromisoformat(s)
        
        # Format in 12-hour style
        return dt.strftime('%I:%M %p').lstrip('0')
    except Exception as e:
        # Fallback: try to extract HH:MM from string
        try:
            s = str(value)
            if 'T' in s:
                time_part = s.split('T')[1][:5]
                return time_part
            if ' ' in s:
                time_part = s.split(' ')[1][:5]
                return time_part
        except Exception:
            pass
        return str(value)[:16]


# ==========================================
# RUN
# ==========================================
if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        scheduler = BackgroundScheduler(timezone="Asia/Manila")
        scheduler.add_job(auto_generate_payroll_job, 'cron', hour=1, minute=0)
        scheduler.add_job(auto_expire_interview_requests, 'cron', hour=2, minute=0)
        scheduler.add_job(auto_timeout_job, 'cron', hour=3, minute=0)
        scheduler.add_job(weekly_absentee_report_job, 'cron', day_of_week='mon', hour=8, minute=0)
        scheduler.start()
        print("⏰ Schedulers started:")
        print("   • Auto-generate payroll — daily 1:00 AM Asia/Manila")
        print("   • Auto-expire interviews — daily 2:00 AM Asia/Manila")
        print("   • Auto-timeout unclosed attendance — daily 3:00 AM Asia/Manila")
        print("   • Weekly absentee digest — Monday 8:00 AM Asia/Manila")
    app.run(debug=True, host='0.0.0.0', port=5000)