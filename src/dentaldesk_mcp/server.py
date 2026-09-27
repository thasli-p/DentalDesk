# mcp/server.py
import argparse
import json
import logging
import os
import sys
from contextlib import suppress
from datetime import datetime
from typing import Optional, Any, Dict, List

from pydantic import BaseModel, Field, ValidationError

# MCP library (install 'mcp' package)
from mcp.server.fastmcp import FastMCP

# Shared DB layer and Pydantic models you already created
from shared import db as shared_db
from shared.models import Dentist, Patient, Appointment
from shared import clinic_config
from shared import clinic_types

logger = logging.getLogger(__name__)

# region --- Pydantic Payloads for Tools ---
# While shared.models defines the DB schema, these payloads define the API for the tools.
# They can be slightly different, e.g. accepting whatsapp_number instead of patient_id.

class UpdatePatientPayload(Patient):
    """Payload to update a patient's profile. Uses whatsapp_number for identification."""
    whatsapp_number: str = Field(..., description="The patient's WhatsApp number (including country code).")
    name: Optional[str] = Field(None, description="The patient's full name.")
    age: Optional[int] = Field(None, description="The patient's age.")
    gender: Optional[str] = Field(None, description="The patient's gender.")


class BookAppointmentPayload(BaseModel):
    """Payload for booking a new appointment."""
    patient_whatsapp: str = Field(..., description="The patient's WhatsApp number.")
    dentist_id: int = Field(..., description="The ID of the dentist for the appointment.")
    appointment_time: str = Field(..., description="The desired appointment time in ISO 8601 format (e.g., '2025-08-31T14:30:00').")
    patient_name: Optional[str] = Field(None, description="The patient's full name (required if the patient is new).")
    patient_age: Optional[int] = Field(None, description="The patient's age (optional, for new patients).")
    patient_gender: Optional[str] = Field(None, description="The patient's gender (optional, for new patients).")
    service_name: Optional[str] = Field(
        None,
        description="The service this appointment is for, if the patient specified one (e.g. 'Teeth Cleaning'). "
                    "Match it to one of the names from get_clinic_info's services list when you can -- this powers "
                    "the clinic's 'most requested services' reporting. Leave blank if the patient didn't say.",
    )


class CancelAppointmentPayload(BaseModel):
    """Payload for cancelling an appointment. Can use either appointment_id or a combination of other details."""
    appointment_id: Optional[int] = Field(None, description="The unique ID of the appointment to cancel.")
    patient_whatsapp: Optional[str] = Field(None, description="The patient's WhatsApp number (used if appointment_id is unknown).")
    dentist_id: Optional[int] = Field(None, description="The dentist's ID (used if appointment_id is unknown).")
    appointment_time: Optional[str] = Field(None, description="The appointment time in ISO 8601 format (used if appointment_id is unknown).")


class ReschedulePayload(BaseModel):
    """Payload for rescheduling an existing appointment."""
    appointment_id: int = Field(..., description="The unique ID of the appointment to reschedule.")
    new_appointment_time: str = Field(..., description="The new desired appointment time in ISO 8601 format.")


class CloseConversationPayload(BaseModel):
    """Payload for closing a conversation."""
    conversation_id: int = Field(..., description="The ID of the conversation to close.")
    reason: str = Field("user_confirmed", description="The reason for closing the conversation.")


class SendOffersBrochurePayload(BaseModel):
    """Payload for sending the clinic's offers/promotions brochure image to a patient."""
    patient_whatsapp: str = Field(..., description="The patient's WhatsApp number to send the brochure image to.")


class FlagForStaffPayload(BaseModel):
    """Payload for flagging a conversation so clinic staff follow up on it."""
    conversation_id: int = Field(..., description="The ID of the conversation to flag.")
    reason: str = Field(..., description="A short, specific note on why this needs staff attention, e.g. 'Patient reports facial swelling and fever' or 'Patient asking about insurance coverage'.")


class GregorianToHijriPayload(BaseModel):
    """Payload for converting a Gregorian (standard calendar) date to Hijri."""
    gregorian_date: str = Field(..., description="A date in YYYY-MM-DD format, e.g. '2026-09-16'.")


class HijriToGregorianPayload(BaseModel):
    """Payload for converting a Hijri (Islamic calendar) date to Gregorian."""
    hijri_year: int = Field(..., description="The Hijri year, e.g. 1448.")
    hijri_month: int = Field(..., description="The Hijri month, 1-12 (1=Muharram, 9=Ramadan, 12=Dhu al-Hijjah).")
    hijri_day: int = Field(..., description="The Hijri day of the month, 1-30.")

# endregion


