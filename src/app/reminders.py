"""
Same-day appointment reminders over WhatsApp.

Each clinic's worker runs `reminder_loop()` next to its conversation
cleanup task. Every few minutes it looks at TODAY's upcoming appointments
(scheduled or rescheduled) in that clinic's own database and sends each
patient one reminder on the day of the appointment.

When a reminder goes out
    At (appointment time - REMINDER_HOURS_BEFORE), but never earlier than
    REMINDER_EARLIEST_HOUR in the morning. With the defaults (3 h, 8 AM):
    a 4 PM appointment is reminded at 1 PM, a 10 AM appointment at 8 AM.
    Skipped: appointments booked less than REMINDER_HOURS_BEFORE ahead
    (the patient just booked, they know), and anything starting in under
    15 minutes by the time the loop gets to it (e.g. after a redeploy).

Meta's 24-hour rule
    A business may only send free text to someone who messaged it within
    the last 24 hours. Otherwise it must use an APPROVED message template.
    So: if the patient wrote to this clinic's WhatsApp in the last ~23 h,
    Sia sends a normal text; otherwise the approved template is used.
    `ensure_reminder_template()` submits that template to each clinic's
    WABA automatically at startup (Meta reviews it, usually within minutes
    to a few hours). Until it's approved, template reminders fail and are
    logged as `failed:...` on the appointment (they aren't retried).

Each appointment row records what happened in `reminder_status`
(sent_text / sent_template / skipped:<why> / failed:<why>), keyed by
`reminder_sent_for` = the appointment_time it was for, so a rescheduled
appointment gets a fresh reminder for its new time.

Environment variables (all optional):
    REMINDERS_ENABLED          "true" (default) / "false"
    REMINDER_HOURS_BEFORE      default 3
    REMINDER_EARLIEST_HOUR     default 8   (local time, TZ=Asia/Riyadh)
    REMINDER_CHECK_MINUTES     default 5
    REMINDER_TEMPLATE_NAME     default "mawaid_same_day_reminder"
    REMINDER_TEMPLATE_LANG     default "ar"
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from shared import db
from shared import clinics_store

logger = logging.getLogger(__name__)

TEMPLATE_BODY = (
    "مرحباً {{1}} 👋\n"
    "نذكّرك بموعدك اليوم في {{2}} الساعة {{3}} مع {{4}}.\n"
    "لتغيير الموعد أو إلغائه، رد على هذه الرسالة.\n\n"
    "Reminder: you have an appointment today (details above). "
    "Reply to this message to reschedule or cancel."
)
TEMPLATE_EXAMPLE = ["محمد", "عيادة الابتسامة", "4:00 مساءً", "د. سارة"]


def _enabled() -> bool:
    return os.getenv("REMINDERS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _template_name() -> str:
    return os.getenv("REMINDER_TEMPLATE_NAME") or "mawaid_same_day_reminder"


def _template_lang() -> str:
    return os.getenv("REMINDER_TEMPLATE_LANG") or "ar"


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{os.environ.get('GRAPH_API_VERSION')}/{path}"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_time_ar(dt: datetime) -> str:
    """16:00 -> '4:00 مساءً', 09:30 -> '9:30 صباحاً'."""
    hour12 = dt.hour % 12 or 12
    period = "صباحاً" if dt.hour < 12 else "مساءً"
    return f"{hour12}:{dt.minute:02d} {period}"


def patient_first_name(name: Optional[str]) -> str:
    name = (name or "").strip()
    if not name or name.lower() == "new patient":
        return "مراجعنا العزيز"
    return name.split()[0]


def reminder_params(appt: dict, clinic_name: str) -> list:
    return [
        patient_first_name(appt["patient_name"]),
        clinic_name,
        format_time_ar(datetime.fromisoformat(appt["appointment_time"])),
        appt["dentist_name"] or "الطبيب",
    ]


def reminder_text(params: list) -> str:
    text = TEMPLATE_BODY
    for i, value in enumerate(params, start=1):
        text = text.replace("{{%d}}" % i, value)
    return text


# ---------------------------------------------------------------------------
# Scheduling rule
# ---------------------------------------------------------------------------
def send_at(appt_time: datetime) -> datetime:
    hours_before = _int_env("REMINDER_HOURS_BEFORE", 3)
    earliest = appt_time.replace(hour=_int_env("REMINDER_EARLIEST_HOUR", 8), minute=0, second=0, microsecond=0)
    return max(appt_time - timedelta(hours=hours_before), earliest)


def decide(appt: dict, now: datetime) -> Optional[str]:
    """Returns None if it isn't time yet, 'send', or 'skipped:<why>'."""
    appt_time = datetime.fromisoformat(appt["appointment_time"])
    if now < send_at(appt_time):
        return None
    if appt_time - now < timedelta(minutes=15):
        return "skipped:too_late"
    created = appt.get("created_at")
    if created and appt["status"] == "scheduled":
        try:
            if appt_time - datetime.fromisoformat(created) < timedelta(hours=_int_env("REMINDER_HOURS_BEFORE", 3)):
                return "skipped:booked_recently"
        except ValueError:
            pass
    return "send"


