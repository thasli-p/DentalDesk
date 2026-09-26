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
import hmac
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
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


def _load_clinic(slug: str) -> dict:
    """Finds the clinic by slug and points shared/db.py at its own database
    for the rest of this request (same pattern as the staff dashboard)."""
    clinic = clinics_store.get_clinic_by_slug(slug)
    if not clinic or not clinic.get("active"):
        raise HTTPException(status_code=404, detail="Clinic not found or inactive.")
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


def _booked_times(dentist_id: int, day: datetime) -> List[str]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    with db.db() as conn:
        rows = conn.execute(
            "SELECT appointment_time FROM appointments "
            "WHERE dentist_id=? AND status IN ('scheduled','rescheduled') "
            "AND appointment_time >= ? AND appointment_time < ? ORDER BY appointment_time",
            (dentist_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return [r["appointment_time"] for r in rows]


def _slot_taken(dentist_id: int, when: datetime, exclude_id: Optional[int] = None) -> bool:
    with db.db() as conn:
        sql = ("SELECT id FROM appointments WHERE dentist_id=? AND appointment_time=? "
               "AND status IN ('scheduled','rescheduled')")
        params: list = [dentist_id, when.isoformat()]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return conn.execute(sql, params).fetchone() is not None


def _check_bookable(clinic: dict, dentist_id: int, when: datetime) -> Optional[Dict[str, Any]]:
    """Shared checks for booking/rescheduling. Returns an error dict, or None if fine."""
    if not db.get_dentist(dentist_id):
        return {"error": "dentist_not_found", "details": "Unknown dentist_id. Call get_clinic_context for the list."}
    if when < datetime.now():
        return {"error": "time_in_past", "details": "That time has already passed. Offer a future time."}
    if _is_holiday(clinic, when):
        return {"error": "clinic_closed", "details": f"The clinic is closed on {_day_label(when)} (holiday)."}
    if _slot_taken(dentist_id, when):
        return {
            "error": "slot_unavailable",
            "details": "That time is already booked for this dentist.",
            "booked_times_that_day": _booked_times(dentist_id, when),
        }
    return None


def _get_or_create_patient(phone: str, name: Optional[str]) -> Patient:
    patient = db.get_patient_by_phone(phone)
    if patient:
        # A WhatsApp-only contact may still be the placeholder "New Patient";
        # fill in the real name now that the caller has given it.
        if name and patient.name in ("", "New Patient"):
            with db.db() as conn:
                conn.execute("UPDATE patients SET name=? WHERE id=?", (name, patient.id))
            patient.name = name
        return patient
    return db.create_patient(Patient(name=name or "New Patient", phone_number=phone))


def _patient_owns(appointment_id: int, patient_id: int) -> Optional[dict]:
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id=? AND patient_id=?", (appointment_id, patient_id)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Request bodies (these become the tool parameters in ElevenLabs)
# ---------------------------------------------------------------------------

class CheckSlotBody(BaseModel):
    dentist_id: int = Field(..., description="Dentist ID from get_clinic_context.")
    appointment_time: str = Field(..., description="Requested time, ISO 8601, e.g. 2026-09-27T16:00:00")


class BookBody(BaseModel):
    caller_phone: str = Field(..., description="Caller's mobile number, 10 digits starting 05 (or caller ID).")
    patient_name: str = Field(..., description="Caller's full name.")
    dentist_id: int
    appointment_time: str = Field(..., description="ISO 8601, e.g. 2026-09-27T16:00:00")
    service_name: Optional[str] = Field(None, description="Service name exactly as listed in get_clinic_context, if known.")


class PhoneBody(BaseModel):
    caller_phone: str


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{slug}/context")
async def get_clinic_context(slug: str, x_voice_key: Optional[str] = Header(None)):
    """Everything the agent needs at the start of a call: today's date/time,
    clinic hours, address, services & prices, holidays, and the dentists
    (with the IDs the booking tools need)."""
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
            "availability": d.availability_schedule,
        }
        for d in db.get_all_dentists()
    ]
    return {
        "now": now.isoformat(timespec="minutes"),
        "today": _day_label(now),
        "clinic_name": clinic["name"],
        "general_hours": clinic.get("general_hours"),
        "address": clinic.get("address"),
        "parking": clinic.get("parking"),
        "upcoming_holidays": upcoming_holidays,
        "services": clinic.get("services") or [],
        "dentists": dentists,
    }


