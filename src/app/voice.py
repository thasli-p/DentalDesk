"""
Voice receptionist API -- the endpoints the phone-call agent (ElevenLabs
Agents, configured as "server tools") calls DURING a live phone call to
read real clinic data and make real bookings.

Why this exists as plain HTTP endpoints rather than reusing Sia's MCP
tools directly: the voice agent runs on ElevenLabs' servers, not inside
this app, so it can only reach Mawaid over the public internet. These
endpoints are thin wrappers around the SAME shared/db.py functions Sia's
WhatsApp tools use, so a booking made by phone lands in the exact same
clinic database -- it shows up on the staff dashboard's schedule, and
Sia on WhatsApp can see it too (e.g. if the patient later messages to
cancel).

URL shape: /voice/<clinic-slug>/<tool>. The slug picks the clinic, just
like the staff dashboard's /clinic/<slug>/... URLs, so ONE shared voice
agent can serve every clinic -- the agent just needs to know which slug a
call belongs to (see docs/voice-receptionist.md for how that's set).

Security: every request must carry the header
    X-Voice-Key: <VOICE_API_KEY>
where VOICE_API_KEY is a long random secret set in Railway's Variables
tab and pasted into each ElevenLabs tool's headers. Without it these
endpoints refuse everything (and if VOICE_API_KEY isn't set at all, the
whole voice API is switched off), since they can read and change patient
bookings.

Phone numbers: callers say local numbers ("05XXXXXXXX") while WhatsApp
stores international ones without a plus ("9665XXXXXXXX"). Every number
coming in here is normalised to the WhatsApp form, so a patient who books
by phone and later messages on WhatsApp is recognised as the same person.
"""
import hashlib
import hmac
import json
import logging
import time
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from shared import clinics_store, db
from shared.models import Appointment, Patient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

