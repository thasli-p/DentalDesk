"""
The staff-facing clinic dashboard -- meant to be left open on a screen at
the clinic's own front desk (or checked from a phone), showing what Sia has
been doing on WhatsApp: a date-selectable schedule (defaults to today, but
any day can be picked to preview tomorrow or any other date), conversations
that need a human to step in, a feed of everyone who's messaged recently
(including people who never actually booked), and effectiveness insights
(booking conversion, busiest hours, most-requested services, estimated
pipeline value). Patient age/gender (already captured by Sia during
booking) are shown alongside name/phone wherever a patient is listed.

Two endpoints per clinic, both requiring auth (see require_dashboard_auth):
  - GET /clinic/{slug}/dashboard      -- the human-readable page
  - GET /clinic/{slug}/api/dashboard  -- the exact same data as plain JSON,
    so this can be pulled into the clinic's own systems (a practice
    management tool, a Google Sheet via a script, another internal
    dashboard) without scraping the HTML page.

The HTML page supports English and Arabic (?lang=en / ?lang=ar, a toggle
link is always on the page) -- only the page's own chrome (headings, table
headers, buttons, badges, empty states) is translated; patient names, phone
numbers, service names and Sia's flag reasons stay exactly as captured, and
the SVG chart internals stay LTR (only their surrounding headings are
translated). Arabic renders the whole page right-to-left.

Kept as plain server-rendered HTML with a page auto-refresh, same spirit as
admin.py -- no build step, no JS framework, nothing to break on an old
front-desk PC. Charts are plain inline SVG generated server-side (no chart
library, no client-side JS at all) -- hover tooltips come for free from
native SVG <title> elements. See claude/staff-dashboard.md for what's
covered and what isn't (no-show tracking, CSAT, etc. -- deliberately left
for later).
"""
import os
import math
import html
import hmac
import json
import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from shared import clinics_store, db, analytics, voice_usage
from app import handoff

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "ar")

PERIOD_CHOICES = [
    (1, "period_today"),
    (7, "period_7days"),
    (30, "period_30days"),
    (0, "period_alltime"),  # 0 here means "all time" -- see _days_param
]

# Logo -- served from the /static mount main.py already sets up (see
# main.py's STATIC_DIR / "static" mount, originally added for the
# brochure image). One shared logo works fine across every clinic's
# dashboard; it's Mawaid's own branding, not per-clinic.
LOGO_URL = "/static/logo.png"

# ---------------------------------------------------------------------
# Chart colors -- from the validated reference palette (see the dataviz
# skill's references/palette.md). Status colors for the outcome donut
# (booked/flagged/no-booking are states, not arbitrary categories); a
# single sequential blue for magnitude bars (volume by month, by hour,
# by service).
# ---------------------------------------------------------------------
CHART_BLUE = "#2a78d6"
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_MUTED = "#898781"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
BASELINE = "#c3c2b7"

