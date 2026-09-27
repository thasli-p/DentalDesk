from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# -----------------------
# Dentist Models
# -----------------------
class Dentist(BaseModel):
    id: Optional[int] = Field(default=None, description="Primary key in DB")
    name: str
    specialization: str
    languages_spoken: str
    qualifications: Optional[str] = None
    years_experience: Optional[int] = None
    availability_schedule: Optional[str] = None
    nationality: Optional[str] = None
    department: Optional[str] = Field(
        default=None,
        description="Which department this practitioner works in (key or name, see shared/clinic_types.py). "
                    "Blank = the clinic's main department -- every practitioner of a plain dental clinic.",
    )
    gender: Optional[str] = Field(default=None, description="Male/Female -- lets Sia honour 'I want a female doctor'.")


# -----------------------
# Patient Models
# -----------------------
class Patient(BaseModel):
    id: Optional[int] = Field(default=None, description="Primary key in DB")
    name: str
    age: Optional[int] = None
    gender: Optional[str] = Field(default=None, description="Male/Female/Other")
    phone_number: str
    created_at: Optional[datetime] = Field(
        default=None,
        description="When this patient/contact first messaged the clinic. Set automatically by db.create_patient "
                     "if left blank. Used by the staff dashboard to count new patients per period -- rows from "
                     "before this field existed have this as NULL and are excluded from period-based new-patient counts.",
    )


# -----------------------
# Appointment Models
# -----------------------
class Appointment(BaseModel):
    id: Optional[int] = Field(default=None, description="Primary key in DB")
    patient_id: int
    dentist_id: int
    appointment_time: datetime
    status: str = Field(default="scheduled", description="scheduled/cancelled/completed/rescheduled")
    created_at: Optional[datetime] = Field(
        default=None,
        description="When this appointment was BOOKED (not the appointment_time itself, which is the future slot). "
                     "Set automatically by db.create_appointment if left blank. Used by the staff dashboard for "
                     "'bookings this week' style reporting -- rows from before this field existed have this as "
                     "NULL and are excluded from period-based booking counts.",
    )
    service_name: Optional[str] = Field(
        default=None,
        description="The clinic service this appointment is for, matched against the clinic's configured services "
                     "list (see clinic_config.get_services()) when possible. Optional -- Sia passes this when the "
                     "patient specified a service; older bookings and any booking without a clear service match "
                     "leave this NULL. Powers the 'most requested services' dashboard insight.",
    )
    price_sar: Optional[float] = Field(
        default=None,
        description="The service's price in SAR at the time of booking, looked up from the clinic's services list "
                     "by service_name. Used for the dashboard's estimated-revenue-booked figure -- an estimate of "
                     "pipeline value, not confirmed/collected revenue.",
    )


# -----------------------
# Conversation Models
# -----------------------
class Conversation(BaseModel):
    id: Optional[int] = Field(default=None, description="Primary key in DB")
    patient_id: Optional[int] = None
    status: str = Field(default="open", description="open/closed")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    closed_reason: Optional[str] = None
    flagged_for_staff: bool = Field(
        default=False, description="Set by shared.db.flag_conversation() when Sia calls flag_for_staff."
    )
    flag_reason: Optional[str] = Field(
        default=None, description="Why this conversation was flagged -- see flagged_for_staff."
    )
    channel: str = Field(
        default="whatsapp", description="'whatsapp' for Sia's chats, 'phone' for voice-receptionist calls."
    )
    external_ref: Optional[str] = Field(
        default=None, description="For phone calls: the voice platform's call/conversation ID (dedupe key)."
    )


# -----------------------
# Single Message Models
# -----------------------
class Message(BaseModel):
    id: int
    conversation_id: int
    sender: str               # "user" , "agent" , "tool" or "agent_tool_call"
    message: str
    created_at: datetime

# -----------------------
# Composite / API models
# -----------------------

class AppointmentWithDetails(BaseModel):
    """Convenience model to return appointments joined with doctor/patient info"""
    appointment_id: int
    appointment_time: datetime
    status: str
    dentist_name: str
    patient_name: str
    service_name: Optional[str] = None
    price_sar: Optional[float] = None


class DentistListResponse(BaseModel):
    dentists: List[Dentist]


class AppointmentListResponse(BaseModel):
    appointments: List[AppointmentWithDetails]