WEEKDAY_NAMES_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_key(x_voice_key: Optional[str]) -> None:
    expected = os.environ.get("VOICE_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="Voice API is not enabled (VOICE_API_KEY is not set).")
    if not x_voice_key or not hmac.compare_digest(x_voice_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Voice-Key header.")


def resolve_clinic(key: Optional[str]) -> Optional[dict]:
    """Which clinic a voice request is for. `key` is what sits in the URL
    where the clinic goes: normally the number the patient DIALLED (the
    ElevenLabs tools fill it from system__called_number), so one shared agent
    serves every clinic -- each clinic's voice number is set in /admin/clinics.
    A clinic slug also works. When there is no dialled number (e.g. the
    ElevenLabs web test link) or it isn't registered to any clinic, the pilot
    clinic in VOICE_DEFAULT_CLINIC_SLUG is used."""
    key = (key or "").strip()
    clinic = None
    if key and not key.startswith("{"):
        clinic = clinics_store.get_clinic_by_slug(key) or clinics_store.get_clinic_by_voice_number(key)
        if not clinic and clinics_store.normalize_phone_digits(key):
            logger.warning("Voice request for number %s, which no clinic has as its voice number.", key)
    if not clinic:
        default = os.environ.get("VOICE_DEFAULT_CLINIC_SLUG")
        clinic = clinics_store.get_clinic_by_slug(default) if default else None
    return clinic


def _load_clinic(slug: str) -> dict:
    """Finds the clinic (by dialled number or slug, see resolve_clinic) and
    points shared/db.py at its own database for the rest of this request."""
    clinic = resolve_clinic(slug)
    if not clinic or not clinic.get("active"):
        raise HTTPException(status_code=404, detail="Clinic not found or inactive.")
    if not clinic.get("voice_enabled"):
        # Voice is a separate paid tier, switched on per clinic in /admin/clinics.
        raise HTTPException(status_code=403, detail="Voice receptionist is not enabled for this clinic.")
    db.set_current_db_path(clinic["db_path"])
    return clinic


def normalize_saudi_phone(raw: str) -> Optional[str]:
    """Turns whatever the caller said / caller ID provided into WhatsApp's
    stored format (9665XXXXXXXX). Returns None if it isn't a valid Saudi
    mobile number, so the agent can ask the caller to repeat it instead of
    booking under a wrong number."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("966"):
        digits = digits[3:]
    if digits.startswith("0"):
        digits = digits[1:]
    # What's left must be the 9-digit mobile part: 5XXXXXXXX
    if len(digits) == 9 and digits.startswith("5"):
        return "966" + digits
    return None


def _local_phone(stored: str) -> str:
    """9665XXXXXXXX -> 05XXXXXXXX, for reading back to the caller."""
    return "0" + stored[3:] if stored.startswith("966") else stored


def _bad_phone(raw: str) -> Dict[str, Any]:
    return {
        "error": "invalid_phone",
        "details": f"'{raw}' is not a valid Saudi mobile number. It must be 10 digits starting with 05. "
                   "Ask the caller to repeat it slowly.",
    }


def _parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _day_label(dt: datetime) -> str:
    return f"{WEEKDAY_NAMES_AR[dt.weekday()]} {dt.strftime('%Y-%m-%d')}"


def _match_service(clinic: dict, service_name: Optional[str]):
    """Same best-effort match as Sia's book_appointment, but against this
    clinic's own services list (from its clinics-store row)."""
    if not service_name:
        return None, None
    wanted = service_name.strip().lower()
    for svc in clinic.get("services") or []:
        name = str(svc.get("name", "")).strip()
        if name.lower() == wanted:
            try:
                price = float(svc.get("price_sar"))
            except (TypeError, ValueError):
                price = None
            return name, price
    return None, None


def _is_holiday(clinic: dict, dt: datetime) -> bool:
    return dt.strftime("%Y-%m-%d") in set(clinic.get("holidays") or [])


# --- Working-hours parsing --------------------------------------------------
# Dentist schedules and clinic hours are free text typed by the clinic in the
# admin page ("Mon-Fri 10:00-17:00", "Saturday-Thursday 9:00 AM - 9:00 PM,
# Friday closed", "السبت-الخميس 4م-10م" ...). The AI used to read these itself
# and sometimes got the days wrong, so the server now works them out and
# refuses bookings outside them. Unreadable text -> no check (never blocks).

_DAY_WORDS = {
    # Python weekday(): Mon=0 ... Sun=6
    "mon": 0, "monday": 0, "الاثنين": 0, "الإثنين": 0, "اثنين": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "الثلاثاء": 1, "ثلاثاء": 1,
    "wed": 2, "wednesday": 2, "الأربعاء": 2, "الاربعاء": 2, "اربعاء": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "الخميس": 3, "خميس": 3,
    "fri": 4, "friday": 4, "الجمعة": 4, "الجمعه": 4, "جمعة": 4,
    "sat": 5, "saturday": 5, "السبت": 5, "سبت": 5,
    "sun": 6, "sunday": 6, "الأحد": 6, "الاحد": 6, "أحد": 6,
}
_DAY_RE = "|".join(sorted((re.escape(w) for w in _DAY_WORDS), key=len, reverse=True))
_TIME_RE = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|ص|م|صباحا|صباحًا|مساء|مساءً)?"
_DASH = r"\s*(?:-|–|—|to|الى|إلى|حتى)\s*"


def _to_minutes(h: str, m: Optional[str], ampm: Optional[str], default_pm: bool = False) -> int:
    hour, minute = int(h), int(m or 0)
    tag = (ampm or "").lower().replace(".", "")
    if tag in ("pm", "م", "مساء", "مساءً") and hour < 12:
        hour += 12
    elif tag in ("am", "ص", "صباحا", "صباحًا") and hour == 12:
        hour = 0
    elif not tag and default_pm and hour < 12:
        hour += 12
    return hour * 60 + minute


def _expand_days(a: int, b: Optional[int]) -> List[int]:
    if b is None:
        return [a]
    days, d = [a], a
    while d != b:  # wraps around the week, so "Sat-Thu" works
        d = (d + 1) % 7
        days.append(d)
    return days


def parse_schedule(text: Optional[str]) -> Optional[Dict[int, tuple]]:
    """'Mon-Fri 10:00-17:00, Sat 10:00-13:00' -> {0: (600, 1020), ..., 5: (600, 780)}
    (minutes after midnight). Returns None when nothing readable is found."""
    if not text:
        return None
    result: Dict[int, tuple] = {}
    closed: set = set()
    pending: List[int] = []  # days listed without hours yet, e.g. "Sun, Tue, Thu 9-5"
    for part in re.split(r"[,;\n،|/]+", text.lower()):
        day_hits = list(re.finditer(rf"({_DAY_RE})(?:{_DASH}({_DAY_RE}))?", part))
        if not day_hits:
            continue
        days: List[int] = []
        for hit in day_hits:
            days += _expand_days(_DAY_WORDS[hit.group(1)], _DAY_WORDS[hit.group(2)] if hit.group(2) else None)
        if re.search(r"closed|off|مغلق|إجازة|اجازة", part):
            closed.update(days)
            pending = []
            continue
        times = re.search(rf"{_TIME_RE}{_DASH}{_TIME_RE}", part[day_hits[-1].end():])
        if not times:
            pending += days
            continue
        days = pending + days
        pending = []
        g = times.groups()
        start = _to_minutes(g[0], g[1], g[2] or (g[5] if g[5] and int(g[0]) <= int(g[3]) else None))
        end = _to_minutes(g[3], g[4], g[5], default_pm=int(g[3]) < int(g[0]) and not g[5])
        if end <= start:
            continue
        for d in days:
            result[d] = (start, end)
    for d in closed:
        result.pop(d, None)
    return result or None


def _fmt_minutes(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def describe_schedule(sched: Optional[Dict[int, tuple]]) -> Optional[List[str]]:
    """Plain list for the agent, e.g. ['السبت 10:00-16:00', 'الاثنين 10:00-16:00']
    in Saudi week order (Sat first)."""
    if not sched:
        return None
    order = [5, 6, 0, 1, 2, 3, 4]
    return [f"{WEEKDAY_NAMES_AR[d]} {_fmt_minutes(sched[d][0])}-{_fmt_minutes(sched[d][1])}" for d in order if d in sched]


def effective_schedule(clinic: dict, dentist) -> Optional[Dict[int, tuple]]:
    """The dentist's hours trimmed to when the clinic is actually open."""
    own = parse_schedule(dentist.availability_schedule)
    general = parse_schedule(clinic.get("general_hours"))
    if not own:
        return general
    if not general:
        return own
    out = {}
    for d, (s1, e1) in own.items():
        if d in general:
            s2, e2 = general[d]
            if max(s1, s2) < min(e1, e2):
                out[d] = (max(s1, s2), min(e1, e2))
    return out or None


def _outside_hours(sched: Optional[Dict[int, tuple]], when: datetime) -> bool:
    if not sched:
        return False  # unknown schedule: don't block
    hours = sched.get(when.weekday())
    if not hours:
        return True
    mins = when.hour * 60 + when.minute
    return not (hours[0] <= mins < hours[1])


def _slot_taken(dentist_id: int, when: datetime, exclude_id: Optional[int] = None) -> bool:
    with db.db() as conn:
        sql = ("SELECT id FROM appointments WHERE dentist_id=? AND appointment_time=? "
               "AND status IN ('scheduled','rescheduled')")
        params: list = [dentist_id, when.isoformat()]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return conn.execute(sql, params).fetchone() is not None


def _problem_at(clinic: dict, dentist, when: datetime, exclude_id: Optional[int] = None) -> Optional[str]:
    """Why this dentist can't take this time (error code), or None if free."""
    if when < datetime.now():
        return "time_in_past"
    if _is_holiday(clinic, when) or _outside_hours(parse_schedule(clinic.get("general_hours")), when):
        return "clinic_closed"
    if _outside_hours(parse_schedule(dentist.availability_schedule), when):
        return "dentist_not_working"
    if _slot_taken(dentist.id, when, exclude_id):
        return "slot_unavailable"
    return None


def _next_free_times(clinic: dict, dentist, after: datetime, count: int = 3,
                     exclude_id: Optional[int] = None) -> List[str]:
    """The next few free half-hour slots for this dentist from `after`
    onwards (within 14 days), so the agent can offer real alternatives
    instead of guessing."""
    start = max(after, datetime.now())
    t = start.replace(second=0, microsecond=0)
    t += timedelta(minutes=(30 - t.minute % 30) % 30)
    found: List[str] = []
    limit = start + timedelta(days=14)
    while t < limit and len(found) < count:
        if not _problem_at(clinic, dentist, t, exclude_id):
            found.append(t.isoformat(timespec="minutes"))
        t += timedelta(minutes=30)
    return found


_PROBLEM_TEXT = {
    "time_in_past": "That time has already passed.",
    "clinic_closed": "The clinic is closed at that time.",
    "dentist_not_working": "This dentist does not work at that day/time.",
    "slot_unavailable": "That time is already booked for this dentist.",
}


def _check_bookable(clinic: dict, dentist_id: int, when: datetime,
                    exclude_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Shared checks for booking/rescheduling. Returns an error dict (with
    real alternative times), or None if fine."""
    dentist = db.get_dentist(dentist_id)
    if not dentist:
        return {"error": "dentist_not_found", "details": "Unknown dentist_id. Call get_clinic_context for the list."}
    problem = _problem_at(clinic, dentist, when, exclude_id)
    if not problem:
        return None
    return {
        "error": problem,
        "details": _PROBLEM_TEXT[problem] + " Offer the caller one of next_free_times instead.",
        "dentist_works": describe_schedule(effective_schedule(clinic, dentist)),
        "next_free_times": _next_free_times(clinic, dentist, when, exclude_id=exclude_id),
    }


def _free_dentists_at(clinic: dict, when: datetime) -> List[Dict[str, Any]]:
    return [
        {"dentist_id": d.id, "name": d.name, "specialization": d.specialization}
        for d in db.get_all_dentists()
        if not _problem_at(clinic, d, when)
    ]


PLACEHOLDER_NAMES = ("", "New Patient")


def _norm_gender(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    if v in ("male", "m", "man", "ذكر", "رجل"):
        return "Male"
    if v in ("female", "f", "woman", "أنثى", "انثى", "امرأة"):
        return "Female"
    return None


def _norm_name(name: str) -> str:
    name = re.sub(r"[ً-ْـ]", "", name or "")  # Arabic diacritics / tatweel
    name = name.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", name).strip().lower()


def _same_person(a: str, b: str) -> bool:
    """Loose match so 'كارين' and 'كارين أحمد' (or a re-spelled name) count
    as the same person, while 'زياد' and 'كارين' don't."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na.split()[0] == nb.split()[0]


def _patients_for_phone(phone: str) -> List[Patient]:
    """Everyone registered under this mobile number. One phone is often
    shared by a family (a wife booking from her husband's phone), so each
    person keeps their own patient record under the same number."""
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM patients WHERE phone_number=? ORDER BY id", (phone,)).fetchall()
    return [Patient(**dict(r)) for r in rows]


def _get_or_create_patient(phone: str, name: Optional[str], gender: Optional[str] = None) -> Patient:
    """The patient record for this caller. Matches by name among the people
    on this number; a new name on a known number becomes a new family
    member rather than overwriting (or booking under) someone else."""
    gender = _norm_gender(gender)
    people = _patients_for_phone(phone)
    chosen: Optional[Patient] = None
    if not name:
        chosen = people[0] if people else None
    else:
        chosen = next((p for p in people if _same_person(p.name, name)), None)
        if not chosen:
            # A WhatsApp-only contact may still be the placeholder "New
            # Patient"; that record becomes this caller.
            chosen = next((p for p in people if p.name in PLACEHOLDER_NAMES), None)
    if not chosen:
        return db.create_patient(Patient(name=name or "New Patient", phone_number=phone, gender=gender))

    updates = {}
    if name and chosen.name in PLACEHOLDER_NAMES:
        updates["name"] = name
    if gender and chosen.gender != gender:
        updates["gender"] = gender
    if updates:
        with db.db() as conn:
            conn.execute(
                f"UPDATE patients SET {', '.join(k + '=?' for k in updates)} WHERE id=?",
                (*updates.values(), chosen.id),
            )
        for k, v in updates.items():
            setattr(chosen, k, v)
    return chosen


def _phone_owns(appointment_id: int, phone: str) -> Optional[dict]:
    """The appointment, if it belongs to anyone registered on this number."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT a.* FROM appointments a JOIN patients p ON a.patient_id = p.id "
            "WHERE a.id=? AND p.phone_number=?", (appointment_id, phone)
        ).fetchone()
    return dict(row) if row else None


def _ensure_call_columns() -> None:
    """The channel/external_ref columns are added by db.init_db when a
    clinic's worker starts; this makes sure they exist even if a voice
    request lands on a database that hasn't been migrated yet."""
    with db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "channel" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'")
        if "external_ref" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN external_ref TEXT")


def _conversation_for_call(call_id: str, patient_id: Optional[int], started_at: Optional[datetime] = None) -> int:
    """Finds (or creates) the ONE dashboard conversation that represents a
    phone call, keyed by the voice platform's call ID. Created already
    'closed' so Sia's WhatsApp worker never mistakes it for an open chat.
    If the call's patient becomes known later, it is filled in."""
    _ensure_call_columns()
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, patient_id FROM conversations WHERE channel='phone' AND external_ref=?", (call_id,)
        ).fetchone()
        if row:
            if patient_id and not row["patient_id"]:
                conn.execute("UPDATE conversations SET patient_id=? WHERE id=?", (patient_id, row["id"]))
            return row["id"]
        start = (started_at or datetime.now()).isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO conversations (patient_id, status, started_at, ended_at, closed_reason, channel, external_ref) "
            "VALUES (?, 'closed', ?, NULL, 'phone_call', 'phone', ?)",
            (patient_id, start, call_id),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Request bodies (these become the tool parameters in ElevenLabs)
# ---------------------------------------------------------------------------

class CheckSlotBody(BaseModel):
    dentist_id: Optional[int] = Field(None, description="Dentist ID from get_clinic_context. Leave empty when the caller has no preference: the reply then lists every dentist free at that time.")
    appointment_time: str = Field(..., description="Requested time, ISO 8601, e.g. 2026-09-27T16:00:00")


class BookBody(BaseModel):
    caller_phone: str = Field(..., description="Caller's mobile number, 10 digits starting 05 (or caller ID).")
    patient_name: str = Field(..., description="Name of the person the appointment is for.")
    patient_gender: Optional[str] = Field(None, description="'male' or 'female' if clear from the conversation, else empty.")
    dentist_id: int
    appointment_time: str = Field(..., description="ISO 8601, e.g. 2026-09-27T16:00:00")
    service_name: Optional[str] = Field(None, description="Service name exactly as listed in get_clinic_context, if known.")
    call_id: Optional[str] = Field(None, description="Filled automatically by ElevenLabs (system__conversation_id).")


class PhoneBody(BaseModel):
    caller_phone: str
    call_id: Optional[str] = Field(None, description="Filled automatically by ElevenLabs (system__conversation_id).")


class CancelBody(BaseModel):
    caller_phone: str
    appointment_id: int


class RescheduleBody(BaseModel):
    caller_phone: str
    appointment_id: int
    new_appointment_time: str = Field(..., description="ISO 8601, e.g. 2026-09-28T18:00:00")


class FlagBody(BaseModel):
    caller_phone: str
    reason: str = Field(..., description="Short, specific note for staff, e.g. 'Caller reports facial swelling and fever'.")
    patient_name: Optional[str] = None
    call_id: Optional[str] = Field(None, description="Filled automatically by ElevenLabs (system__conversation_id).")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{slug}/context")
async def get_clinic_context(slug: str, caller_phone: Optional[str] = None,
                             x_voice_key: Optional[str] = Header(None)):
    """Everything the agent needs at the start of a call: today's date/time,
    clinic hours, address, services & prices, holidays, the dentists (with
    the IDs the booking tools need and the days they work), and -- when the
    call comes from a known number -- who is registered on it."""
    _check_key(x_voice_key)
    clinic = _load_clinic(slug)
    now = datetime.now()
    upcoming_holidays = [h for h in (clinic.get("holidays") or []) if h >= now.strftime("%Y-%m-%d")]
    dentists = [
        {
            "dentist_id": d.id,
            "name": d.name,
            "specialization": d.specialization,
            "languages": d.languages_spoken,
            "works": describe_schedule(effective_schedule(clinic, d)) or d.availability_schedule,
        }
        for d in db.get_all_dentists()
    ]
    result = {
        "now": now.isoformat(timespec="minutes"),
        "today": _day_label(now),
        "clinic_name": clinic["name"],
        "general_hours": clinic.get("general_hours"),
        "address": clinic.get("address"),
        "parking": clinic.get("parking"),
        "upcoming_holidays": upcoming_holidays,
        "services": clinic.get("services") or [],
        "dentists": dentists,
        "caller": _caller_summary(caller_phone, now),
    }
    return result


def _caller_summary(caller_phone: Optional[str], now: datetime) -> Dict[str, Any]:
    """What Mawaid already knows about the number that is calling."""
    phone = normalize_saudi_phone(caller_phone or "")
    if not phone:
        return {"known": False, "phone": None,
                "note": "No usable caller ID. Ask for the caller's mobile number when needed."}
    people = [p for p in _patients_for_phone(phone) if p.name not in PLACEHOLDER_NAMES]
    if not people:
        return {"known": False, "phone": _local_phone(phone),
                "note": "New number, not registered yet. You may book on this number after confirming with the caller."}
    with db.db() as conn:
        rows = conn.execute(
            "SELECT a.id, a.appointment_time, p.name AS patient, d.name AS dentist, a.service_name "
            "FROM appointments a JOIN patients p ON a.patient_id=p.id JOIN dentists d ON a.dentist_id=d.id "
            "WHERE p.phone_number=? AND a.status IN ('scheduled','rescheduled') AND a.appointment_time>=? "
            "ORDER BY a.appointment_time",
            (phone, now.isoformat()),
        ).fetchall()
    return {
        "known": True,
        "phone": _local_phone(phone),
        "people_on_this_number": [{"name": p.name, "gender": (p.gender or "unknown").lower()} for p in people],
        "upcoming_appointments": [
            {"appointment_id": r["id"], "for": r["patient"],
             "day": _day_label(datetime.fromisoformat(r["appointment_time"])),
             "time": datetime.fromisoformat(r["appointment_time"]).strftime("%H:%M"),
             "dentist": r["dentist"], "service": r["service_name"]}
            for r in rows
        ],
        "note": "Several family members may share one phone. Don't assume who is calling: "
                "confirm the name before booking or discussing an appointment.",
    }


@router.post("/{slug}/check-slot")
async def check_slot(slug: str, body: CheckSlotBody, x_voice_key: Optional[str] = Header(None)):
    """Is this dentist free at this time? If not, returns the real reason
    and the next free times. Without a dentist_id, lists every dentist free
    at that time (for callers who don't mind which dentist)."""
    _check_key(x_voice_key)
    clinic = _load_clinic(slug)
    when = _parse_time(body.appointment_time)
    if not when:
        return {"error": "bad_time_format", "details": "Use ISO 8601, e.g. 2026-09-27T16:00:00"}
    if not body.dentist_id:
        free = _free_dentists_at(clinic, when)
        if free:
            return {"available": True, "day": _day_label(when), "time": when.strftime("%H:%M"),
                    "free_dentists": free,
                    "details": "Pick the first one that suits the service unless the caller asks otherwise."}
        # Nobody free: suggest the soonest times per dentist.
        options = []
        for d in db.get_all_dentists():
            times = _next_free_times(clinic, d, when, count=2)
            if times:
                options.append({"dentist_id": d.id, "name": d.name, "next_free_times": times})
        options.sort(key=lambda o: o["next_free_times"][0])
        return {"available": False, "error": "no_dentist_free",
                "details": "No dentist is free at that time. Offer the earliest of these.",
                "alternatives": options[:3]}
    problem = _check_bookable(clinic, body.dentist_id, when)
    if problem:
        problem["available"] = False
        return problem
    return {"available": True, "day": _day_label(when), "time": when.strftime("%H:%M")}


@router.post("/{slug}/book")
async def book(slug: str, body: BookBody, x_voice_key: Optional[str] = Header(None)):
    _check_key(x_voice_key)
    clinic = _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    when = _parse_time(body.appointment_time)
    if not when:
        return {"error": "bad_time_format", "details": "Use ISO 8601, e.g. 2026-09-27T16:00:00"}
    problem = _check_bookable(clinic, body.dentist_id, when)
    if problem:
        return problem

    patient = _get_or_create_patient(phone, body.patient_name, body.patient_gender)
    service_name, price = _match_service(clinic, body.service_name)
    appt = db.create_appointment(Appointment(
        patient_id=patient.id,
        dentist_id=body.dentist_id,
        appointment_time=when,
        status="scheduled",
        service_name=service_name,
        price_sar=price,
    ))
    if body.call_id:
        _conversation_for_call(body.call_id, patient.id)
    dentist = db.get_dentist(body.dentist_id)
    logger.info("[%s] Voice booking: appointment %s for patient %s at %s", slug, appt.id, patient.id, when)
    return {
        "status": "booked",
        "appointment_id": appt.id,
        "patient_name": patient.name,
        "phone": _local_phone(phone),
        "dentist": dentist.name if dentist else None,
        "day": _day_label(when),
        "time": when.strftime("%H:%M"),
        "service": service_name,
    }


@router.post("/{slug}/appointments")
async def find_appointments(slug: str, body: PhoneBody, x_voice_key: Optional[str] = Header(None)):
    """The caller's upcoming appointments (needed before cancelling or rescheduling)."""
    _check_key(x_voice_key)
    _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    people = _patients_for_phone(phone)
    if not people:
        return {"appointments": [], "details": "No patient found with this number."}
    now = datetime.now()
    upcoming = []
    for person in people:
        for a in db.get_patient_appointments(person.id):
            if a.status in ("scheduled", "rescheduled") and a.appointment_time >= now:
                upcoming.append({
                    "appointment_id": a.appointment_id,
                    "for": person.name,
                    "day": _day_label(a.appointment_time),
                    "time": a.appointment_time.strftime("%H:%M"),
                    "dentist": a.dentist_name,
                    "service": a.service_name,
                    "_sort": a.appointment_time,
                })
    upcoming.sort(key=lambda x: x.pop("_sort"))
    return {"people_on_this_number": [p.name for p in people if p.name not in PLACEHOLDER_NAMES],
            "appointments": upcoming}


@router.post("/{slug}/cancel")
async def cancel(slug: str, body: CancelBody, x_voice_key: Optional[str] = Header(None)):
    _check_key(x_voice_key)
    _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    # Only let a caller cancel an appointment that belongs to their own number.
    appt = _phone_owns(body.appointment_id, phone)
    if not appt or appt["status"] not in ("scheduled", "rescheduled"):
        return {"error": "not_found", "details": "No active appointment with that ID for this caller's number."}
    db.update_appointment_status(body.appointment_id, "cancelled")
    logger.info("[%s] Voice cancel: appointment %s", slug, body.appointment_id)
    return {"status": "cancelled", "appointment_id": body.appointment_id}


@router.post("/{slug}/reschedule")
async def reschedule(slug: str, body: RescheduleBody, x_voice_key: Optional[str] = Header(None)):
    _check_key(x_voice_key)
    clinic = _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    when = _parse_time(body.new_appointment_time)
    if not when:
        return {"error": "bad_time_format", "details": "Use ISO 8601, e.g. 2026-09-27T16:00:00"}
    appt = _phone_owns(body.appointment_id, phone)
    if not appt or appt["status"] not in ("scheduled", "rescheduled"):
        return {"error": "not_found", "details": "No active appointment with that ID for this caller's number."}

    problem = _check_bookable(clinic, appt["dentist_id"], when, exclude_id=body.appointment_id)
    if problem:
        return problem

    with db.db() as conn:
        conn.execute(
            "UPDATE appointments SET appointment_time=?, status='rescheduled' WHERE id=?",
            (when.isoformat(), body.appointment_id),
        )
    logger.info("[%s] Voice reschedule: appointment %s -> %s", slug, body.appointment_id, when)
    return {"status": "rescheduled", "appointment_id": body.appointment_id, "day": _day_label(when), "time": when.strftime("%H:%M")}


@router.post("/{slug}/flag")
async def flag_for_staff(slug: str, body: FlagBody, x_voice_key: Optional[str] = Header(None)):
    """Puts this call on the dashboard's "Needs attention" list, the same
    place Sia's WhatsApp flags go, so reception can call the patient back."""
    _check_key(x_voice_key)
    _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    patient = _get_or_create_patient(phone, body.patient_name)
    if body.call_id:
        # The full transcript arrives after the call (see post_call below)
        # and lands in this same conversation.
        conversation_id = _conversation_for_call(body.call_id, patient.id)
    else:
        _ensure_call_columns()
        with db.db() as conn:
            conversation_id = conn.execute(
                "INSERT INTO conversations (patient_id, status, started_at, closed_reason, channel) "
                "VALUES (?, 'closed', ?, 'phone_call', 'phone')",
                (patient.id, datetime.now().isoformat(timespec="seconds")),
            ).lastrowid
        db.add_message(conversation_id, sender="agent", message=f"📞 Phone call — flagged for staff: {body.reason}")
    db.flag_conversation(conversation_id, f"📞 {body.reason}")
    logger.warning("[%s] Voice flag for patient %s: %s", slug, patient.id, body.reason)
    return {"status": "flagged"}


# ---------------------------------------------------------------------------
# Post-call webhook: the whole call transcript, sent by ElevenLabs after
# every call, stored as a 📞 conversation on the clinic's dashboard.
# ---------------------------------------------------------------------------

def _verify_elevenlabs_signature(raw_body: bytes, header: Optional[str]) -> None:
    """ElevenLabs signs each webhook as `t=<unix time>,v0=<hex HMAC-SHA256
    of "<t>.<body>">` using the webhook's shared secret (VOICE_WEBHOOK_SECRET
    in Railway). Rejects anything unsigned, forged, or older than 30 minutes."""
    secret = os.environ.get("VOICE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Voice webhook is not enabled (VOICE_WEBHOOK_SECRET is not set).")
    if not header:
        raise HTTPException(status_code=401, detail="Missing signature.")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig:
        raise HTTPException(status_code=401, detail="Malformed signature.")
    try:
        if abs(time.time() - int(ts)) > 30 * 60:
            raise HTTPException(status_code=401, detail="Signature too old.")
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed signature timestamp.")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=401, detail="Invalid signature.")


def _pick_clinic_for_call(data: dict) -> Optional[dict]:
    """Which clinic a call belongs to: an explicit clinic_slug dynamic
    variable if the call carried one, else VOICE_DEFAULT_CLINIC_SLUG, else
    the only active clinic (pilot setups)."""
    dyn = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
    phone_call = (data.get("metadata") or {}).get("phone_call") or {}
    dialled = (dyn.get("system__called_number") or phone_call.get("agent_number")
               or phone_call.get("called_number") or phone_call.get("to_number"))
    key = dyn.get("clinic_slug") or dialled
    if key or os.environ.get("VOICE_DEFAULT_CLINIC_SLUG"):
        clinic = resolve_clinic(key)
        return clinic if clinic and clinic.get("active") and clinic.get("voice_enabled") else None
    active = [c for c in clinics_store.list_clinics() if c.get("active") and c.get("voice_enabled")]
    return active[0] if len(active) == 1 else None


def _caller_phone_from_call(data: dict) -> Optional[str]:
    """Caller ID when the call came in over a real phone line; otherwise
    the mobile number the caller confirmed, taken from the agent's own tool
    calls during the call (book / find_appointments / flag)."""
    phone_call = (data.get("metadata") or {}).get("phone_call") or {}
    for key in ("external_number", "caller_id", "from_number"):
        normalized = normalize_saudi_phone(str(phone_call.get(key) or ""))
        if normalized:
            return normalized
    for turn in data.get("transcript") or []:
        for tc in turn.get("tool_calls") or []:
            params = tc.get("params_as_json")
            try:
                params = json.loads(params) if isinstance(params, str) else (params or {})
            except ValueError:
                params = {}
            normalized = normalize_saudi_phone(str(params.get("caller_phone") or ""))
            if normalized:
                return normalized
    return None


def _patient_name_from_call(data: dict) -> Optional[str]:
    """The name the agent booked or flagged under during the call, so a
    call from a shared family phone lands on the right person."""
    for turn in data.get("transcript") or []:
        for tc in turn.get("tool_calls") or []:
            params = tc.get("params_as_json")
            try:
                params = json.loads(params) if isinstance(params, str) else (params or {})
            except ValueError:
                params = {}
            if params.get("patient_name"):
                return str(params["patient_name"])
    return None


@router.post("/post-call")
async def post_call(request: Request):
    raw = await request.body()
    _verify_elevenlabs_signature(raw, request.headers.get("elevenlabs-signature"))
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.")

    if payload.get("type") != "post_call_transcription":
        return {"status": "ignored"}  # audio / failure events: nothing to store

    data = payload.get("data") or {}
    call_id = data.get("conversation_id")
    clinic = _pick_clinic_for_call(data)
    if not call_id or not clinic:
        logger.warning("Post-call webhook for call %s: no matching clinic, not stored.", call_id)
        return {"status": "ignored", "reason": "no_clinic"}
    db.set_current_db_path(clinic["db_path"])

    metadata = data.get("metadata") or {}
    start_ts = metadata.get("start_time_unix_secs")
    started_at = datetime.fromtimestamp(start_ts) if start_ts else datetime.now()
    duration = metadata.get("call_duration_secs") or 0

    phone = _caller_phone_from_call(data)
    patient = _get_or_create_patient(phone, _patient_name_from_call(data)) if phone else None
    conversation_id = _conversation_for_call(call_id, patient.id if patient else None, started_at)

    with db.db() as conn:
        already = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        if already:
            return {"status": "duplicate", "conversation_id": conversation_id}
        conn.execute(
            "UPDATE conversations SET started_at=?, ended_at=?, status='closed', closed_reason='phone_call' WHERE id=?",
            (started_at.isoformat(timespec="seconds"),
             (started_at + timedelta(seconds=duration)).isoformat(timespec="seconds"),
             conversation_id),
        )
        rows = []
        for turn in data.get("transcript") or []:
            text = (turn.get("message") or "").strip()
            if not text:
                continue  # tool-call-only turns
            sender = "user" if turn.get("role") == "user" else "agent"
            at = started_at + timedelta(seconds=turn.get("time_in_call_secs") or 0)
            rows.append((conversation_id, sender, text, at.isoformat(timespec="seconds")))
        conn.executemany(
            "INSERT INTO messages (conversation_id, sender, message, created_at) VALUES (?, ?, ?, ?)", rows
        )

    logger.info("[%s] Stored phone call %s as conversation %s (%d lines)", clinic["slug"], call_id, conversation_id, len(rows))
    return {"status": "stored", "conversation_id": conversation_id}