# ---------------------------------------------------------------------
# Bilingual UI chrome. Only the page's own labels/headings/buttons/badges
# are translated here -- patient names, phone numbers, service names, flag
# reasons and the SVG chart internals stay exactly as captured/generated,
# in whichever language that was. lang defaults to "en"; ?lang=ar switches
# to Arabic with a right-to-left layout (see PAGE_STYLE's logical
# properties + the <html dir> attribute in the two render functions).
# ---------------------------------------------------------------------
TRANSLATIONS = {
    "dashboard_title": {"en": "Sia Dashboard", "ar": "لوحة تحكم سيا"},
    "updated": {"en": "updated", "ar": "آخر تحديث"},
    "refresh_note": {"en": "refreshes automatically every 30s", "ar": "يتم التحديث تلقائيًا كل 30 ثانية"},
    "period_today": {"en": "Today", "ar": "اليوم"},
    "period_7days": {"en": "7 days", "ar": "7 أيام"},
    "period_30days": {"en": "30 days", "ar": "30 يومًا"},
    "period_alltime": {"en": "All time", "ar": "كل الوقت"},
    "period_label_days": {"en": "Last {n} days", "ar": "آخر {n} يومًا"},
    "period_label_alltime": {"en": "All time", "ar": "كل الوقت"},
    "kpi_conversations": {"en": "Conversations", "ar": "المحادثات"},
    "kpi_bookings_made": {"en": "Bookings made", "ar": "الحجوزات المكتملة"},
    "kpi_conversion_rate": {"en": "Conversion rate", "ar": "معدل التحويل"},
    "kpi_new_patients": {"en": "New patients", "ar": "مرضى جدد"},
    "kpi_flagged": {"en": "Flagged for staff", "ar": "بحاجة لمتابعة الموظفين"},
    "kpi_cancelled": {"en": "Cancelled", "ar": "ملغاة"},
    "kpi_avg_messages": {"en": "Avg. messages/chat", "ar": "متوسط الرسائل/محادثة"},
    "kpi_est_revenue": {"en": "Est. booked value", "ar": "القيمة التقديرية للحجوزات"},
    "h2_outcomes": {"en": "Conversation outcomes", "ar": "نتائج المحادثات"},
    "h2_trend": {"en": "Conversations by month", "ar": "المحادثات حسب الشهر"},
    "h2_schedule": {"en": "Schedule", "ar": "الجدول"},
    "h2_needs_attention": {"en": "Needs attention", "ar": "بحاجة لمتابعة"},
    "h2_recent": {"en": "Recent conversations", "ar": "المحادثات الأخيرة"},
    "kpi_phone_calls": {"en": "📞 Phone calls", "ar": "📞 المكالمات"},
    "kpi_phone_minutes": {"en": "📞 Call minutes", "ar": "📞 دقائق المكالمات"},
    "kpi_voice_month": {"en": "📞 Voice minutes this month", "ar": "📞 دقائق الاتصال هذا الشهر"},
    "tab_all": {"en": "All", "ar": "الكل"},
    "tab_whatsapp": {"en": "💬 WhatsApp", "ar": "💬 واتساب"},
    "tab_calls": {"en": "📞 Phone calls", "ar": "📞 المكالمات"},
    "col_msgs_or_duration": {"en": "Messages / call length", "ar": "الرسائل / مدة المكالمة"},
    "h2_services": {"en": "Most requested services", "ar": "الخدمات الأكثر طلبًا"},
    "h2_doctors": {"en": "Bookings by doctor", "ar": "الحجوزات حسب الطبيب"},
    "h2_specialties": {"en": "Bookings by specialty", "ar": "الحجوزات حسب التخصص"},
    "doctors_empty": {
        "en": "No bookings yet to break down by doctor.",
        "ar": "لا توجد حجوزات بعد لتصنيفها حسب الطبيب.",
    },
    "specialties_empty": {
        "en": "No bookings yet to break down by specialty.",
        "ar": "لا توجد حجوزات بعد لتصنيفها حسب التخصص.",
    },
    "h2_hourly": {"en": "Message volume by hour of day", "ar": "حجم الرسائل حسب ساعة اليوم"},
    "outcome_booked": {"en": "Booked", "ar": "تم الحجز"},
    "outcome_flagged": {"en": "Flagged for staff", "ar": "بحاجة لمتابعة الموظفين"},
    "outcome_no_booking": {"en": "No booking", "ar": "بدون حجز"},
    "schedule_today": {"en": "Today", "ar": "اليوم"},
    "schedule_tomorrow": {"en": "Tomorrow", "ar": "غدًا"},
    "schedule_go": {"en": "Go", "ar": "عرض"},
    "schedule_date_label": {"en": "Date", "ar": "التاريخ"},
    "schedule_empty": {"en": "No appointments scheduled for {date}.", "ar": "لا توجد مواعيد بتاريخ {date}."},
    "col_time": {"en": "Time", "ar": "الوقت"},
    "col_patient": {"en": "Patient", "ar": "المريض"},
    "col_dentist": {"en": "Dentist", "ar": "الطبيب"},
    "col_service": {"en": "Service", "ar": "الخدمة"},
    "col_status": {"en": "Status", "ar": "الحالة"},
    "col_reminder": {"en": "Reminder", "ar": "التذكير"},
    "reminder_sent": {"en": "Sent", "ar": "أُرسل"},
    "reminder_failed": {"en": "Failed", "ar": "فشل"},
    "reminder_skipped": {"en": "Not needed", "ar": "غير مطلوب"},
    "col_age": {"en": "Age", "ar": "العمر"},
    "col_gender": {"en": "Gender", "ar": "الجنس"},
    "col_started": {"en": "Started", "ar": "بدأت"},
    "col_whatsapp": {"en": "Mobile", "ar": "الجوال"},
    "col_messages": {"en": "Messages", "ar": "الرسائل"},
    "col_outcome": {"en": "Outcome", "ar": "النتيجة"},
    "search_placeholder": {"en": "Search patient name…", "ar": "ابحث باسم المريض…"},
    "filter_from": {"en": "From", "ar": "من"},
    "filter_to": {"en": "To", "ar": "إلى"},
    "filter_button": {"en": "Filter", "ar": "تصفية"},
    "filter_clear": {"en": "Clear filters", "ar": "مسح التصفية"},
    "filter_hint": {
        "en": "Filters \"Needs attention\" and \"Recent conversations\" below. Set From and To to the same date for a single day. A name search with no date searches this clinic's whole history, not just the period buttons above.",
        "ar": "يُصفّي هذا \"بحاجة لمتابعة\" و\"المحادثات الأخيرة\" أدناه. اجعل \"من\" و\"إلى\" بنفس التاريخ ليوم واحد. البحث بالاسم بدون تاريخ يبحث في كامل سجل هذه العيادة، وليس فقط ضمن الفترة المحددة أعلاه.",
    },
    "no_number": {"en": "no number", "ar": "بدون رقم"},
    "view_conversation": {"en": "Open chat", "ar": "فتح المحادثة"},
    "view": {"en": "View", "ar": "عرض"},
    "attention_empty_filtered": {"en": "Nothing flagged matches this filter.", "ar": "لا يوجد ما يطابق هذا التصفية."},
    "attention_empty": {"en": "Nothing flagged right now.", "ar": "لا يوجد ما يحتاج للمتابعة حاليًا."},
    "recent_empty_filtered": {"en": "No conversations match this filter.", "ar": "لا توجد محادثات مطابقة لهذا التصفية."},
    "recent_empty": {"en": "No conversations in this period.", "ar": "لا توجد محادثات في هذه الفترة."},
    "truncated_note": {
        "en": "Showing the most recent {n} matches — narrow the filter (add a name, or a tighter date range) to see more precisely.",
        "ar": "يعرض أحدث {n} نتيجة — ضيّق نطاق البحث (بإضافة اسم أو تحديد فترة أضيق) لرؤية نتائج أدق.",
    },
    "services_empty": {
        "en": "No service breakdown yet — this fills in as bookings come in with a service specified.",
        "ar": "لا يوجد تفصيل للخدمات بعد — سيظهر هذا تدريجيًا مع ورود حجوزات محددة الخدمة.",
    },
    "api_hint": {
        "en": "Want this data in your own system? The same numbers are available as JSON at",
        "ar": "تريد هذه البيانات في نظامك الخاص؟ نفس الأرقام متوفرة بصيغة JSON على",
    },
    "api_hint_suffix": {"en": "(same login)", "ar": "(نفس بيانات الدخول)"},
    "lang_toggle": {"en": "العربية", "ar": "English"},
    "back_to_dashboard": {"en": "Back to dashboard", "ar": "العودة إلى لوحة التحكم"},
    "booked_during": {"en": "Booked during this conversation", "ar": "تم الحجز خلال هذه المحادثة"},
    "no_booking_made": {"en": "No booking made", "ar": "لم يتم أي حجز"},
    "no_reason_given": {"en": "No reason given.", "ar": "لم يُذكر سبب."},
    "status_label": {"en": "status", "ar": "الحالة"},
    "started_label": {"en": "started", "ar": "بدأت في"},
    "no_messages": {"en": "No messages in this conversation.", "ar": "لا توجد رسائل في هذه المحادثة."},
    "badge_booked": {"en": "booked", "ar": "تم الحجز"},
    "voice_agent_name": {"en": "AI receptionist (phone)", "ar": "موظف الاستقبال الآلي (هاتف)"},
    "badge_flagged": {"en": "flagged", "ar": "بحاجة لمتابعة"},
    "badge_no_booking": {"en": "no booking", "ar": "بدون حجز"},
    "unknown_patient": {"en": "Unknown", "ar": "غير معروف"},
    "unknown_patient_title": {"en": "Unknown patient", "ar": "مريض غير معروف"},
    # Shared inbox (app/handoff.py)
    "staff_name": {"en": "Clinic staff", "ar": "موظف العيادة"},
    "badge_staff": {"en": "staff replying", "ar": "يرد الموظف"},
    "inbox_sia_active": {"en": "Sia is answering this chat.", "ar": "سيا ترد على هذه المحادثة."},
    "inbox_staff_active": {
        "en": "Staff are handling this chat — Sia is paused until {time}, then takes over again automatically.",
        "ar": "الموظفون يتولّون هذه المحادثة — سيا متوقفة حتى {time} ثم تعود للرد تلقائيًا.",
    },
    "inbox_take_over": {"en": "Take over", "ar": "تولَّ المحادثة"},
    "inbox_hand_back": {"en": "Return to Sia", "ar": "أعِد المحادثة لسيا"},
    "inbox_reply_placeholder": {
        "en": "Type a reply to the patient… (sending it pauses Sia for this chat)",
        "ar": "اكتب ردًا للمريض… (الإرسال يوقف سيا في هذه المحادثة)",
    },
    "inbox_send": {"en": "Send on WhatsApp", "ar": "إرسال عبر واتساب"},
    "inbox_window_left": {
        "en": "You can reply for another {left} (WhatsApp allows replies up to 24 hours after the patient's last message).",
        "ar": "يمكنك الرد خلال {left} المتبقية (يسمح واتساب بالرد خلال 24 ساعة من آخر رسالة من المريض).",
    },
    "inbox_window_closed": {
        "en": "More than 24 hours since the patient last wrote, so WhatsApp won't deliver a normal reply. Call the patient, or wait for them to message again.",
        "ar": "مرّ أكثر من 24 ساعة على آخر رسالة من المريض، لذلك لن يوصل واتساب ردًا عاديًا. اتصل بالمريض أو انتظر حتى يراسل مجددًا.",
    },
    "inbox_not_latest": {
        "en": "This patient has a newer conversation — reply there.",
        "ar": "لدى هذا المريض محادثة أحدث — قم بالرد هناك.",
    },
    "inbox_open_latest": {"en": "Open the latest conversation", "ar": "افتح أحدث محادثة"},
    "inbox_phone_note": {
        "en": "This was a phone call — replies can only be sent in a WhatsApp chat.",
        "ar": "هذه مكالمة هاتفية — لا يمكن الرد إلا في محادثة واتساب.",
    },
    "inbox_ok_sent": {"en": "Sent. Sia is paused for this chat.", "ar": "تم الإرسال. سيا متوقفة في هذه المحادثة."},
    "inbox_ok_taken": {"en": "You're handling this chat. Sia is paused.", "ar": "أنت تتولى هذه المحادثة الآن. سيا متوقفة."},
    "inbox_ok_returned": {"en": "Sia will answer this patient's next message.", "ar": "سترد سيا على الرسالة القادمة من هذا المريض."},
    "inbox_err_empty": {"en": "Type a message first.", "ar": "اكتب رسالة أولًا."},
    "inbox_err_too_long": {"en": "That message is too long for WhatsApp.", "ar": "الرسالة أطول من المسموح في واتساب."},
    "inbox_err_window_closed": {
        "en": "Not sent: more than 24 hours since the patient's last message.",
        "ar": "لم تُرسل: مرّ أكثر من 24 ساعة على آخر رسالة من المريض.",
    },
    "inbox_err_not_latest": {"en": "Not sent: reply in the patient's latest conversation.", "ar": "لم تُرسل: قم بالرد في أحدث محادثة للمريض."},
    "inbox_err_not_whatsapp": {"en": "Not sent: this isn't a WhatsApp chat.", "ar": "لم تُرسل: هذه ليست محادثة واتساب."},
    "inbox_err_send_failed": {
        "en": "WhatsApp didn't accept the message. Try again; if it keeps failing, check the clinic's WhatsApp connection.",
        "ar": "لم يقبل واتساب الرسالة. حاول مجددًا، وإذا تكرر الخطأ تحقق من ربط واتساب الخاص بالعيادة.",
    },
    "inbox_err_expired": {"en": "The page was out of date — please try again.", "ar": "انتهت صلاحية الصفحة — حاول مرة أخرى."},
    "hours_short": {"en": "{h} h {m} min", "ar": "{h} س {m} د"},
    # Sound alerts
    "sound_on": {"en": "🔔 Sound on", "ar": "🔔 الصوت مفعّل"},
    "sound_off": {"en": "🔕 Sound off", "ar": "🔕 الصوت متوقف"},
    "sound_blocked": {"en": "🔔 Click to allow sound", "ar": "🔔 اضغط للسماح بالصوت"},
    "alert_flag": {"en": "Needs attention: {name}", "ar": "بحاجة لمتابعة: {name}"},
    "alert_msg": {"en": "New message from {name}", "ar": "رسالة جديدة من {name}"},
    "alert_open": {"en": "Open", "ar": "فتح"},
    "alert_dismiss": {"en": "Dismiss", "ar": "إخفاء"},
}

# Small, fixed vocabulary (not free text like a name), so it's translated
# like any other UI label rather than left as raw stored data.
GENDER_LABELS = {
    "male": {"en": "Male", "ar": "ذكر"},
    "female": {"en": "Female", "ar": "أنثى"},
}


def _t(key: str, lang: str) -> str:
    entry = TRANSLATIONS.get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get(DEFAULT_LANG) or key


def _fmt_gender(value: Optional[str], lang: str) -> str:
    if not value:
        return "—"
    entry = GENDER_LABELS.get(str(value).strip().lower())
    if entry:
        return entry.get(lang) or entry.get(DEFAULT_LANG)
    return html.escape(str(value))


def _fmt_reminder(a: dict, lang: str) -> str:
    """Same-day reminder state for a schedule row (see app/reminders.py).
    Only counts if it was handled for the appointment's CURRENT time."""
    status = a.get("reminder_status") or ""
    if not status or a.get("reminder_sent_for") != a.get("appointment_time"):
        return "—"
    if status.startswith("sent"):
        return "🔔 " + _t("reminder_sent", lang)
    if status.startswith("failed"):
        return f'<span class="badge badge-cancelled" title="{html.escape(status)}">{_t("reminder_failed", lang)}</span>'
    return f'<span title="{html.escape(status)}">{_t("reminder_skipped", lang)}</span>'


def _fmt_age(value) -> str:
    return str(value) if value is not None else "—"