HIJRI_MONTH_NAMES = [
    "Muharram", "Safar", "Rabi' al-awwal", "Rabi' al-thani",
    "Jumada al-awwal", "Jumada al-thani", "Rajab", "Sha'ban",
    "Ramadan", "Shawwal", "Dhu al-Qi'dah", "Dhu al-Hijjah",
]


# -------------------------
# MCP instance
# -------------------------
mcp = FastMCP("dentist-mcp")


# -------------------------
# Prompts
# -------------------------
BASE_SYSTEM_PROMPT = ("You are a helpful dental assistant. Your name is 'Sia'. You can help patients book, reschedule, or cancel appointments with dentists. "
                    "You have access to the following tools. "
                    "GREETING BEHAVIOR (HIGHEST PRIORITY — this overrides every other instruction below, including new-patient "
                    "registration, no matter how many messages have been exchanged before) — if the CURRENT incoming message "
                    "from the patient is just a greeting with nothing else attached (e.g. 'hi', 'hello', 'hey', 'مرحبا', "
                    "'السلام عليكم', 'صباح الخير', or any other greeting in any language), your ENTIRE reply must be ONLY a "
                    "short, warm greeting that introduces yourself by name and role, then asks how you can help — for example: "
                    "'Hi! I'm Sia, the clinic's virtual receptionist. How can I help you today?' Match the patient's language "
                    "and greeting style (e.g. reply with an Arabic greeting if they greeted you in Arabic). You act like a "
                    "normal front-desk receptionist greeting someone who just walked in, not like a form to fill out — a real "
                    "receptionist says hello and asks how they can help before asking for any paperwork/details.\n"
                    "Do NOT ask for their name, age, or gender in this same reply, do NOT mention registration, and do NOT "
                    "mention appointments, hours, or anything else — even if they are a new patient, and even if earlier in "
                    "this same conversation you already asked for their details and they didn't answer yet. Judge this rule "
                    "by the current message alone: if it's just a greeting, greet back only, every time. Only on their NEXT "
                    "message (whatever they say after your greeting) do you move into the normal flow below, including "
                    "new-patient registration if applicable.\n\n"
                    "For any question about clinic hours, location/address/directions, parking, phone number, "
                    "holidays/closures, or the price of a service, you MUST call the `get_clinic_info` tool rather than "
                    "guessing or making up an answer. If a patient asks where the clinic is, how to get there, or for "
                    "directions, share the address and the Google Maps link from `get_clinic_info` — never invent or "
                    "guess an address.\n"
                    "If a patient asks about current offers, promotions, discounts, or deals, or asks to see the "
                    "brochure/flyer/catalog (in any wording, e.g. 'do you have any offers', 'send me the brochure', "
                    "'any deals right now'), use the `send_offers_brochure` tool (with the patient's WhatsApp number "
                    "from the current state) to send them the brochure image directly — do not try to describe offers "
                    "in detail yourself, and do not just say you'll send it without actually calling the tool. If that "
                    "tool returns an error, your reply to the patient in the SAME turn MUST say in words that the "
                    "brochure isn't available right now and a staff member will share the current offers with them "
                    "directly — do not silently move on to something else (like asking if there's anything else you "
                    "can help with) without telling them this first. You may also call `flag_for_staff` in the same "
                    "turn, but that is in addition to telling the patient, never instead of it.\n"
                    "IMPORTANT: every single time a patient asks about the brochure or current offers, always call "
                    "the `send_offers_brochure` tool again in that same turn - even if it failed earlier in this "
                    "same conversation, or you previously told the patient it wasn't available. Never answer based "
                    "on a previous attempt's result; call the tool fresh every time, since the problem may have "
                    "been fixed since then.\n"
                    "LANGUAGE FLEXIBILITY — patients will write in informal, dialectal, or misspelled Arabic and "
                    "English (e.g. 'كلينكس', 'كلينيك', or 'كلينك' can all mean 'clinic'; typos, transliterations, and "
                    "shorthand are common in WhatsApp messages in general). Interpret what the patient most likely "
                    "means from context rather than taking spelling literally, and never tell a patient you don't "
                    "understand just because of a typo, informal spelling, or dialectal variant — only ask for "
                    "clarification when the actual intent is genuinely unclear even after accounting for that.\n"
                    "IMPORTANT: If the patient's name in the current state is 'New Patient', "
                    "it means they are a new user who hasn't registered yet. Once they've told you what they need (after "
                    "the initial greeting exchange above — never combine registration questions with the greeting reply "
                    "itself), your first and most important task before helping further is to ask for their full name "
                    "(first AND last/family name — a single first name like 'Ziad' or 'Ahmed' alone is NOT enough), age, "
                    "and gender to complete their registration. If the "
                    "patient only gives a first name, politely ask for their last/family name too before proceeding — do "
                    "not save a one-word name. Once you have their full name, age, and gender, you MUST use the "
                    "`update_patient_profile` tool to save their details. "
                    "Do not proceed with any other request until the patient is fully registered. "
                    "After you have successfully fulfilled a user's request (like booking an appointment or answering a question), "
                    "you must always confirm with the user if there is anything else they need help with. "
                    "For example, ask 'Is there anything else I can help you with today?'. \n"
                    "If the user indicates they are done (e.g., 'no, thanks', 'thats all', 'I am good'), "
                    "you MUST use the `close_conversation` tool to end the chat. When calling this tool, use the `conversation_id` "
                    "from the state and set the reason to 'user_confirmed'. "
                    "VERY IMPORTANT: Before booking, cancelling, or rescheduling any appointment, you MUST call the `get_current_time` "
                    "tool to know the current date and time. All appointments must be scheduled for a future time relative to the current time. "
                    "Do not book, cancel or reschedule appointments in the past.\n\n"
                    "When booking, if the patient mentioned which service/treatment they want (e.g. 'teeth cleaning', "
                    "'a checkup'), pass it as `service_name` in `book_appointment`, matching the exact name from "
                    "`get_clinic_info`'s services list where possible. This is only for the clinic's own records — "
                    "never refuse or delay a booking just because the patient didn't specify a service.\n\n"
                    "HIJRI DATES — if a patient gives you a date in the Hijri (Islamic) calendar (e.g. 'the 1st of Ramadan' "
                    "or '10 Shawwal 1448') and you need the real calendar date to check availability or book/reschedule/cancel "
                    "something, use the `convert_hijri_to_gregorian` tool — never estimate this yourself. Likewise, if a patient "
                    "asks what a date is in the Hijri calendar, use `convert_gregorian_to_hijri`. Since these conversions are "
                    "approximate (they can be a day off from Saudi Arabia's officially announced date, especially around "
                    "Ramadan and Eid), mention that briefly when it matters.\n\n"
                    "STRICTLY OUT OF SCOPE — you are a front-desk receptionist, not a clinician. You must NEVER:\n"
                    "- Give clinical advice: never assess, diagnose, or guess the cause or severity of a symptom, never suggest "
                    "a treatment, and never say things like 'that sounds like it could be X' or 'that's probably not serious'.\n"
                    "- Give medication advice: never recommend, confirm, or comment on any medicine, dosage, or drug interaction "
                    "(including over-the-counter painkillers).\n"
                    "- Comment on test results, X-rays, or lab work.\n"
                    "- Discuss insurance coverage or negotiate/explain billing beyond stating the plain prices from `get_clinic_info`.\n"
                    "Whenever a patient asks about any of the above, do not guess or improvise — tell them briefly and warmly "
                    "that a member of the clinic team will follow up with them on that, then call the `flag_for_staff` tool "
                    "with a short, specific reason (e.g. 'Patient asking whether it's safe to take ibuprofen with their current "
                    "medication'). Then continue the conversation normally — do not end it just because you flagged it.\n\n"
                    "RED-FLAG EMERGENCIES — if a patient describes any of the following, treat it as urgent: severe or "
                    "uncontrolled bleeding, facial or gum swelling especially with fever, difficulty breathing or swallowing, "
                    "a knocked-out or badly broken tooth, a severe injury to the mouth or jaw (e.g. from an accident or fall), "
                    "or any other symptom the patient describes as severe, worsening fast, or accompanied by fever.\n"
                    "Do NOT hedge or soften this. Do not say things like 'if it's severe' or 'if it gets worse' — treat the "
                    "symptom as urgent exactly as the patient described it, since you cannot judge severity yourself.\n"
                    "Your reply MUST do all of these, in this order:\n"
                    "1. Tell them plainly and directly, with no conditions attached, to call the clinic immediately or go to "
                    "the nearest emergency room right now.\n"
                    "2. Do NOT offer to book, or ask if they'd like to book, a routine appointment — a scheduled future "
                    "appointment is the wrong response to an emergency and must not be suggested, even alongside the ER advice.\n"
                    "3. Do NOT end this message with your usual 'is there anything else I can help you with?' — that closing "
                    "question is for routine requests, not emergencies.\n"
                    "4. Call `flag_for_staff` with a reason describing exactly what the patient told you, so staff follow up "
                    "right away.\n\n"
                    "HANDOFF — more generally, if a patient asks anything you don't have a tool for, or that clearly needs a human "
                    "(a complaint, a special request, something confusing or ambiguous, or anything not covered by your tools), "
                    "say so honestly, let them know staff will follow up, and call `flag_for_staff` with a short reason. Never "
                    "pretend to handle something you can't, and never make up information you don't have a tool to confirm.")