@router.post("/{slug}/check-slot")
async def check_slot(slug: str, body: CheckSlotBody, x_voice_key: Optional[str] = Header(None)):
    """Is this dentist free at this time? Also returns what's already booked
    that day so the agent can offer a nearby free time straight away."""
    _check_key(x_voice_key)
    clinic = _load_clinic(slug)
    when = _parse_time(body.appointment_time)
    if not when:
        return {"error": "bad_time_format", "details": "Use ISO 8601, e.g. 2026-09-27T16:00:00"}
    problem = _check_bookable(clinic, body.dentist_id, when)
    if problem:
        problem["available"] = False
        return problem
    return {"available": True, "day": _day_label(when), "booked_times_that_day": _booked_times(body.dentist_id, when)}


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

    patient = _get_or_create_patient(phone, body.patient_name)
    service_name, price = _match_service(clinic, body.service_name)
    appt = db.create_appointment(Appointment(
        patient_id=patient.id,
        dentist_id=body.dentist_id,
        appointment_time=when,
        status="scheduled",
        service_name=service_name,
        price_sar=price,
    ))
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
    patient = db.get_patient_by_phone(phone)
    if not patient:
        return {"appointments": [], "details": "No patient found with this number."}
    now = datetime.now()
    upcoming = [
        {
            "appointment_id": a.appointment_id,
            "day": _day_label(a.appointment_time),
            "time": a.appointment_time.strftime("%H:%M"),
            "dentist": a.dentist_name,
            "service": a.service_name,
        }
        for a in db.get_patient_appointments(patient.id)
        if a.status in ("scheduled", "rescheduled") and a.appointment_time >= now
    ]
    return {"patient_name": patient.name, "appointments": upcoming}


@router.post("/{slug}/cancel")
async def cancel(slug: str, body: CancelBody, x_voice_key: Optional[str] = Header(None)):
    _check_key(x_voice_key)
    _load_clinic(slug)
    phone = normalize_saudi_phone(body.caller_phone)
    if not phone:
        return _bad_phone(body.caller_phone)
    patient = db.get_patient_by_phone(phone)
    # Only let a caller cancel an appointment that belongs to their own number.
    appt = _patient_owns(body.appointment_id, patient.id) if patient else None
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
    patient = db.get_patient_by_phone(phone)
    appt = _patient_owns(body.appointment_id, patient.id) if patient else None
    if not appt or appt["status"] not in ("scheduled", "rescheduled"):
        return {"error": "not_found", "details": "No active appointment with that ID for this caller's number."}

    dentist_id = appt["dentist_id"]
    if when < datetime.now():
        return {"error": "time_in_past", "details": "That time has already passed. Offer a future time."}
    if _is_holiday(clinic, when):
        return {"error": "clinic_closed", "details": f"The clinic is closed on {_day_label(when)} (holiday)."}
    if _slot_taken(dentist_id, when, exclude_id=body.appointment_id):
        return {"error": "slot_unavailable", "booked_times_that_day": _booked_times(dentist_id, when)}

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
    conversation = db.create_conversation(patient.id)
    db.add_message(conversation.id, sender="agent", message=f"📞 Phone call — flagged for staff: {body.reason}")
    db.flag_conversation(conversation.id, f"📞 Phone call: {body.reason}")
    db.close_conversation(conversation.id, reason="phone_call")
    logger.warning("[%s] Voice flag for patient %s: %s", slug, patient.id, body.reason)
    return {"status": "flagged"}