# ---------------------------------------------------------------------------
# Database helpers (run inside the clinic worker's task -> its own DB file)
# ---------------------------------------------------------------------------
def due_candidates(now: datetime) -> list:
    """Today's future appointments that haven't been handled for their
    current time yet."""
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.appointment_time, a.status, a.created_at,
                   d.name AS dentist_name, p.name AS patient_name, p.phone_number
            FROM appointments a
            JOIN dentists d ON d.id = a.dentist_id
            JOIN patients p ON p.id = a.patient_id
            WHERE a.status IN ('scheduled', 'rescheduled')
              AND a.appointment_time > ? AND a.appointment_time <= ?
              AND (a.reminder_sent_for IS NULL OR a.reminder_sent_for != a.appointment_time)
            ORDER BY a.appointment_time
            """,
            (now.isoformat(), day_end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def mark(appt: dict, status: str) -> None:
    with db.db() as conn:
        conn.execute(
            "UPDATE appointments SET reminder_sent_for=?, reminder_status=? WHERE id=?",
            (appt["appointment_time"], status[:200], appt["id"]),
        )


def whatsapp_window_open(phone_number: str, now: datetime) -> bool:
    """True if this number sent the clinic a WhatsApp message in the last
    23 hours (1 h margin under Meta's 24 h limit). Phone calls don't count."""
    with db.db() as conn:
        row = conn.execute(
            """
            SELECT MAX(m.created_at) AS last_in
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            JOIN patients p ON p.id = c.patient_id
            WHERE p.phone_number = ? AND m.sender = 'user'
              AND COALESCE(c.channel, 'whatsapp') = 'whatsapp'
            """,
            (phone_number,),
        ).fetchone()
    if not row or not row["last_in"]:
        return False
    try:
        return now - datetime.fromisoformat(row["last_in"]) < timedelta(hours=23)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Meta Graph API
# ---------------------------------------------------------------------------
def send_template(clinic: dict, to: str, params: list) -> requests.Response:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": _template_name(),
            "language": {"code": _template_lang()},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }],
        },
    }
    return requests.post(
        _graph_url(f"{clinic['whatsapp_phone_number_id']}/messages"),
        headers={"Authorization": f"Bearer {clinic['whatsapp_access_token']}",
                 "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=20,
    )


def ensure_reminder_template(clinic: dict) -> str:
    """Makes sure the reminder template exists on this clinic's WABA,
    submitting it for Meta's review if it doesn't. Returns its status
    (APPROVED / PENDING / REJECTED / ...) or an 'error:...' string. Safe to
    call on every startup."""
    headers = {"Authorization": f"Bearer {clinic['whatsapp_access_token']}"}
    waba = clinic["whatsapp_waba_id"]
    name, lang = _template_name(), _template_lang()
    try:
        r = requests.get(_graph_url(f"{waba}/message_templates"),
                         params={"name": name, "fields": "name,language,status"},
                         headers=headers, timeout=20)
        if r.ok:
            for t in r.json().get("data", []):
                if t.get("name") == name and t.get("language") == lang:
                    return t.get("status", "UNKNOWN")
        else:
            return f"error:lookup {r.status_code} {r.text[:300]}"

        r = requests.post(
            _graph_url(f"{waba}/message_templates"),
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({
                "name": name,
                "language": lang,
                "category": "UTILITY",
                "components": [{
                    "type": "BODY",
                    "text": TEMPLATE_BODY,
                    "example": {"body_text": [TEMPLATE_EXAMPLE]},
                }],
            }),
            timeout=20,
        )
        if r.ok:
            return r.json().get("status", "PENDING")
        return f"error:create {r.status_code} {r.text[:300]}"
    except requests.RequestException as e:
        return f"error:{e}"


# ---------------------------------------------------------------------------
# One pass + the loop
# ---------------------------------------------------------------------------
def run_once(worker, now: Optional[datetime] = None) -> list:
    """Handles every reminder that's due right now for this worker's
    clinic. Returns a list of (appointment_id, status) for logging/tests."""
    clinic = worker.clinic
    now = now or datetime.now()
    results = []

    ends = clinics_store.subscription_ends_on(clinic)
    if ends and now >= ends:
        return results

    for appt in due_candidates(now):
        decision = decide(appt, now)
        if decision is None:
            continue
        if decision != "send":
            mark(appt, decision)
            results.append((appt["id"], decision))
            continue

        params = reminder_params(appt, clinic["name"])
        to = appt["phone_number"]
        try:
            if whatsapp_window_open(to, now):
                worker.send(to, reminder_text(params))
                status = "sent_text"
            else:
                r = send_template(clinic, to, params)
                status = "sent_template" if r.ok else f"failed:{r.status_code} {r.text[:150]}"
        except Exception as e:  # never let one patient break the loop
            status = f"failed:{e}"[:200]

        mark(appt, status)
        results.append((appt["id"], status))
        log = logger.info if status.startswith("sent") else logger.error
        log(f"[{clinic['slug']}] Reminder for appointment {appt['id']} ({to}, {appt['appointment_time']}): {status}")

    return results


async def reminder_loop(worker):
    if not _enabled():
        logger.info(f"[{worker.clinic['slug']}] Same-day reminders are switched off (REMINDERS_ENABLED).")
        return

    status = await asyncio.to_thread(ensure_reminder_template, worker.clinic)
    worker.reminder_template_status = status
    log = logger.error if status.startswith("error") or status == "REJECTED" else logger.info
    log(f"[{worker.clinic['slug']}] Reminder template '{_template_name()}' ({_template_lang()}): {status}")

    interval = max(1, _int_env("REMINDER_CHECK_MINUTES", 5)) * 60
    while True:
        try:
            run_once(worker)
        except Exception:
            logger.exception(f"[{worker.clinic['slug']}] Reminder pass failed")
        await asyncio.sleep(interval)