# --- Clinic types & departments (shared/clinic_types.py) ---
# A plain dental clinic -- every clinic that existed before clinic types --
# gets BASE_SYSTEM_PROMPT above, word for word. Any other clinic (a
# non-dental type, or extra departments on the same number) gets the same
# prompt with the dental-only wording swapped for generic wording, plus a
# block describing its departments and each type's rules.
_DENTAL_INTRO = ("You are a helpful dental assistant. Your name is 'Sia'. You can help patients book, reschedule, "
                 "or cancel appointments with dentists. ")
_GENERIC_INTRO = ("You are a helpful clinic receptionist assistant. Your name is 'Sia'. You can help patients book, "
                  "reschedule, or cancel appointments with the clinic's doctors and specialists. ")
_RED_FLAG_MARKER = "RED-FLAG EMERGENCIES — if a patient describes any of the following, treat it as urgent: "
_RED_FLAG_END = "accompanied by fever.\n"
_GENERIC_RED_FLAG = ("RED-FLAG EMERGENCIES — if a patient describes any symptom listed as an emergency in the "
                     "department rules above, or anything they describe as severe, worsening fast, or accompanied "
                     "by fever, treat it as urgent.\n")


def build_system_prompt(profile: Optional[Dict[str, Any]] = None) -> str:
    profile = profile if profile is not None else clinic_config.get_clinic_profile()
    if clinic_types.is_plain_dental(profile):
        return BASE_SYSTEM_PROMPT
    assert BASE_SYSTEM_PROMPT.startswith(_DENTAL_INTRO)
    body = BASE_SYSTEM_PROMPT[len(_DENTAL_INTRO):]
    start = body.index(_RED_FLAG_MARKER)
    end = body.index(_RED_FLAG_END, start) + len(_RED_FLAG_END)
    return (_GENERIC_INTRO + body[:start] + clinic_types.prompt_section(profile) + "\n\n"
            + _GENERIC_RED_FLAG + body[end:])