def _fmt_date_human(date_str: str) -> str:
    """'2026-09-19' -> 'Sep 19, 2026'; falls back to the raw string on a
    malformed value rather than raising. Kept LTR/English month
    abbreviations even in Arabic mode -- like the rest of the raw data on
    this page, this is a value, not UI chrome."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str or ""
    return d.strftime("%b %d, %Y")


def _period_label(days_param: int, lang: str) -> str:
    if days_param and days_param > 0:
        return _t("period_label_days", lang).format(n=days_param)
    return _t("period_label_alltime", lang)


def _days_param(days: int) -> Optional[int]:
    return None if days <= 0 else days


def require_dashboard_auth(clinic: dict, credentials: HTTPBasicCredentials):
    """Accepts EITHER the site-wide admin login (so whoever manages
    /admin/clinics never has to remember every clinic's own password), OR
    that specific clinic's own dashboard_password with its slug as the
    username (what you actually hand to that clinic's front desk)."""
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_password and _secrets.compare_digest(credentials.username, admin_username) \
            and _secrets.compare_digest(credentials.password, admin_password):
        return

    clinic_password = clinic.get("dashboard_password") or clinics_store.ensure_dashboard_password(clinic["id"]) or ""
    if clinic_password and _secrets.compare_digest(credentials.username, clinic["slug"]) \
            and _secrets.compare_digest(credentials.password, clinic_password):
        return

    raise HTTPException(
        status_code=401,
        detail="Incorrect dashboard username or password",
        headers={"WWW-Authenticate": "Basic"},
    )


def _get_clinic_or_404(slug: str) -> dict:
    clinic = clinics_store.get_clinic_by_slug(slug)
    if not clinic:
        raise HTTPException(status_code=404, detail=f"No clinic with slug '{slug}'")
    return clinic


PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  .header-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  .brand-row { display: flex; align-items: center; gap: 16px; margin-bottom: 6px; }
  .brand-row img { width: 128px; height: 128px; border-radius: 24px; flex: none; }
  .lang-toggle { font-size: 13px; color: #1a7f37; text-decoration: none; border: 1px solid #1a7f37; border-radius: 14px; padding: 4px 14px; white-space: nowrap; }
  h1 { font-size: 22px; margin: 0; }
  .subtitle { color: #666; font-size: 13px; margin-bottom: 18px; }
  h2 { font-size: 16px; margin-top: 30px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
  .periods { margin: 10px 0 4px 0; }
  .periods a { display: inline-block; padding: 6px 14px; margin-inline-end: 6px; border-radius: 16px; background: #f0f0f0; color: #333; text-decoration: none; font-size: 13px; }
  .periods a.active { background: #1a7f37; color: white; }
  .kpi-grid { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
  .kpi-card { flex: 1 1 150px; background: #f7f7f7; border-radius: 8px; padding: 14px 16px; }
  .kpi-value { font-size: 26px; font-weight: 700; }
  .kpi-label { font-size: 12px; color: #666; margin-top: 2px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { text-align: start; padding: 8px; border-bottom: 1px solid #eee; font-size: 13px; }
  th { background: #f7f7f7; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-yes { background: #e6ffed; color: #1a7f37; }
  .badge-no { background: #f0f0f0; color: #999; }
  .badge-flag { background: #fff3cd; color: #8a6300; }
  .badge-cancelled { background: #ffeef0; color: #cf222e; }
  .badge-staff { background: #e8f1fd; color: #0b3a75; }
  .empty { color: #999; font-size: 13px; padding: 10px 0; }
  .bar-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 2px 0; }
  .bar-hour { width: 34px; color: #666; text-align: end; }
  .bar-track { flex: 1; background: #f0f0f0; border-radius: 3px; height: 12px; overflow: hidden; }
  .bar-fill { background: #1a7f37; height: 100%; }
  .bar-count { width: 24px; color: #666; }
  .attention-item { background: #fff8e6; border: 1px solid #f0d78c; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
  .attention-reason { font-size: 13px; margin-top: 4px; }
  .api-hint { color: #666; font-size: 12px; margin-top: 30px; padding-top: 10px; border-top: 1px solid #eee; }
  .api-hint code { background: #f0f0f0; padding: 2px 5px; border-radius: 3px; }
  .back-link { display: inline-block; margin-bottom: 10px; font-size: 13px; color: #1a7f37; text-decoration: none; }
  .conv-meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  .transcript { margin-top: 10px; }
  .bubble-row { display: flex; margin-bottom: 10px; }
  .bubble-row.patient { justify-content: flex-start; }
  .bubble-row.sia { justify-content: flex-end; }
  .bubble { max-width: 65%; padding: 8px 12px; border-radius: 12px; font-size: 14px; line-height: 1.4; white-space: pre-wrap; }
  .bubble-row.patient .bubble { background: #f0f0f0; color: #1a1a1a; }
  .bubble-row.sia .bubble { background: #dcf3e3; color: #14532d; }
  .bubble-row.staff { justify-content: flex-end; }
  .bubble-row.staff .bubble { background: #dbe9fb; color: #0b3a75; }
  .bubble-time { font-size: 10px; color: #999; margin-top: 3px; }
  .inbox-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; border-radius: 8px; padding: 10px 14px; margin: 10px 0 14px 0; font-size: 13px; }
  .inbox-bar.sia { background: #e6ffed; border: 1px solid #b7e4c4; color: #14532d; }
  .inbox-bar.staff { background: #e8f1fd; border: 1px solid #b5cff3; color: #0b3a75; }
  .inbox-bar form { margin: 0; }
  .inbox-bar button { padding: 6px 14px; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; background: #fff; border: 1px solid currentColor; color: inherit; }
  .reply-box { position: sticky; bottom: 0; background: #fff; border-top: 1px solid #ddd; padding: 12px 0 16px 0; margin-top: 16px; }
  .reply-box textarea { width: 100%; box-sizing: border-box; min-height: 70px; padding: 10px; border: 1px solid #ccc; border-radius: 8px; font: inherit; font-size: 14px; resize: vertical; }
  .reply-box .reply-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 8px; flex-wrap: wrap; }
  .reply-box .window-note { font-size: 12px; color: #666; }
  .reply-box button { padding: 8px 18px; border: none; border-radius: 6px; background: #1a7f37; color: white; font-size: 14px; cursor: pointer; }
  .reply-box .reply-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .reply-box button.handback-btn { background: #fff; color: #0b3a75; border: 1px solid #0b3a75; }
  .reply-closed { background: #f7f7f7; border-radius: 8px; padding: 12px 14px; margin-top: 16px; font-size: 13px; color: #555; }
  .sound-toggle { position: fixed; bottom: 16px; inset-inline-start: 16px; z-index: 50; padding: 7px 14px; border-radius: 18px; border: 1px solid #ccc; background: #fff; font-size: 13px; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
  .sound-toggle.blocked { border-color: #f0d78c; background: #fff8e6; }
  #alerts { position: fixed; bottom: 16px; inset-inline-end: 16px; z-index: 60; display: flex; flex-direction: column; gap: 8px; max-width: 340px; }
  .alert-toast { background: #fff; border: 1px solid #f0d78c; border-inline-start: 5px solid #fab219; border-radius: 8px; padding: 10px 12px; font-size: 13px; box-shadow: 0 3px 12px rgba(0,0,0,.15); display: flex; gap: 10px; align-items: center; justify-content: space-between; }
  .alert-toast.msg { border-color: #b5cff3; border-inline-start-color: #2a78d6; }
  .alert-toast a { color: #1a7f37; font-weight: 600; text-decoration: none; white-space: nowrap; }
  .alert-toast button { border: none; background: none; color: #999; cursor: pointer; font-size: 16px; }
  .flash { border-radius: 6px; padding: 8px 12px; margin: 8px 0; font-size: 13px; }
  .flash.ok { background: #e6ffed; color: #1a7f37; }
  .flash.err { background: #ffeef0; color: #cf222e; }
  .view-link { font-size: 12px; }
  .chart-wrap { margin: 10px 0 20px 0; direction: ltr; }
  .donut-wrap { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; margin: 10px 0 20px 0; }
  .donut-wrap svg { direction: ltr; }
  .legend { display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
  .legend-item { display: flex; align-items: center; gap: 8px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
  .schedule-picker { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 12px 0; }
  .schedule-picker a { padding: 5px 12px; border-radius: 14px; background: #f0f0f0; color: #333; text-decoration: none; font-size: 12px; }
  .schedule-picker a.active { background: #1a7f37; color: white; }
  .schedule-picker form { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #666; }
  .schedule-picker input[type=date] { padding: 4px 8px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }
  .schedule-picker button { padding: 5px 12px; border: none; border-radius: 6px; background: #1a7f37; color: white; font-size: 12px; cursor: pointer; }
  .filter-bar { background: #f7f7f7; border-radius: 8px; padding: 12px 14px; margin: 16px 0 6px 0; }
  .filter-bar form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .filter-bar input[type=text] { padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; min-width: 180px; }
  .filter-bar input[type=date] { padding: 5px 8px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }
  .filter-bar .date-field { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #666; }
  .filter-bar button { padding: 6px 14px; border: none; border-radius: 6px; background: #1a7f37; color: white; font-size: 13px; cursor: pointer; }
  .filter-clear { font-size: 12px; color: #666; text-decoration: none; margin-inline-start: 4px; }
  .filter-hint { font-size: 11px; color: #999; margin-top: 6px; }
  .truncated-note { font-size: 12px; color: #999; margin-top: 6px; }
</style>
"""


def _fmt_dt(value: Optional[str]) -> str:
    if not value:
        return "—"
    # Values are stored as ISO strings like "2026-09-18T14:05:00"; just trim
    # to something readable without pulling in a date-formatting dependency.
    return value.replace("T", " ")[:16]


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} SAR"


def _extra_qs(name_filter: str = "", date_from: str = "", date_to: str = "", sched: str = "", lang: str = DEFAULT_LANG) -> str:
    """&name=...&from=...&to=...&sched=...&lang=... fragment (possibly
    empty) for chaining onto other query strings, so links (period switch,
    conversation view, back button, schedule quick-links) don't silently
    drop whichever filters/options are currently in effect.

    sched is deliberately the RAW query value the current page was loaded
    with (empty when nothing was picked), never the resolved
    default-to-today date -- baking today's literal date into every link
    would leave this page (meant to be left open and auto-refreshing all
    day) stuck showing today's schedule forever once midnight passes.
    Only the explicit "Tomorrow" link / date picker set sched to a real
    value; "Today" clears it back to empty so it keeps tracking today."""
    parts = []
    if name_filter:
        parts.append(f"name={quote(name_filter)}")
    if date_from:
        parts.append(f"from={quote(date_from)}")
    if date_to:
        parts.append(f"to={quote(date_to)}")
    if sched:
        parts.append(f"sched={quote(sched)}")
    if lang and lang != DEFAULT_LANG:
        parts.append(f"lang={quote(lang)}")
    return ("&" + "&".join(parts)) if parts else ""


def _svg_bar_chart(
    items, *, value_key: str, label_key: str, width: int = 760, height: int = 180,
    bar_color: str = CHART_BLUE, value_fmt=None, show_values: bool = True, label_every: int = 1,
) -> str:
    """Vertical bar chart: <=24px bars, 4px rounded data-ends, single
    baseline, sparing direct labels (per marks-and-anatomy.md). Native
    <title> on each bar gives a hover tooltip with no JS."""
    if not items:
        return '<div class="empty">Not enough data yet.</div>'
    value_fmt = value_fmt or (lambda v: str(v))
    n = len(items)
    max_val = max((i[value_key] for i in items), default=0) or 1
    pad_left, pad_right, pad_top, pad_bottom = 8, 8, 22, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    slot_w = plot_w / n
    bar_w = min(24.0, slot_w * 0.55)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMinYMin meet" role="img" aria-label="bar chart">'
    ]
    baseline_y = pad_top + plot_h
    for idx, item in enumerate(items):
        v = item[value_key]
        bar_h = (v / max_val) * plot_h if max_val else 0
        x = pad_left + idx * slot_w + (slot_w - bar_w) / 2
        y = baseline_y - bar_h
        r = min(4.0, bar_h / 2) if bar_h > 0 else 0
        label = html.escape(str(item[label_key]))
        title = f"{label}: {html.escape(value_fmt(v))}"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 1):.1f}" '
            f'rx="{r:.1f}" ry="{r:.1f}" fill="{bar_color}"><title>{title}</title></rect>'
        )
        if show_values and v:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{INK_SECONDARY}">{html.escape(value_fmt(v))}</text>'
            )
        if idx % label_every == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{INK_MUTED}">{label}</text>'
            )
    parts.append(
        f'<line x1="{pad_left}" y1="{baseline_y:.1f}" x2="{width - pad_right}" y2="{baseline_y:.1f}" '
        f'stroke="{BASELINE}" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return f'<div class="chart-wrap">{"".join(parts)}</div>'


def _svg_hbar_chart(
    items, *, value_key: str, label_key: str, width: int = 700, bar_h: int = 18, gap: int = 12,
    bar_color: str = CHART_BLUE, value_fmt=None, label_w: int = 150,
) -> str:
    """Horizontal bar chart -- for magnitude comparisons with longer text
    labels (service names) that would collide as vertical-bar x-labels.
    label_w is overridable per call -- a doctor-name-plus-specialty label
    ("Dr. Fatima Al-Zahrani (Orthodontics)") needs more room than a bare
    service name before it starts clipping."""
    if not items:
        return '<div class="empty">Not enough data yet.</div>'
    value_fmt = value_fmt or (lambda v: str(v))
    max_val = max((i[value_key] for i in items), default=0) or 1
    right_pad = 48
    plot_w = max(width - label_w - right_pad, 40)
    height = len(items) * (bar_h + gap) + gap

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMinYMin meet" role="img" aria-label="bar chart">'
    ]
    y = gap
    for item in items:
        v = item[value_key]
        w = (v / max_val) * plot_w if max_val else 0
        r = min(4.0, bar_h / 2)
        label = html.escape(str(item[label_key]))
        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="{INK_SECONDARY}">{label}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{max(w, 2):.1f}" height="{bar_h}" '
            f'rx="{r:.1f}" ry="{r:.1f}" fill="{bar_color}">'
            f'<title>{label}: {html.escape(value_fmt(v))}</title></rect>'
        )
        parts.append(
            f'<text x="{label_w + w + 8:.1f}" y="{y + bar_h / 2 + 4:.1f}" '
            f'font-size="11" fill="{INK_SECONDARY}">{html.escape(value_fmt(v))}</text>'
        )
        y += bar_h + gap
    parts.append("</svg>")
    return f'<div class="chart-wrap">{"".join(parts)}</div>'


def _svg_donut_chart(slices, *, size: int = 160, thickness: int = 26) -> str:
    """slices: list of (label, value, color) tuples. A status color never
    carries meaning alone -- each slice gets a legend row with a dot + the
    label + the count + the percentage (the 'icon + label' pairing the
    palette's status colors require)."""
    total = sum(v for _, v, _ in slices if v)
    if not total:
        return '<div class="empty">Not enough data yet.</div>'

    cx = cy = size / 2
    r = (size - thickness) / 2
    circumference = 2 * math.pi * r
    offset = 0.0
    arcs = []
    legend_items = []
    for label, value, color in slices:
        if not value:
            continue
        frac = value / total
        dash = circumference * frac
        arcs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" stroke="{color}" stroke-width="{thickness}" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})"><title>{html.escape(label)}: {value} ({frac * 100:.0f}%)</title></circle>'
        )
        offset += dash
        legend_items.append(
            f'<div class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
            f'{html.escape(label)} — <b>{value}</b> ({frac * 100:.0f}%)</div>'
        )

    svg = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="donut chart">'
        + "".join(arcs)
        + f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="22" font-weight="700" fill="{INK_PRIMARY}">{total}</text>'
        + f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="10" fill="{INK_MUTED}">conversations</text>'
        + "</svg>"
    )
    return f'<div class="donut-wrap"><div>{svg}</div><div class="legend">{"".join(legend_items)}</div></div>'


def _fmt_duration(secs) -> str:
    if secs is None:
        return ""
    secs = max(0, int(secs))
    return f"{secs // 60}m {secs % 60:02d}s"


def _alerts_widget(clinic: dict, lang: str, soft_refresh: bool = False) -> str:
    """Sound + pop-up alerts, on every dashboard page. Polls /api/alerts
    every 10 s and chimes when (a) Sia flags a chat for staff, or (b) a
    patient writes in a chat staff have taken over (Sia is paused there,
    so nobody else will answer). Sia's normal chats never chime.

    Browsers only allow sound after someone has clicked the page, and a
    full page reload takes that permission away again -- so the main
    dashboard (soft_refresh=True) refreshes its contents in place every
    30 s instead of reloading, which keeps the sound working all day.
    What has already been alerted is remembered per browser tab, so moving
    between pages doesn't chime again for the same thing."""
    labels = {k: _t(k, lang) for k in (
        "sound_on", "sound_off", "sound_blocked", "alert_flag", "alert_msg", "alert_open", "alert_dismiss")}
    cfg = {
        "slug": clinic["slug"],
        "alertsUrl": f'/clinic/{clinic["slug"]}/api/alerts',
        "convBase": f'/clinic/{clinic["slug"]}/dashboard/conversation/',
        "lang": lang,
        "softRefresh": soft_refresh,
        "t": labels,
    }
    return f"""
<button type="button" class="sound-toggle" id="sound-toggle">{labels['sound_on']}</button>
<div id="alerts"></div>
<script>
(function () {{
  var C = {json.dumps(cfg, ensure_ascii=False)};
  var PREF = "mawaid_sound", SEEN = "mawaid_seen_" + C.slug;
  var ctx = null, baseTitle = document.title;
  function soundOn() {{ try {{ return localStorage.getItem(PREF) !== "off"; }} catch (e) {{ return true; }} }}
  function getCtx() {{
    if (!ctx) {{ var A = window.AudioContext || window.webkitAudioContext; if (A) ctx = new A(); }}
    return ctx;
  }}
  function label() {{
    var b = document.getElementById("sound-toggle"); if (!b) return;
    var blocked = soundOn() && (!getCtx() || ctx.state !== "running");
    b.textContent = !soundOn() ? C.t.sound_off : (blocked ? C.t.sound_blocked : C.t.sound_on);
    b.className = "sound-toggle" + (blocked ? " blocked" : "");
  }}
  function unlock() {{ var c = getCtx(); if (c && c.state !== "running") c.resume().then(label, label); else label(); }}
  document.addEventListener("click", unlock, true);
  document.addEventListener("keydown", unlock, true);
  document.getElementById("sound-toggle").addEventListener("click", function (e) {{
    e.stopPropagation(); unlock();
    try {{ localStorage.setItem(PREF, soundOn() ? "off" : "on"); }} catch (err) {{}}
    label(); if (soundOn()) chime();
  }});
  function tone(c, f, start, dur) {{
    var o = c.createOscillator(), g = c.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, start);
    g.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    o.connect(g); g.connect(c.destination); o.start(start); o.stop(start + dur + 0.05);
  }}
  function chime() {{
    if (!soundOn()) return;
    var c = getCtx(); if (!c || c.state !== "running") return;
    var t = c.currentTime; tone(c, 880, t, 0.35); tone(c, 1318.5, t + 0.18, 0.5);
  }}
  function esc(s) {{ return String(s || "").replace(/[&<>"]/g, function (ch) {{ return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[ch]; }}); }}
  function toast(kind, text, convId) {{
    var box = document.getElementById("alerts");
    var el = document.createElement("div");
    el.className = "alert-toast " + kind;
    el.innerHTML = "<span>" + esc(text) + "</span><span><a href='" + C.convBase + convId + "?lang=" + C.lang + "'>" +
      esc(C.t.alert_open) + "</a> <button type='button' title='" + esc(C.t.alert_dismiss) + "'>×</button></span>";
    el.querySelector("button").onclick = function () {{ el.remove(); }};
    box.prepend(el);
    while (box.children.length > 4) box.lastChild.remove();
    document.title = "🔔 " + baseTitle;
  }}
  window.addEventListener("focus", function () {{ document.title = baseTitle; }});
  function loadSeen() {{ try {{ return JSON.parse(sessionStorage.getItem(SEEN)); }} catch (e) {{ return null; }} }}
  function saveSeen(v) {{ try {{ sessionStorage.setItem(SEEN, JSON.stringify(v)); }} catch (e) {{}} }}
  function poll() {{
    fetch(C.alertsUrl, {{credentials: "same-origin", cache: "no-store"}})
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (d) {{
        if (!d) return;
        var seen = loadSeen(), first = !seen;
        seen = seen || {{flags: {{}}, msg: 0}};
        var ring = false;
        d.flags.forEach(function (f) {{
          var key = String(f.conversation_id);
          if (seen.flags[key] !== f.sig) {{
            if (!first) {{ toast("flag", C.t.alert_flag.replace("{{name}}", f.name), f.conversation_id); ring = true; }}
            seen.flags[key] = f.sig;
          }}
        }});
        d.messages.forEach(function (m) {{
          if (m.id > seen.msg && !first) {{ toast("msg", C.t.alert_msg.replace("{{name}}", m.name), m.conversation_id); ring = true; }}
        }});
        if (d.max_message_id > seen.msg) seen.msg = d.max_message_id;
        saveSeen(seen);
        if (ring) chime();
      }})
      .catch(function () {{}});
  }}
  label(); poll(); setInterval(poll, 10000);
  if (C.softRefresh) {{
    setInterval(function () {{
      var a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
      fetch(location.href, {{credentials: "same-origin", cache: "no-store"}})
        .then(function (r) {{ return r.ok ? r.text() : null; }})
        .then(function (h) {{
          if (!h) return;
          var doc = new DOMParser().parseFromString(h, "text/html");
          var fresh = doc.getElementById("page"), cur = document.getElementById("page");
          if (fresh && cur) cur.innerHTML = fresh.innerHTML;
        }})
        .catch(function () {{}});
    }}, 30000);
  }}
}})();
</script>"""


@router.get("/clinic/{slug}/api/alerts")
async def clinic_alerts(slug: str, credentials: HTTPBasicCredentials = Depends(security)):
    """What the sound alerts poll: chats Sia flagged in the last 2 days, and
    patient messages from the last day in chats staff have taken over.
    Each flag has a 'sig' that changes when Sia adds another reason, so a
    second flag on the same chat alerts again."""
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)
    db.set_current_db_path(clinic["db_path"])
    now = datetime.now()
    flags, messages, max_id = [], [], 0
    with db.db() as conn:
        for r in conn.execute(
            """
            SELECT c.id, LENGTH(COALESCE(c.flag_reason, '')) AS len, p.name
            FROM conversations c LEFT JOIN patients p ON p.id = c.patient_id
            WHERE c.flagged_for_staff = 1 AND c.started_at >= ?
            ORDER BY c.id DESC LIMIT 30
            """,
            ((now - timedelta(days=2)).isoformat(),),
        ).fetchall():
            flags.append({"conversation_id": r["id"], "sig": r["len"], "name": r["name"] or _t("unknown_patient", DEFAULT_LANG)})
        row = conn.execute("SELECT MAX(id) AS m FROM messages").fetchone()
        max_id = (row["m"] if row else 0) or 0
        try:
            rows = conn.execute(
                """
                SELECT m.id, m.conversation_id, p.name
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                JOIN patients p ON p.id = c.patient_id
                WHERE m.sender = 'user' AND p.ai_paused_until > ? AND m.created_at >= ?
                ORDER BY m.id DESC LIMIT 30
                """,
                (now.isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat()),
            ).fetchall()
        except Exception:  # database not migrated yet (no ai_paused_until column)
            rows = []
        for r in rows:
            messages.append({"id": r["id"], "conversation_id": r["conversation_id"], "name": r["name"] or ""})
    return {"flags": flags, "messages": messages, "max_message_id": max_id}


def _render_page(clinic: dict, data: dict, days_param: int, sched_param: str = "", lang: str = DEFAULT_LANG,
                 channel: str = "") -> str:
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    dir_attr = "rtl" if lang == "ar" else "ltr"

    kpis = data["kpis"]
    filters = data.get("filters", {})
    name_filter_val = filters.get("name", "")
    date_from_val = filters.get("from", "")
    date_to_val = filters.get("to", "")
    filter_qs = _extra_qs(name_filter_val, date_from_val, date_to_val, sched_param, lang)

    lang_toggle_target = "ar" if lang != "ar" else "en"
    lang_toggle_url = (
        f'/clinic/{clinic["slug"]}/dashboard?days={days_param}'
        f'{_extra_qs(name_filter_val, date_from_val, date_to_val, sched_param, lang_toggle_target)}'
    )

    periods_html = ""
    for value, label_key in PERIOD_CHOICES:
        active = "active" if value == days_param else ""
        periods_html += f'<a class="{active}" href="?days={value}{filter_qs}">{_t(label_key, lang)}</a>'

    kpi_cards = [
        (kpis["conversations"], _t("kpi_conversations", lang)),
        (kpis["bookings_made"], _t("kpi_bookings_made", lang)),
        (f'{kpis["conversion_rate_pct"]}%', _t("kpi_conversion_rate", lang)),
        (kpis["new_patients_registered"], _t("kpi_new_patients", lang)),
        (kpis["flagged_conversations"], _t("kpi_flagged", lang)),
        (kpis["cancelled_appointments"], _t("kpi_cancelled", lang)),
        (kpis["avg_messages_per_conversation"], _t("kpi_avg_messages", lang)),
        (_fmt_money(kpis["estimated_revenue_booked_sar"]), _t("kpi_est_revenue", lang)),
    ]
    # Phone calls get their own cards (for clinics on the voice tier, or any
    # clinic that has received calls), so calls aren't lost among the chats.
    if clinic.get("voice_enabled") or kpis.get("phone_calls"):
        kpi_cards.insert(1, (kpis.get("phone_calls", 0), _t("kpi_phone_calls", lang)))
        kpi_cards.insert(2, (kpis.get("phone_minutes", 0), _t("kpi_phone_minutes", lang)))
    if clinic.get("voice_enabled"):
        now = datetime.now()
        usage = voice_usage.usage_summary(clinic, now.year, now.month)
        kpi_cards.append((f'{usage["minutes"]:,} / {usage["included"]:,}', _t("kpi_voice_month", lang)))
    kpi_html = "".join(
        f'<div class="kpi-card"><div class="kpi-value">{html.escape(str(v))}</div>'
        f'<div class="kpi-label">{html.escape(label)}</div></div>'
        for v, label in kpi_cards
    )

    # Schedule -- any single day's live front-desk view (defaults to
    # today), independent of the days= period and the name/date-range list
    # filters below. Today/Tomorrow are one click away; any other date via
    # the picker. Age/Gender ride alongside name/phone so reception can see
    # who's coming in without opening each conversation.
    schedule_resolved = data.get("schedule_date") or datetime.now().strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    no_sched_qs = _extra_qs(name_filter_val, date_from_val, date_to_val, "", lang)
    today_link = f'/clinic/{clinic["slug"]}/dashboard?days={days_param}{no_sched_qs}'
    tomorrow_qs = _extra_qs(name_filter_val, date_from_val, date_to_val, tomorrow_str, lang)
    tomorrow_link = f'/clinic/{clinic["slug"]}/dashboard?days={days_param}{tomorrow_qs}'

    schedule_picker_html = f"""
  <div class="schedule-picker">
    <a class="{'active' if schedule_resolved == today_str else ''}" href="{today_link}">{_t('schedule_today', lang)}</a>
    <a class="{'active' if schedule_resolved == tomorrow_str else ''}" href="{tomorrow_link}">{_t('schedule_tomorrow', lang)}</a>
    <form method="get">
      <input type="hidden" name="days" value="{days_param}">
      <input type="hidden" name="name" value="{html.escape(name_filter_val)}">
      <input type="hidden" name="from" value="{html.escape(date_from_val)}">
      <input type="hidden" name="to" value="{html.escape(date_to_val)}">
      <input type="hidden" name="lang" value="{lang}">
      <label>{_t('schedule_date_label', lang)} <input type="date" name="sched" value="{schedule_resolved}"></label>
      <button type="submit">{_t('schedule_go', lang)}</button>
    </form>
  </div>
"""

    if data.get("schedule_appointments"):
        rows = ""
        for a in data["schedule_appointments"]:
            badge = f'<span class="badge badge-cancelled">{html.escape(a["status"])}</span>' if a["status"] == "cancelled" else html.escape(a["status"])
            rows += (
                f'<tr><td>{_fmt_dt(a["appointment_time"])[-5:]}</td>'
                f'<td>{html.escape(a["patient_name"] or "")}</td>'
                f'<td>{_fmt_age(a["patient_age"])}</td>'
                f'<td>{_fmt_gender(a["patient_gender"], lang)}</td>'
                f'<td>{html.escape(a["phone_number"] or "")}</td>'
                f'<td>{html.escape(a["dentist_name"] or "")}</td>'
                f'<td>{html.escape(a["service_name"] or "—")}</td>'
                f'<td>{badge}</td>'
                f'<td>{_fmt_reminder(a, lang)}</td></tr>'
            )
        schedule_html = (
            f'<table><tr><th>{_t("col_time", lang)}</th><th>{_t("col_patient", lang)}</th>'
            f'<th>{_t("col_age", lang)}</th><th>{_t("col_gender", lang)}</th>'
            f'<th>{_t("col_whatsapp", lang)}</th>'
            f'<th>{_t("col_dentist", lang)}</th><th>{_t("col_service", lang)}</th>'
            f'<th>{_t("col_status", lang)}</th><th>{_t("col_reminder", lang)}</th></tr>{rows}</table>'
        )
    else:
        schedule_html = f'<div class="empty">{_t("schedule_empty", lang).format(date=_fmt_date_human(schedule_resolved))}</div>'

    # Filter bar -- controls the two lists below it (needs attention +
    # recent conversations). A From/To date narrows to an exact day (set
    # both the same) or a range, and overrides the period buttons above
    # for these two lists only. A name search with no date searches the
    # clinic's whole history, not just the current period.
    clear_link = (
        f'<a class="filter-clear" href="?days={days_param}{no_sched_qs}">{_t("filter_clear", lang)}</a>'
        if (name_filter_val or date_from_val or date_to_val) else ""
    )
    filter_bar_html = f"""
  <div class="filter-bar">
    <form method="get">
      <input type="hidden" name="days" value="{days_param}">
      <input type="hidden" name="sched" value="{html.escape(sched_param)}">
      <input type="hidden" name="lang" value="{lang}">
      <input type="text" name="name" placeholder="{_t('search_placeholder', lang)}" value="{html.escape(name_filter_val)}">
      <span class="date-field">{_t('filter_from', lang)} <input type="date" name="from" value="{html.escape(date_from_val)}"></span>
      <span class="date-field">{_t('filter_to', lang)} <input type="date" name="to" value="{html.escape(date_to_val)}"></span>
      <button type="submit">{_t('filter_button', lang)}</button>
      {clear_link}
    </form>
    <div class="filter-hint">{_t('filter_hint', lang)}</div>
  </div>
"""

    # Needs attention
    if data["needs_attention"]:
        items = ""
        for a in data["needs_attention"]:
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{a["conversation_id"]}?days={days_param}{filter_qs}'
            extra_bits = []
            if a.get("patient_age") is not None:
                extra_bits.append(f'{_t("col_age", lang)} {_fmt_age(a["patient_age"])}')
            if a.get("patient_gender"):
                extra_bits.append(f'{_t("col_gender", lang)} {_fmt_gender(a["patient_gender"], lang)}')
            extra_html = f' · {" · ".join(extra_bits)}' if extra_bits else ""
            items += (
                f'<div class="attention-item">{"📞 " if a.get("channel") == "phone" else ""}<b>{html.escape(a["patient_name"] or _t("unknown_patient", lang))}</b> '
                f'({html.escape(a["phone_number"] or _t("no_number", lang))}){extra_html} — {_fmt_dt(a["started_at"])} '
                f'— <a class="view-link" href="{conv_url}">{_t("view_conversation", lang)}</a>'
                f'<div class="attention-reason">{html.escape(a["reason"] or _t("no_reason_given", lang))}</div></div>'
            )
        attention_html = items
    elif name_filter_val or date_from_val or date_to_val:
        attention_html = f'<div class="empty">{_t("attention_empty_filtered", lang)}</div>'
    else:
        attention_html = f'<div class="empty">{_t("attention_empty", lang)}</div>'

    # All / WhatsApp / Phone calls tabs for the conversations list
    channel_tabs_html = '<div class="periods channel-tabs">' + "".join(
        f'<a class="{"active" if channel == value else ""}" '
        f'href="?days={days_param}{filter_qs}{("&ch=" + value) if value else ""}#recent">{_t(key, lang)}</a>'
        for value, key in (("", "tab_all"), ("whatsapp", "tab_whatsapp"), ("phone", "tab_calls"))
    ) + '</div>'

    # Recent conversations feed
    if data["recent_conversations"]:
        rows = ""
        for c in data["recent_conversations"]:
            if c.get("staff_handling") and c.get("channel", "whatsapp") == "whatsapp":
                booked_badge = f'<span class="badge badge-staff">{_t("badge_staff", lang)}</span>'
            elif c["flagged"]:
                booked_badge = f'<span class="badge badge-flag">{_t("badge_flagged", lang)}</span>'
            elif c["booked"]:
                booked_badge = f'<span class="badge badge-yes">{_t("badge_booked", lang)}</span>'
            else:
                booked_badge = f'<span class="badge badge-no">{_t("badge_no_booking", lang)}</span>'
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{c["conversation_id"]}?days={days_param}{filter_qs}'
            rows += (
                f'<tr><td>{_fmt_dt(c["started_at"])}</td>'
                f'<td>{"📞 " if c.get("channel") == "phone" else "💬 "}{html.escape(c["patient_name"] or _t("unknown_patient", lang))}</td>'
                f'<td>{_fmt_age(c["patient_age"])}</td>'
                f'<td>{_fmt_gender(c["patient_gender"], lang)}</td>'
                f'<td>{html.escape(c["phone_number"] or "")}</td>'
                f'<td>{_fmt_duration(c.get("duration_secs")) if c.get("channel") == "phone" else c["message_count"]}</td>'
                f'<td>{booked_badge}</td>'
                f'<td><a class="view-link" href="{conv_url}">{_t("view", lang)}</a></td></tr>'
            )
        recent_html = (
            f'<table><tr><th>{_t("col_started", lang)}</th><th>{_t("col_patient", lang)}</th>'
            f'<th>{_t("col_age", lang)}</th><th>{_t("col_gender", lang)}</th>'
            f'<th>{_t("col_whatsapp", lang)}</th><th>{_t("col_msgs_or_duration", lang)}</th>'
            f'<th>{_t("col_outcome", lang)}</th><th></th></tr>{rows}</table>'
        )
        if data.get("recent_conversations_truncated"):
            recent_html += f'<div class="truncated-note">{_t("truncated_note", lang).format(n=len(data["recent_conversations"]))}</div>'
    elif name_filter_val or date_from_val or date_to_val:
        recent_html = f'<div class="empty">{_t("recent_empty_filtered", lang)}</div>'
    else:
        recent_html = f'<div class="empty">{_t("recent_empty", lang)}</div>'

    # Conversation outcomes -- donut
    ob = data.get("outcome_breakdown", {})
    outcomes_html = _svg_donut_chart([
        (_t("outcome_booked", lang), ob.get("booked", 0), STATUS_GOOD),
        (_t("outcome_flagged", lang), ob.get("flagged", 0), STATUS_WARNING),
        (_t("outcome_no_booking", lang), ob.get("no_booking", 0), STATUS_MUTED),
    ])

    # Conversations by month -- trend bar chart (fixed 6-month window)
    trend_html = _svg_bar_chart(
        data.get("monthly_trend", []), value_key="count", label_key="label", bar_color=CHART_BLUE,
    )

    # Top services -- horizontal bar chart
    services_html = _svg_hbar_chart(
        data["top_services"], value_key="count", label_key="service_name", bar_color=CHART_BLUE,
    )
    if not data["top_services"]:
        services_html = f'<div class="empty">{_t("services_empty", lang)}</div>'

    # Bookings by doctor -- horizontal bar chart; label combines name +
    # specialty so the chart is legible on its own, without a separate
    # legend mapping doctor to specialty.
    doctor_items = [
        {
            "label": f'{d["dentist_name"]} ({d["specialization"]})' if d["specialization"] else d["dentist_name"],
            "count": d["count"],
        }
        for d in data.get("top_doctors", [])
    ]
    doctors_html = _svg_hbar_chart(doctor_items, value_key="count", label_key="label", bar_color=CHART_BLUE, label_w=260)
    if not doctor_items:
        doctors_html = f'<div class="empty">{_t("doctors_empty", lang)}</div>'

    # Bookings by specialty -- horizontal bar chart, same period/counting
    # as top_services and top_doctors above (all bookings in period,
    # including cancelled).
    specialties_html = _svg_hbar_chart(
        data.get("top_specialties", []), value_key="count", label_key="specialization", bar_color=CHART_BLUE,
    )
    if not data.get("top_specialties"):
        specialties_html = f'<div class="empty">{_t("specialties_empty", lang)}</div>'

    # Message volume by hour of day -- vertical bar chart, values hidden
    # (24 direct labels would be clutter -- the hover tooltip has the
    # exact count) and hour labels shown every 3 hours.
    hourly_items = [
        {"label": f'{h["hour"]:02d}', "message_count": h["message_count"]}
        for h in data["hourly_distribution"]
    ]
    hourly_html = _svg_bar_chart(
        hourly_items, value_key="message_count", label_key="label", width=900, height=160,
        bar_color=CHART_BLUE, show_values=False, label_every=3,
    )

    return f"""<!doctype html>
<html lang="{lang}" dir="{dir_attr}">
<head>
  <meta charset="utf-8">
  <noscript><meta http-equiv="refresh" content="30"></noscript>
  <title>{html.escape(clinic['name'])} — {_t('dashboard_title', lang)}</title>
  {PAGE_STYLE}
</head>
<body>
<div id="page">
  <div class="header-row">
    <div class="brand-row"><img src="{LOGO_URL}" alt="Mawaid"><h1>{html.escape(clinic['name'])} — {_t('dashboard_title', lang)}</h1></div>
    <a class="lang-toggle" href="{lang_toggle_url}">{_t('lang_toggle', lang)}</a>
  </div>
  <div class="subtitle">{_period_label(days_param, lang)} · {_t('updated', lang)} {_fmt_dt(data['generated_at'])} · {_t('refresh_note', lang)}</div>

  <div class="periods">{periods_html}</div>

  <div class="kpi-grid">{kpi_html}</div>

  <h2>{_t('h2_outcomes', lang)}</h2>
  {outcomes_html}

  <h2>{_t('h2_trend', lang)}</h2>
  {trend_html}

  <h2>{_t('h2_schedule', lang)} — {_fmt_date_human(schedule_resolved)}</h2>
  {schedule_picker_html}
  {schedule_html}

  {filter_bar_html}

  <h2>{_t('h2_needs_attention', lang)} ({len(data['needs_attention'])})</h2>
  {attention_html}

  <h2 id="recent">{_t('h2_recent', lang)}</h2>
  {channel_tabs_html}
  {recent_html}

  <h2>{_t('h2_services', lang)}</h2>
  {services_html}

  <h2>{_t('h2_doctors', lang)}</h2>
  {doctors_html}

  <h2>{_t('h2_specialties', lang)}</h2>
  {specialties_html}

  <h2>{_t('h2_hourly', lang)}</h2>
  {hourly_html}

  <div class="api-hint">
    {_t('api_hint', lang)}
    <code>/clinic/{html.escape(clinic['slug'])}/api/dashboard?days={days_param}</code> {_t('api_hint_suffix', lang)}.
  </div>
</div>
{_alerts_widget(clinic, lang, soft_refresh=True)}
</body>
</html>"""


def _render_transcript(messages: list, patient, is_phone: bool, lang: str) -> str:
    """The human-readable half of a conversation as chat bubbles. Tool
    rows ("tool"/"agent_tool_call") are Sia's internal lookups and are left
    out. Staff replies sent from this dashboard sit on the clinic's side,
    in blue, labelled as staff."""
    visible = [m for m in messages if m.sender in ("user", "agent", "staff")]
    if not visible:
        return f'<div class="empty">{_t("no_messages", lang)}</div>'
    agent_name = _t("voice_agent_name", lang) if is_phone else "Sia"
    out = ""
    for m in visible:
        if m.sender == "user":
            side, speaker = "patient", (patient.name if patient else "Patient")
        elif m.sender == "staff":
            side, speaker = "staff", _t("staff_name", lang)
        else:
            side, speaker = "sia", agent_name
        out += (
            f'<div class="bubble-row {side}"><div>'
            f'<div class="bubble">{html.escape(m.message)}</div>'
            f'<div class="bubble-time">{html.escape(speaker)} · {_fmt_dt(str(m.created_at))}</div>'
            f'</div></div>'
        )
    return out


def _fmt_left(delta: timedelta, lang: str) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    return _t("hours_short", lang).format(h=mins // 60, m=mins % 60)


def _inbox_token(clinic: dict, conversation_id: int) -> str:
    """Per-conversation form token. The dashboard uses HTTP Basic auth,
    which the browser re-sends automatically -- so without this, another
    website could post a reply "as" a logged-in receptionist."""
    secret = (os.environ.get("META_APP_SECRET") or os.environ.get("ADMIN_PASSWORD") or "mawaid").encode()
    return hmac.new(secret, f"inbox:{clinic['slug']}:{conversation_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _inbox_state(clinic: dict, conversation, patient, now: Optional[datetime] = None) -> dict:
    """Everything the conversation page needs to show the inbox controls."""
    now = now or datetime.now()
    state = {"is_whatsapp": getattr(conversation, "channel", "whatsapp") == "whatsapp" and patient is not None,
             "paused": None, "can_reply": False, "window_closes": None, "blocked": None, "latest_id": None}
    if not state["is_whatsapp"]:
        state["blocked"] = "phone"
        return state
    state["paused"] = db.get_ai_pause(patient.id, now)
    latest = db.get_latest_whatsapp_conversation(patient.id)
    if latest is not None and latest.id != conversation.id:
        state["blocked"] = "not_latest"
        state["latest_id"] = latest.id
        return state
    closes = handoff.window_closes_at(patient.phone_number)
    state["window_closes"] = closes
    if closes is None or now >= closes:
        state["blocked"] = "window_closed"
        return state
    state["can_reply"] = True
    return state


def _render_inbox_bar(clinic: dict, conversation, inbox: dict, lang: str, qs: str) -> str:
    if not inbox["is_whatsapp"] or inbox["blocked"] == "not_latest":
        return ""
    base = f'/clinic/{clinic["slug"]}/dashboard/conversation/{conversation.id}'
    token = _inbox_token(clinic, conversation.id)
    if inbox["paused"]:
        text = _t("inbox_staff_active", lang).format(time=inbox["paused"]["until"].strftime("%H:%M"))
        action, label, cls = "handback", _t("inbox_hand_back", lang), "staff"
        if inbox["can_reply"]:
            # The Return to Sia button sits next to Send in the reply box.
            return f'<div class="inbox-bar {cls}"><span>{html.escape(text)}</span></div>'
    else:
        text, action, label, cls = _t("inbox_sia_active", lang), "takeover", _t("inbox_take_over", lang), "sia"
    return (
        f'<div class="inbox-bar {cls}"><span>{html.escape(text)}</span>'
        f'<form method="post" action="{base}/{action}?{qs}">'
        f'<input type="hidden" name="token" value="{token}">'
        f'<button type="submit">{label}</button></form></div>'
    )


def _render_conversation_page(
    clinic: dict, conversation, patient, messages: list, booked: bool, days_param: int,
    name_filter: str = "", date_from: str = "", date_to: str = "", sched_param: str = "",
    lang: str = DEFAULT_LANG, inbox: Optional[dict] = None, flash: str = "",
) -> str:
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    dir_attr = "rtl" if lang == "ar" else "ltr"
    inbox = inbox or {"is_whatsapp": False, "paused": None, "can_reply": False, "window_closes": None,
                      "blocked": "phone", "latest_id": None}

    page_qs = f"days={days_param}{_extra_qs(name_filter, date_from, date_to, sched_param, lang)}"
    back_url = f'/clinic/{clinic["slug"]}/dashboard?{page_qs}'
    lang_toggle_target = "ar" if lang != "ar" else "en"
    lang_toggle_url = (
        f'/clinic/{clinic["slug"]}/dashboard/conversation/{conversation.id}?days={days_param}'
        f'{_extra_qs(name_filter, date_from, date_to, sched_param, lang_toggle_target)}'
    )
    base = f'/clinic/{clinic["slug"]}/dashboard/conversation/{conversation.id}'

    if conversation.flagged_for_staff:
        flag_banner = (
            f'<div class="attention-item"><b>{_t("h2_needs_attention", lang)}</b>'
            f'<div class="attention-reason">{html.escape(conversation.flag_reason or _t("no_reason_given", lang))}</div></div>'
        )
    else:
        flag_banner = ""

    outcome_badge = f'<span class="badge badge-yes">{_t("booked_during", lang)}</span>' if booked \
        else f'<span class="badge badge-no">{_t("no_booking_made", lang)}</span>'

    age_gender_bits = []
    if patient is not None and getattr(patient, "age", None) is not None:
        age_gender_bits.append(f'{_t("col_age", lang)} {_fmt_age(patient.age)}')
    if patient is not None and getattr(patient, "gender", None):
        age_gender_bits.append(f'{_t("col_gender", lang)} {_fmt_gender(patient.gender, lang)}')
    age_gender_html = f' · {" · ".join(age_gender_bits)}' if age_gender_bits else ""

    is_phone = getattr(conversation, "channel", "whatsapp") == "phone"
    transcript_html = _render_transcript(messages, patient, is_phone, lang)

    flash_html = ""
    if flash:
        kind, _, code = flash.partition(":")
        key = f"inbox_{kind}_{code}"
        if key in TRANSLATIONS:
            flash_html = f'<div class="flash {"ok" if kind == "ok" else "err"}">{_t(key, lang)}</div>'

    inbox_bar_html = _render_inbox_bar(clinic, conversation, inbox, lang, page_qs)

    if inbox["can_reply"]:
        left = inbox["window_closes"] - datetime.now()
        reply_html = f"""
  <form class="reply-box" method="post" action="{base}/reply?{page_qs}">
    <input type="hidden" name="token" value="{_inbox_token(clinic, conversation.id)}">
    <textarea name="text" id="reply-text" maxlength="4000" placeholder="{_t('inbox_reply_placeholder', lang)}" dir="auto"></textarea>
    <div class="reply-row">
      <span class="window-note">{_t('inbox_window_left', lang).format(left=_fmt_left(left, lang))}</span>
      <span class="reply-actions">
        <button type="submit" class="handback-btn" id="handback-btn" formaction="{base}/handback?{page_qs}"{'' if inbox["paused"] else ' style="display:none"'}>{_t('inbox_hand_back', lang)}</button>
        <button type="submit">{_t('inbox_send', lang)}</button>
      </span>
    </div>
  </form>"""
    elif inbox["blocked"] == "window_closed":
        reply_html = f'<div class="reply-closed">{_t("inbox_window_closed", lang)}</div>'
    elif inbox["blocked"] == "not_latest":
        latest_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{inbox["latest_id"]}?{page_qs}'
        reply_html = (f'<div class="reply-closed">{_t("inbox_not_latest", lang)} '
                      f'<a href="{latest_url}">{_t("inbox_open_latest", lang)}</a></div>')
    elif is_phone:
        reply_html = f'<div class="reply-closed">{_t("inbox_phone_note", lang)}</div>'
    else:
        reply_html = ""

    # Live updates: every 5 s, re-fetch the transcript and the Sia/staff bar
    # without reloading the page (a reload would wipe a half-typed reply).
    poll_url = f'/clinic/{clinic["slug"]}/api/conversation/{conversation.id}?lang={lang}&{page_qs}'
    live_js = f"""
<script>
(function () {{
  var last = null;
  function tick() {{
    fetch({json.dumps(poll_url)}, {{credentials: "same-origin", cache: "no-store"}})
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (d) {{
        if (!d) return;
        var key = d.count + "|" + d.last_id;
        if (last !== null && key !== last) {{
          document.getElementById("transcript").innerHTML = d.transcript_html;
          window.scrollTo(0, document.body.scrollHeight);
        }}
        last = key;
        var bar = document.getElementById("inbox-bar");
        if (bar && bar.getAttribute("data-state") !== d.bar_state) {{
          bar.innerHTML = d.bar_html;
          bar.setAttribute("data-state", d.bar_state);
        }}
        var hb = document.getElementById("handback-btn");
        if (hb) hb.style.display = d.bar_state === "staff" ? "" : "none";
      }})
      .catch(function () {{}});
  }}
  tick();
  setInterval(tick, 5000);
  var box = document.getElementById("reply-text");
  if (box) {{
    box.addEventListener("keydown", function (e) {{
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); box.form.submit(); }}
    }});
  }}
  window.scrollTo(0, document.body.scrollHeight);
}})();
</script>"""

    bar_state = "phone" if not inbox["is_whatsapp"] else ("staff" if inbox["paused"] else "sia")

    return f"""<!doctype html>
<html lang="{lang}" dir="{dir_attr}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(patient.name if patient else _t('unknown_patient_title', lang))} — {html.escape(clinic['name'])}</title>
  {PAGE_STYLE}
</head>
<body>
  <div class="header-row">
    <div class="brand-row"><img src="{LOGO_URL}" alt="Mawaid"></div>
    <a class="lang-toggle" href="{lang_toggle_url}">{_t('lang_toggle', lang)}</a>
  </div>
  <a class="back-link" href="{back_url}">{'&rarr;' if lang == 'ar' else '&larr;'} {_t('back_to_dashboard', lang)}</a>
  <h1>{'📞' if is_phone else '💬'} {html.escape(patient.name if patient else _t('unknown_patient_title', lang))}</h1>
  <div class="conv-meta">
    {html.escape(patient.phone_number if patient else '')}{age_gender_html} · {_t('started_label', lang)} {_fmt_dt(str(conversation.started_at))}
    · {_t('status_label', lang)}: {html.escape(conversation.status)} · {outcome_badge}
  </div>

  {flash_html}
  {flag_banner}
  <div id="inbox-bar" data-state="{bar_state}">{inbox_bar_html}</div>

  <div class="transcript" id="transcript">{transcript_html}</div>
  {reply_html}
  {live_js}
  {_alerts_widget(clinic, lang)}
</body>
</html>"""


def _load_conversation(clinic: dict, conversation_id: int):
    db.set_current_db_path(clinic["db_path"])
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="No such conversation")
    patient = db.get_patient(conversation.patient_id) if conversation.patient_id else None
    return conversation, patient


@router.get("/clinic/{slug}/dashboard/conversation/{conversation_id}", response_class=HTMLResponse)
async def clinic_dashboard_conversation(
    slug: str, conversation_id: int, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    sched: Optional[str] = Query(None),
    lang: str = Query(DEFAULT_LANG),
    flash: Optional[str] = Query(None),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    conversation, patient = _load_conversation(clinic, conversation_id)
    messages = db.get_messages(conversation_id)

    booked = False
    if conversation.patient_id is not None:
        with db.db() as conn:
            booked_row = conn.execute(
                """
                SELECT 1 FROM appointments
                WHERE patient_id = ? AND created_at IS NOT NULL AND created_at >= ?
                  AND (? IS NULL OR created_at <= ?)
                LIMIT 1
                """,
                (
                    conversation.patient_id, conversation.started_at.isoformat(),
                    conversation.ended_at.isoformat() if conversation.ended_at else None,
                    conversation.ended_at.isoformat() if conversation.ended_at else None,
                ),
            ).fetchone()
            booked = booked_row is not None

    return HTMLResponse(_render_conversation_page(
        clinic, conversation, patient, messages, booked, days,
        name or "", date_from or "", date_to or "", sched or "", lang,
        inbox=_inbox_state(clinic, conversation, patient), flash=flash or "",
    ))


@router.get("/clinic/{slug}/api/conversation/{conversation_id}")
async def clinic_conversation_poll(
    slug: str, conversation_id: int, request: Request,
    lang: str = Query(DEFAULT_LANG),
    credentials: HTTPBasicCredentials = Depends(security),
):
    """What the open conversation page polls every few seconds: the
    transcript and the Sia/staff bar, pre-rendered, so new patient
    messages show up without a page reload."""
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    conversation, patient = _load_conversation(clinic, conversation_id)
    messages = db.get_messages(conversation_id)
    visible = [m for m in messages if m.sender in ("user", "agent", "staff")]
    inbox = _inbox_state(clinic, conversation, patient)
    page_qs = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.items() if k != "lang")
    page_qs = (page_qs + "&" if page_qs else "") + f"lang={lang}"
    is_phone = getattr(conversation, "channel", "whatsapp") == "phone"
    return {
        "count": len(visible),
        "last_id": visible[-1].id if visible else None,
        "transcript_html": _render_transcript(messages, patient, is_phone, lang),
        "bar_state": "phone" if not inbox["is_whatsapp"] else ("staff" if inbox["paused"] else "sia"),
        "bar_html": _render_inbox_bar(clinic, conversation, inbox, lang, page_qs),
        "sia_paused_until": inbox["paused"]["until"].isoformat() if inbox.get("paused") else None,
        "can_reply": inbox["can_reply"],
    }


def _check_inbox_post(request: Request, clinic: dict, conversation_id: int, token: str):
    """Blocks cross-site form posts: the per-conversation token must match,
    and when the browser says where the post came from, it must be here."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if urlparse(origin).netloc != host:
            raise HTTPException(status_code=403, detail="Cross-site request blocked")
    return bool(token) and hmac.compare_digest(token, _inbox_token(clinic, conversation_id))


def _back_to_conversation(slug: str, conversation_id: int, request: Request, flash: str) -> RedirectResponse:
    params = {k: v for k, v in request.query_params.items() if k != "flash"}
    params["flash"] = flash
    qs = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return RedirectResponse(f"/clinic/{slug}/dashboard/conversation/{conversation_id}?{qs}", status_code=303)


@router.post("/clinic/{slug}/dashboard/conversation/{conversation_id}/reply")
async def clinic_conversation_reply(
    slug: str, conversation_id: int, request: Request,
    text: str = Form(""), token: str = Form(""),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)
    if not _check_inbox_post(request, clinic, conversation_id, token):
        return _back_to_conversation(slug, conversation_id, request, "err:expired")
    conversation, _patient = _load_conversation(clinic, conversation_id)
    try:
        handoff.staff_reply(clinic, conversation, text, by=credentials.username)
    except handoff.ReplyError as e:
        return _back_to_conversation(slug, conversation_id, request, f"err:{e.code}")
    return _back_to_conversation(slug, conversation_id, request, "ok:sent")


@router.post("/clinic/{slug}/dashboard/conversation/{conversation_id}/takeover")
async def clinic_conversation_takeover(
    slug: str, conversation_id: int, request: Request, token: str = Form(""),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)
    if not _check_inbox_post(request, clinic, conversation_id, token):
        return _back_to_conversation(slug, conversation_id, request, "err:expired")
    conversation, patient = _load_conversation(clinic, conversation_id)
    if getattr(conversation, "channel", "whatsapp") != "whatsapp" or patient is None:
        return _back_to_conversation(slug, conversation_id, request, "err:not_whatsapp")
    handoff.take_over(clinic, conversation, by=credentials.username)
    return _back_to_conversation(slug, conversation_id, request, "ok:taken")


@router.post("/clinic/{slug}/dashboard/conversation/{conversation_id}/handback")
async def clinic_conversation_handback(
    slug: str, conversation_id: int, request: Request, token: str = Form(""),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)
    if not _check_inbox_post(request, clinic, conversation_id, token):
        return _back_to_conversation(slug, conversation_id, request, "err:expired")
    conversation, patient = _load_conversation(clinic, conversation_id)
    if patient is not None:
        handoff.hand_back(clinic, conversation)
    return _back_to_conversation(slug, conversation_id, request, "ok:returned")


@router.get("/clinic/{slug}/dashboard", response_class=HTMLResponse)
async def clinic_dashboard(
    slug: str, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    sched: Optional[str] = Query(None),
    lang: str = Query(DEFAULT_LANG),
    ch: Optional[str] = Query(None),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    channel = ch if ch in ("phone", "whatsapp") else ""
    data = analytics.get_dashboard_summary(
        days=_days_param(days), name_filter=name, date_from=date_from, date_to=date_to,
        schedule_date=sched, channel=channel or None,
    )
    return HTMLResponse(_render_page(clinic, data, days, sched or "", lang, channel))


@router.get("/clinic/{slug}/api/dashboard")
async def clinic_dashboard_api(
    slug: str, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    sched: Optional[str] = Query(None),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    data = analytics.get_dashboard_summary(
        days=_days_param(days), name_filter=name, date_from=date_from, date_to=date_to,
        schedule_date=sched,
    )
    return {
        "clinic": {"name": clinic["name"], "slug": clinic["slug"]},
        **data,
    }