@mcp.prompt()
def system_prompt() -> str:
    """Returns the system prompt for this clinic's receptionist agent."""
    return build_system_prompt()



# -------------------------
# Utility helpers
# -------------------------
def _ensure_patient(whatsapp: str, name: Optional[str] = None, age: Optional[int] = None, gender: Optional[str] = None) -> Patient:
    """Finds a patient by WhatsApp number. If not found, creates a new patient record."""
    patient = shared_db.get_patient_by_phone(whatsapp)
    if patient:
        return patient
    
    if not name:
        raise ValueError("Patient name is required for new patient registration.")

    new_patient_data = Patient(phone_number=whatsapp, name=name, age=age, gender=gender)
    return shared_db.create_patient(new_patient_data)


# -------------------------
# MCP Tools
# -------------------------

@mcp.tool()
def get_current_time() -> str:
    """
    Returns the current date and time in ISO 8601 format.
    This must be called before any time-sensitive operations like booking, rescheduling or cancelling
    to ensure the agent has accurate knowledge of the present moment.
    """
    now = datetime.now().isoformat()
    logger.debug("Tool: get_current_time, returning: %s", now)
    return now


@mcp.tool()
def convert_gregorian_to_hijri(payload: GregorianToHijriPayload) -> Dict[str, Any]:
    """
    Converts a standard (Gregorian) calendar date to the equivalent Hijri
    (Islamic) calendar date. Use this whenever a patient asks what a date
    is in the Hijri calendar. Never guess or calculate this yourself —
    always call this tool.

    IMPORTANT: this uses the standard mathematical (tabular) Hijri
    calendar, which can be off by a day from Saudi Arabia's officially
    announced date (which depends on physical moon-sighting, especially
    around Ramadan and Eid). Mention that uncertainty to the patient for
    anything Ramadan/Eid-related.
    """
    logger.debug("Tool: convert_gregorian_to_hijri, payload=%s", payload)
    try:
        try:
            from hijri_converter import Gregorian
        except ImportError:
            from hijri_converter.convert import Gregorian

        g_date = datetime.fromisoformat(payload.gregorian_date).date()
        hijri = Gregorian(g_date.year, g_date.month, g_date.day).to_hijri()
        month_name = HIJRI_MONTH_NAMES[hijri.month - 1]
        return {
            "gregorian_date": payload.gregorian_date,
            "hijri_year": hijri.year,
            "hijri_month": hijri.month,
            "hijri_month_name": month_name,
            "hijri_day": hijri.day,
            "hijri_date_formatted": f"{hijri.day} {month_name} {hijri.year} AH",
            "note": "Approximate — may be off by a day from Saudi Arabia's officially announced date.",
        }
    except Exception as e:
        logger.error("Error in convert_gregorian_to_hijri: %s", e, exc_info=True)
        return {"error": "conversion_failed", "details": str(e)}


@mcp.tool()
def convert_hijri_to_gregorian(payload: HijriToGregorianPayload) -> Dict[str, Any]:
    """
    Converts a Hijri (Islamic calendar) date to the equivalent standard
    (Gregorian) calendar date. Use this whenever a patient gives you a date
    in the Hijri calendar (e.g. "1 Ramadan" or "10 Shawwal 1448") and you
    need the actual calendar date to check availability or book/reschedule
    an appointment. Never guess or calculate this yourself — always call
    this tool.

    IMPORTANT: this uses the standard mathematical (tabular) Hijri
    calendar, which can be off by a day from Saudi Arabia's officially
    announced date (which depends on physical moon-sighting, especially
    around Ramadan and Eid). Mention that uncertainty to the patient for
    anything Ramadan/Eid-related.
    """
    logger.debug("Tool: convert_hijri_to_gregorian, payload=%s", payload)
    try:
        try:
            from hijri_converter import Hijri
        except ImportError:
            from hijri_converter.convert import Hijri

        gregorian = Hijri(payload.hijri_year, payload.hijri_month, payload.hijri_day).to_gregorian()
        gregorian_date_str = f"{gregorian.year:04d}-{gregorian.month:02d}-{gregorian.day:02d}"
        month_name = HIJRI_MONTH_NAMES[payload.hijri_month - 1]
        return {
            "hijri_date": f"{payload.hijri_day} {month_name} {payload.hijri_year} AH",
            "gregorian_date": gregorian_date_str,
            "note": "Approximate — may be off by a day from Saudi Arabia's officially announced date.",
        }
    except Exception as e:
        logger.error("Error in convert_hijri_to_gregorian: %s", e, exc_info=True)
        return {"error": "conversion_failed", "details": str(e)}


@mcp.tool()
def get_clinic_info() -> Dict[str, Any]:
    """
    Returns general clinic information: name, address, Google Maps link,
    general working hours, parking instructions, phone number, upcoming
    holidays/closures, and the list of services offered with their prices
    (in SAR).
    Use this to answer routine questions like "what are your hours",
    "where are you located", "how do I get there", "how much does X cost",
    "is there parking", or "are you open on <date>" — do not guess this
    information, always call this tool.
    """
    logger.debug("Tool: get_clinic_info")
    info = {
        **clinic_config.get_clinic_info(),
        "services": clinic_config.get_services(),
    }
    profile = clinic_config.get_clinic_profile()
    if not clinic_types.is_plain_dental(profile):
        info["departments"] = [
            {"key": d["key"], "name": d["name"], "type": d["type"],
             "bookable_here": d["active"], "women_only": d["women_only"]}
            for d in clinic_types.departments(profile)
        ]
    return info


@mcp.tool()
def send_offers_brochure(payload: SendOffersBrochurePayload) -> Dict[str, Any]:
    """
    Sends the clinic's current offers/promotions brochure image directly to
    the patient over WhatsApp. Use this whenever a patient asks about
    current offers, promotions, discounts, or deals — send the image
    rather than trying to describe offers yourself in detail.
    """
    logger.debug("Tool: send_offers_brochure, payload=%s", payload)
    try:
        # Imported here (not at module load time) since this MCP server
        # process only needs it for this one tool, and to avoid loading the
        # FastAPI app's dependencies for every other tool call.
        from app.whatsapp import send_image_message

        image_url = clinic_config.get_offers_image_url()
        if not image_url:
            return {
                "error": "brochure_not_available",
                "details": "No public URL is configured for the brochure image right now.",
            }

        caption = clinic_config.get_clinic_info().get("offers_text") or None
        send_image_message(payload.patient_whatsapp, image_url, caption=caption)
        return {"status": "sent"}
    except Exception as e:
        logger.error("Error in send_offers_brochure: %s", e, exc_info=True)
        return {"error": "send_failed", "details": str(e)}


@mcp.tool()
def list_dentists(specialization: Optional[str] = None, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Retrieves a list of all available dentists (in clinics with other
    departments: all the clinic's doctors and specialists), including each
    one's nationality and gender (when set — they may be blank).
    You can optionally filter the list by specialization (e.g., 'Orthodontist', 'Endodontist'),
    and, in a clinic with several departments, by department (the department key or name from
    get_clinic_info, e.g. 'laser').
    """
    logger.debug("Tool: list_dentists, specialization=%s, department=%s", specialization, department)
    dentists = shared_db.get_all_dentists()
    if specialization:
        dentists = [d for d in dentists if specialization.lower() in (d.specialization or "").lower()]
    profile = clinic_config.get_clinic_profile()
    if department and not clinic_types.is_plain_dental(profile):
        wanted = clinic_types.find_department(profile, department)
        if wanted is None:
            return [{"error": "unknown_department",
                     "departments": [d["key"] for d in clinic_types.departments(profile)]}]
        dentists = [d for d in dentists
                    if (clinic_types.find_department(profile, d.department) or {}).get("key") == wanted["key"]]
    result = []
    for d in dentists:
        row = d.model_dump()
        if not clinic_types.is_plain_dental(profile):
            dept = clinic_types.find_department(profile, d.department)
            row["department"] = dept["key"] if dept else d.department
            row["department_bookable_here"] = dept["active"] if dept else True
        else:
            row.pop("department", None)
            if row.get("gender") is None:
                row.pop("gender", None)
        result.append(row)
    return result


@mcp.tool()
def get_dentist_profile(dentist_id: Optional[int] = None, name: Optional[str] = None) -> Dict[str, Any]:
    """
    Gets the detailed profile of a specific dentist, either by their unique ID or by their name.
    Providing a name will return the first dentist that matches.
    The profile includes nationality (when set for that dentist — it may be
    blank). Use this field to answer a patient asking a dentist's nationality
    rather than declining — only say you don't have it if the field is
    actually empty for that dentist.
    """
    logger.debug("Tool: get_dentist_profile, id=%s, name=%s", dentist_id, name)
    if dentist_id:
        dentist = shared_db.get_dentist(dentist_id)
        return dentist.model_dump() if dentist else {"error": "dentist_not_found"}
    if name:
        with shared_db.db() as conn:
            row = conn.execute("SELECT * FROM dentists WHERE name LIKE ? LIMIT 1", (f"%{name}%",)).fetchone()
            if row:
                return dict(row)
    return {"error": "A dentist_id or name must be provided."}


@mcp.tool()
def get_availability(dentist_id: int) -> Dict[str, Any]:
    """
    Fetches the weekly availability schedule for a specific dentist, identified by their ID.
    """
    logger.debug("Tool: get_availability, dentist_id=%s", dentist_id)
    dentist = shared_db.get_dentist(dentist_id)
    if not dentist:
        return {"error": "dentist_not_found"}
    return {"availability_schedule": dentist.availability_schedule}


@mcp.tool()
def update_patient_profile(payload: UpdatePatientPayload) -> Dict[str, Any]:
    """
    Updates a patient's profile details (name, age, gender) using their WhatsApp number.
    This should be used to register the full details of a newly identified patient.
    """
    logger.debug("Tool: update_patient_profile, payload=%s", payload)
    with shared_db.db() as conn:
        patient_row = conn.execute("SELECT * FROM patients WHERE phone_number = ?", (payload.whatsapp_number,)).fetchone()
        if not patient_row:
            return {"error": "patient_not_found", "details": f"No patient with WhatsApp number {payload.whatsapp_number}"}
        
        patient_id = patient_row["id"]
        updates = []
        params = []
        if payload.name is not None:
            updates.append("name = ?")
            params.append(payload.name)
        if payload.age is not None:
            updates.append("age = ?")
            params.append(payload.age)
        if payload.gender is not None:
            updates.append("gender = ?")
            params.append(payload.gender)

        if not updates:
            return {"error": "no_update_fields_provided", "details": "You must provide at least one field to update."}

        params.append(patient_id)
        query = f"UPDATE patients SET {', '.join(updates)} WHERE id = ?"
        conn.execute(query, tuple(params))

    logger.info(f"Updated patient profile for patient id {patient_id}")
    return {"status": "success", "patient_id": patient_id}


@mcp.tool()
def upcoming_appointments(patient_whatsapp: str) -> List[Dict[str, Any]]:
    """
    Returns a list of all upcoming scheduled appointments for a patient, identified by their WhatsApp number.
    """
    logger.debug("Tool: upcoming_appointments, patient_whatsapp=%s", patient_whatsapp)
    patient = shared_db.get_patient_by_phone(patient_whatsapp)
    if not patient:
        return []  # Return an empty list if the patient is not found
    
    appointments = shared_db.get_patient_appointments(patient.id)
    return [appt.model_dump() for appt in appointments if appt.status == 'scheduled']


@mcp.tool()
def book_appointment(payload: BookAppointmentPayload) -> Dict[str, Any]:
    """
    Books a new appointment for a patient with a specific dentist at a given time.
    If the patient does not exist, their name must be provided to create a new patient record.
    """
    logger.debug("Tool: book_appointment, payload=%s", payload)
    try:
        dentist = shared_db.get_dentist(payload.dentist_id)
        if not dentist:
            return {"error": "dentist_not_found"}

        # Hard safety net against booking into the past. The system prompt
        # also tells the agent to check get_current_time and never book a
        # past slot, but that's just an instruction the agent could in
        # theory forget to follow — this check makes it impossible
        # regardless, by rejecting the request at the data layer.
        if datetime.fromisoformat(payload.appointment_time) < datetime.now():
            return {"error": "time_in_past", "details": "That appointment time has already passed. Please choose a future date and time."}

        # Department checks (clinics with several departments only): an
        # extra department whose own subscription ran out can't be booked,
        # and a women-only department can't be booked for a male patient.
        existing_patient = shared_db.get_patient_by_phone(payload.patient_whatsapp)
        gender = payload.patient_gender or (existing_patient.gender if existing_patient else None)
        blocked = clinic_types.booking_block(clinic_config.get_clinic_profile(), dentist.department, gender)
        if blocked:
            return blocked

        patient = _ensure_patient(
            whatsapp=payload.patient_whatsapp,
            name=payload.patient_name,
            age=payload.patient_age,
            gender=payload.patient_gender
        )

        with shared_db.db() as conn:
            clash = conn.execute(
                "SELECT id FROM appointments WHERE dentist_id = ? AND appointment_time = ? AND status = 'scheduled'",
                (payload.dentist_id, payload.appointment_time),
            ).fetchone()
            if clash:
                return {"error": "slot_unavailable", "details": "The requested time slot is already booked."}

            # Best-effort match of the freeform service_name the agent passed
            # against this clinic's configured services list, so we store the
            # clinic's own canonical name/price rather than whatever casing or
            # phrasing the agent used -- this is what keeps "most requested
            # services" reporting clean instead of splitting "Cleaning" and
            # "teeth cleaning" into two separate buckets. No match (or no
            # service_name given) just leaves both fields NULL -- never a
            # reason to fail the booking itself.
            matched_service_name, matched_price = None, None
            if payload.service_name:
                for svc in clinic_config.get_services():
                    if str(svc.get("name", "")).strip().lower() == payload.service_name.strip().lower():
                        matched_service_name = svc.get("name")
                        # price_sar in clinic_config.yaml is free text (a non-developer edits it), so it isn't
                        # guaranteed numeric (e.g. "Contact us for pricing") -- only store it if it actually is.
                        try:
                            matched_price = float(svc.get("price_sar"))
                        except (TypeError, ValueError):
                            matched_price = None
                        break

            new_appointment = Appointment(
                patient_id=patient.id,
                dentist_id=payload.dentist_id,
                appointment_time=datetime.fromisoformat(payload.appointment_time),
                status='scheduled',
                service_name=matched_service_name,
                price_sar=matched_price,
            )
            created_appt = shared_db.create_appointment(new_appointment)

        logger.info("Booked appointment id=%s for patient=%s", created_appt.id, patient.id)
        return created_appt.model_dump()

    except ValueError as ve:
        return {"error": "validation_error", "details": str(ve)}
    except Exception as e:
        logger.error("Error in book_appointment: %s", e, exc_info=True)
        return {"error": "internal_server_error", "details": str(e)}


@mcp.tool()
def cancel_appointment(payload: CancelAppointmentPayload) -> Dict[str, Any]:
    """
    Cancels an existing appointment.
    This can be done by providing the unique appointment_id, or by providing the patient's WhatsApp number, the dentist's ID, and the appointment time.
    """
    logger.debug("Tool: cancel_appointment, payload=%s", payload)
    if payload.appointment_id:
        updated = shared_db.update_appointment_status(payload.appointment_id, 'cancelled')
        if updated:
            logger.info("Canceled appointment id=%s", payload.appointment_id)
            return {"status": "cancelled", "appointment_id": payload.appointment_id}
        return {"error": "not_found", "details": "Appointment ID not found or already cancelled."}

    if payload.patient_whatsapp and payload.dentist_id and payload.appointment_time:
        patient = shared_db.get_patient_by_phone(payload.patient_whatsapp)
        if not patient:
            return {"error": "patient_not_found"}
        
        with shared_db.db() as conn:
            # Find the specific appointment to cancel
            appt_to_cancel = conn.execute(
                "SELECT id FROM appointments WHERE patient_id=? AND dentist_id=? AND appointment_time=? AND status='scheduled'",
                (patient.id, payload.dentist_id, payload.appointment_time)
            ).fetchone()

            if not appt_to_cancel:
                return {"error": "not_found", "details": "No matching scheduled appointment found for the given details."}
            
            updated = shared_db.update_appointment_status(appt_to_cancel['id'], 'cancelled')
            if updated:
                logger.info("Cancelled appointment id=%s", appt_to_cancel['id'])
                return {"status": "cancelled", "appointment_id": appt_to_cancel['id']}

    return {"error": "invalid_payload", "details": "You must provide either an appointment_id or the trio of patient_whatsapp, dentist_id, and appointment_time."}


@mcp.tool()
def reschedule_appointment(payload: ReschedulePayload) -> Dict[str, Any]:
    """
    Reschedules an existing appointment to a new time. Requires the unique appointment_id.
    """
    logger.debug("Tool: reschedule_appointment, payload=%s", payload)

    # Hard safety net against rescheduling into the past — see the matching
    # comment in book_appointment for why this can't just rely on the
    # system prompt's instruction alone.
    if datetime.fromisoformat(payload.new_appointment_time) < datetime.now():
        return {"error": "time_in_past", "details": "That appointment time has already passed. Please choose a future date and time."}

    with shared_db.db() as conn:
        appt_row = conn.execute("SELECT * FROM appointments WHERE id = ?", (payload.appointment_id,)).fetchone()
        if not appt_row or appt_row["status"] != "scheduled":
            return {"error": "appointment_not_found_or_not_scheduled"}

        clash = conn.execute(
            "SELECT id FROM appointments WHERE dentist_id = ? AND appointment_time = ? AND status = 'scheduled' AND id <> ?",
            (appt_row["dentist_id"], payload.new_appointment_time, payload.appointment_id),
        ).fetchone()
        if clash:
            return {"error": "new_slot_unavailable"}

        conn.execute(
            "UPDATE appointments SET appointment_time = ?, status = 'rescheduled' WHERE id = ?",
            (payload.new_appointment_time, payload.appointment_id),
        )
    logger.info("Rescheduled appointment id=%s to %s", payload.appointment_id, payload.new_appointment_time)
    return {"status": "rescheduled", "appointment_id": payload.appointment_id}


@mcp.tool()
def close_conversation(payload: CloseConversationPayload) -> Dict[str, Any]:
    """
    Closes the current conversation when the user has confirmed they have no more requests.
    Use this tool when the user says "no", "that's all", "I'm done", etc.
    """
    logger.debug("Tool: close_conversation, payload=%s", payload)
    try:
        shared_db.close_conversation(payload.conversation_id, payload.reason)
        logger.info(f"Conversation {payload.conversation_id} closed by agent with reason: {payload.reason}")
        return {"status": "success", "conversation_id": payload.conversation_id}
    except Exception as e:
        logger.error(f"Failed to close conversation {payload.conversation_id}: {e}", exc_info=True)
        return {"error": "db_error", "details": str(e)}


@mcp.tool()
def flag_for_staff(payload: FlagForStaffPayload) -> Dict[str, Any]:
    """
    Flags the current conversation so clinic staff can follow up directly with
    the patient. Use this any time you tell a patient you cannot help with
    something and a human needs to take over — for example: symptom/emergency
    questions, medication questions, questions about test results, insurance
    or billing questions, complaints, or anything else outside your scope.
    This does NOT end the conversation — keep responding normally to the
    patient after calling it (e.g. still tell them to call the clinic or go
    to the ER if needed). Always include a short, specific reason so staff
    know what to follow up on.
    """
    logger.debug("Tool: flag_for_staff, payload=%s", payload)
    try:
        shared_db.flag_conversation(payload.conversation_id, payload.reason)
        logger.warning(f"Conversation {payload.conversation_id} flagged for staff: {payload.reason}")
        return {"status": "success", "conversation_id": payload.conversation_id}
    except Exception as e:
        logger.error(f"Failed to flag conversation {payload.conversation_id}: {e}", exc_info=True)
        return {"error": "db_error", "details": str(e)}


# -------------------------
# Bootstrap and run
# -------------------------
def setup_mcp_logging(level=logging.INFO):
    """
    Configures logging specifically for the MCP server process.
    """
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, "mcp_server.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # encoding="utf-8" avoids UnicodeEncodeError crashes on Windows when a
    # logged message contains an emoji (the default codepage can't hold it).
    file_handler = logging.FileHandler(log_file_path, mode='a', encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # IMPORTANT: this server talks to its parent process over stdin/stdout
    # (that's what "stdio transport" means for MCP). Logging to stdout would
    # mix plain-text log lines into that same channel and corrupt the
    # protocol, so diagnostic logging goes to stderr instead — stdout is
    # reserved entirely for MCP protocol messages.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)


def main():
    parser = argparse.ArgumentParser(prog="mcp.server", description="MCP Server for Dentist App (stdio transport)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_mcp_logging(log_level)
    
    logger.info("MCP server starting (stdio transport). Verbose=%s", args.verbose)

    shared_db.init_db(seed=True)

    try:
        mcp.run(transport="stdio")
    except Exception as e:
        logger.exception("MCP server exited with error: %s", e)


if __name__ == "__main__":
    main()


"""
Running
# normal logging
uv run python -m mcp.server

# verbose (debug logs)
uv run python -m mcp.server --verbose
"""
