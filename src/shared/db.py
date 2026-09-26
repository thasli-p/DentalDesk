import os
import sqlite3
import logging
import contextvars
from contextlib import contextmanager
from typing import List, Optional
from datetime import datetime

from shared.models import Dentist, Patient, Appointment, Message, Conversation, AppointmentWithDetails

logger = logging.getLogger(__name__)

# The default (legacy, single-clinic) database file -- used whenever no
# per-clinic path has been set below. Kept exactly as before so nothing
# changes for the one clinic that predates the multi-clinic feature.
_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "../..", "data", "dentaldesk_app.db")

# Multi-clinic support: this one running app now handles several clinics at
# once, each with its own separate database file (see shared/clinics_store.py).
# The MCP tool subprocess is one separate OS process per clinic, so it can
# just read CLINIC_DB_PATH from its own environment. But shared/db.py is
# ALSO called directly from app/agent.py, which runs ALL clinics' background
# tasks together inside the ONE FastAPI process -- there, a plain
# environment variable would be shared (and wrong) across clinics running
# concurrently. A contextvars.ContextVar solves this: each asyncio task
# gets its own independent copy, so one clinic's task setting this never
# affects another clinic's task running at the same time, while everything
# awaited from within that task (including this module's own functions)
# still sees the value that task set.
_current_db_path: contextvars.ContextVar = contextvars.ContextVar("current_db_path", default=None)


def set_current_db_path(path: str) -> None:
    """Call this once at the start of a clinic's background task (see
    app/agent.py's ClinicWorker.run) so every db.* call made from within
    that task -- and everything it awaits -- reads/writes that clinic's own
    database file instead of the shared default."""
    _current_db_path.set(path)


def get_db_path() -> str:
    return (
        _current_db_path.get()
        or os.environ.get("CLINIC_DB_PATH")
        or _DEFAULT_DB_PATH
    )


@contextmanager
def db():
    path = get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# -----------------------
# Dentist Queries
# -----------------------
def get_all_dentists() -> List[Dentist]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM dentists").fetchall()
        return [Dentist(**dict(row)) for row in rows]


def get_dentist(dentist_id: int) -> Optional[Dentist]:
    with db() as conn:
        row = conn.execute("SELECT * FROM dentists WHERE id=?", (dentist_id,)).fetchone()
        return Dentist(**dict(row)) if row else None


# -----------------------
# Patient Queries
# -----------------------
def create_patient(patient: Patient) -> Patient:
    created_at = patient.created_at or datetime.now()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO patients (name, age, gender, phone_number, created_at) VALUES (?, ?, ?, ?, ?)",
            (patient.name, patient.age, patient.gender, patient.phone_number, created_at.isoformat()),
        )
        patient.id = cur.lastrowid
        patient.created_at = created_at
        logger.info("Patient created with id=%s", patient.id)
        return patient


def get_patient(patient_id: int) -> Optional[Patient]:
    with db() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        return Patient(**dict(row)) if row else None


def get_patient_by_phone(phone_number: str) -> Optional[Patient]:
    with db() as conn:
        row = conn.execute("SELECT * FROM patients WHERE phone_number=?", (phone_number,)).fetchone()
        return Patient(**dict(row)) if row else None


# -----------------------
# Appointment Queries
# -----------------------
def create_appointment(appt: Appointment) -> Appointment:
    created_at = appt.created_at or datetime.now()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO appointments (patient_id, dentist_id, appointment_time, status, created_at, service_name, price_sar)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appt.patient_id, appt.dentist_id, appt.appointment_time.isoformat(), appt.status,
                created_at.isoformat(), appt.service_name, appt.price_sar,
            ),
        )
        appt.id = cur.lastrowid
        appt.created_at = created_at
        logger.info("Appointment created with id=%s", appt.id)
        return appt


def update_appointment_status(appt_id: int, status: str) -> bool:
    with db() as conn:
        cur = conn.execute("UPDATE appointments SET status=? WHERE id=?", (status, appt_id))
        if cur.rowcount > 0:
            logger.info("Appointment id=%s updated to status=%s", appt_id, status)
        else:
            logger.warning("No appointment found with id=%s", appt_id)
        return cur.rowcount > 0


def get_patient_appointments(patient_id: int) -> List[AppointmentWithDetails]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id as appointment_id,
                a.appointment_time,
                a.status,
                d.name as dentist_name,
                p.name as patient_name,
                a.service_name,
                a.price_sar
            FROM appointments a
            JOIN dentists d ON a.dentist_id = d.id
            JOIN patients p ON a.patient_id = p.id
            WHERE a.patient_id = ?
            ORDER BY a.appointment_time
            """,
            (patient_id,),
        ).fetchall()
        return [AppointmentWithDetails(**dict(row)) for row in rows]


# -----------------------
# Conversation Queries
# -----------------------
def create_conversation(patient_id: Optional[int]) -> Conversation:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (patient_id, status, started_at) VALUES (?, 'open', ?)",
            (patient_id, datetime.now().isoformat()),
        )
        conv = Conversation(
            id=cur.lastrowid,
            patient_id=patient_id,
            status="open",
            started_at=datetime.now(),
        )
        logger.debug("Conversation created with id=%s", conv.id)
        return conv

def close_conversation(conversation_id: int, reason: str):
    with db() as conn:
        conn.execute(
            "UPDATE conversations SET status='closed', ended_at=?, closed_reason=? WHERE id=?",
            (datetime.now().isoformat(), reason, conversation_id),
        )
        logger.debug("Conversation %s closed", conversation_id)

def flag_conversation(conversation_id: int, reason: str):
    """Marks a conversation as needing staff attention (e.g. a red-flag
    symptom, or any question outside the bot's scope). Does NOT close the
    conversation — the bot keeps talking to the patient (e.g. telling them
    to call the clinic or go to the ER) while this flag lets staff know to
    check in. See get_flagged_conversations() to review these.

    If the same conversation gets flagged more than once (e.g. the patient
    asks two different out-of-scope questions before the chat ends), each
    new reason is appended to the existing one with a timestamp rather than
    replacing it, so staff can see the full history of what came up.
    """
    with db() as conn:
        existing = conn.execute(
            "SELECT flag_reason FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        existing_reason = existing["flag_reason"] if existing else None
        timestamp = datetime.now().isoformat(timespec="minutes")
        new_entry = f"[{timestamp}] {reason}"
        combined_reason = f"{existing_reason}\n{new_entry}" if existing_reason else new_entry

        conn.execute(
            "UPDATE conversations SET flagged_for_staff=1, flag_reason=? WHERE id=?",
            (combined_reason, conversation_id),
        )
        logger.warning("Conversation %s flagged for staff: %s", conversation_id, reason)


def get_flagged_conversations() -> List[dict]:
    """Returns open-or-closed conversations flagged for staff attention,
    newest first, with the patient's name and phone number included so
    staff can follow up."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                c.id as conversation_id,
                c.status,
                c.started_at,
                c.flag_reason,
                p.name as patient_name,
                p.phone_number as patient_phone
            FROM conversations c
            LEFT JOIN patients p ON c.patient_id = p.id
            WHERE c.flagged_for_staff = 1
            ORDER BY c.started_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def add_message(conversation_id: int, sender: str, message: str) -> Message:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, sender, message, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, sender, message, datetime.now().isoformat()),
        )
        msg = Message(
            id=cur.lastrowid,
            conversation_id=conversation_id,
            sender=sender,
            message=message,
            created_at=datetime.now(),
        )
        logger.debug("Message added to conversation_id=%s", conversation_id)
        return msg

def get_messages(conversation_id: int) -> List[Message]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [Message(**dict(row)) for row in rows]


def get_conversation(conversation_id: int) -> Optional[Conversation]:
    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return Conversation(**dict(row)) if row else None


def get_open_conversation(patient_id: int) -> Optional[Conversation]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM conversations
            WHERE patient_id=? AND status='open'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (patient_id,),
        ).fetchone()
        return Conversation(**dict(row)) if row else None


def get_all_open_conversations() -> List[Conversation]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM conversations WHERE status='open'").fetchall()
        return [Conversation(**dict(row)) for row in rows]

def get_last_message_time(conversation_id: int) -> Optional[datetime]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT created_at FROM messages
            WHERE conversation_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None

def get_last_message_for_patient(patient_id: int) -> Optional[Message]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT m.*
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.patient_id=?
            ORDER BY m.created_at DESC
            LIMIT 1
            """,
            (patient_id,),
        ).fetchone()
        return Message(**dict(row)) if row else None


# -----------------------
# DB Initialization
# -----------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dentists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    specialization TEXT NOT NULL,
    languages_spoken TEXT,
    qualifications TEXT,
    years_experience INTEGER,
    availability_schedule TEXT,
    nationality TEXT
);

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER,
    gender TEXT,
    phone_number TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    dentist_id INTEGER NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT,
    service_name TEXT,
    price_sar REAL,
    FOREIGN KEY(patient_id) REFERENCES patients(id),
    FOREIGN KEY(dentist_id) REFERENCES dentists(id)
);

-- Conversation sessions
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    closed_reason TEXT,
    flagged_for_staff INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    FOREIGN KEY(patient_id) REFERENCES patients(id)
);

-- Individual messages per conversation
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    sender TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);
"""

def init_db(seed: bool = True, dentists: Optional[list] = None):
    """Create tables if missing, seed only if dentists table is empty.

    By default, dentists are seeded from clinic_config.get_dentists_config()
    (which reads config/clinic_config.yaml, or that clinic's own copy of it
    for a clinic added through the Add Clinic admin page). Pass `dentists`
    explicitly (a list of dicts, same shape as clinic_config.yaml's
    `dentists:` entries) to seed from an in-memory list instead, without
    touching any file -- this is what a clinic's ClinicWorker does, since
    its dentists list already lives in the clinics_store row.

    Note: this only runs when the dentists table is empty, so editing the
    config later won't update an already-seeded database -- delete that
    clinic's database file and restart the app to re-seed from scratch.
    """
    path = get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA_SQL)

        # Migration for databases created before flagged_for_staff/flag_reason
        # existed. CREATE TABLE IF NOT EXISTS above won't add columns to an
        # existing table, so add them here if missing. Safe to run every time:
        # SQLite raises "duplicate column" if they're already there, and we
        # just ignore that.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "flagged_for_staff" not in existing_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN flagged_for_staff INTEGER NOT NULL DEFAULT 0")
            logger.info("Migrated conversations table: added flagged_for_staff column.")
        if "flag_reason" not in existing_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN flag_reason TEXT")
            logger.info("Migrated conversations table: added flag_reason column.")
        # Voice receptionist: phone calls are stored as conversations too
        # (channel='phone'), keyed by the voice platform's own call ID
        # (external_ref) so a call is never stored twice. Existing rows are
        # all WhatsApp chats, hence the default.
        if "channel" not in existing_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'")
            logger.info("Migrated conversations table: added channel column.")
        if "external_ref" not in existing_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN external_ref TEXT")
            logger.info("Migrated conversations table: added external_ref column.")

        # Same idea for dentists.nationality on databases created before it existed.
        existing_dentist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(dentists)")}
        if "nationality" not in existing_dentist_columns:
            conn.execute("ALTER TABLE dentists ADD COLUMN nationality TEXT")
            logger.info("Migrated dentists table: added nationality column.")

        # Same idea for patients.created_at (added for the staff dashboard's
        # "new patients" reporting -- see shared/analytics.py). Existing rows
        # get NULL here since we genuinely don't know when they first
        # messaged; the dashboard excludes NULLs from period-based counts
        # rather than guessing.
        existing_patient_columns = {row["name"] for row in conn.execute("PRAGMA table_info(patients)")}
        if "created_at" not in existing_patient_columns:
            conn.execute("ALTER TABLE patients ADD COLUMN created_at TEXT")
            logger.info("Migrated patients table: added created_at column.")

        # Same idea for appointments.created_at/service_name/price_sar (also
        # added for the staff dashboard -- see shared/analytics.py).
        existing_appt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(appointments)")}
        if "created_at" not in existing_appt_columns:
            conn.execute("ALTER TABLE appointments ADD COLUMN created_at TEXT")
            logger.info("Migrated appointments table: added created_at column.")
        if "service_name" not in existing_appt_columns:
            conn.execute("ALTER TABLE appointments ADD COLUMN service_name TEXT")
            logger.info("Migrated appointments table: added service_name column.")
        if "price_sar" not in existing_appt_columns:
            conn.execute("ALTER TABLE appointments ADD COLUMN price_sar REAL")
            logger.info("Migrated appointments table: added price_sar column.")

        if seed:
            existing = conn.execute("SELECT COUNT(*) FROM dentists").fetchone()[0]
            if existing == 0:
                if dentists is None:
                    from shared.clinic_config import get_dentists_config
                    dentists = get_dentists_config()

                conn.executemany(
                    "INSERT INTO dentists (name, specialization, languages_spoken, qualifications, years_experience, availability_schedule, nationality) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            d.get("name"),
                            d.get("specialization"),
                            d.get("languages_spoken"),
                            d.get("qualifications"),
                            d.get("years_experience"),
                            d.get("availability_schedule"),
                            d.get("nationality"),
                        )
                        for d in dentists
                    ],
                )
                logger.info("Seeded %d dentists into %s.", len(dentists), path)
            else:
                logger.info("Dentists already present, skipping seeding.")


def clean_db():
    path = get_db_path()
    if os.path.exists(path):
        confirm = input(f"⚠️ Are you sure you want to delete {path}? (y/N): ")
        if confirm.lower() == "y":
            os.remove(path)
            logger.warning("Database deleted: %s", path)
        else:
            logger.info("Cancelled database deletion.")
    else:
        logger.info("No DB found to clean.")


# -----------------------
# CLI
# -----------------------
if __name__ == "__main__":
    from shared.logger_config import setup_logging
    setup_logging()

    import argparse

    parser = argparse.ArgumentParser(description="Dentist App DB Utility")
    parser.add_argument("--init", action="store_true", help="Initialize DB if empty and seed dentists")
    parser.add_argument("--clean", action="store_true", help="Delete the database file with confirmation")
    parser.add_argument("--list-dentists", action="store_true", help="List all dentists")
    parser.add_argument("--list-patients", action="store_true", help="List all patients")
    parser.add_argument("--list-appointments", type=int, help="List appointments for a patient_id")
    parser.add_argument("--list-flagged", action="store_true", help="List conversations flagged for staff attention")
    parser.add_argument("--add-patient", action="store_true", help="Interactive: add a new patient")
    parser.add_argument("--book-appointment", action="store_true", help="Interactive: book an appointment")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Configure logging after args parsed
    # log_level = logging.DEBUG if args.verbose else logging.INFO
    # logging.basicConfig(
    #     level=log_level,
    #     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    # )

    if args.init:
        init_db(seed=True)
        logger.info("Database initialized at: %s", get_db_path())

    if args.clean:
        clean_db()

    if args.list_dentists:
        for d in get_all_dentists():
            logger.info(d)

    if args.list_patients:
        with db() as conn:
            rows = conn.execute("SELECT * FROM patients").fetchall()
            for r in rows:
                logger.info(dict(r))

    if args.list_appointments:
        for appt in get_patient_appointments(args.list_appointments):
            logger.info(appt)

    if args.list_flagged:
        flagged = get_flagged_conversations()
        if not flagged:
            logger.info("No conversations are currently flagged for staff attention.")
        for f in flagged:
            logger.info(
                "Conversation #%s | patient: %s (%s) | status: %s | started: %s | reason: %s",
                f["conversation_id"], f["patient_name"], f["patient_phone"],
                f["status"], f["started_at"], f["flag_reason"],
            )

    if args.add_patient:
        name = input("Name: ")
        age = int(input("Age: "))
        gender = input("Gender (Male/Female/Other): ")
        phone = input("Phone: ")
        p = create_patient(Patient(name=name, age=age, gender=gender, phone_number=phone))
        logger.info("Added patient: %s", p)

    if args.book_appointment:
        pid = int(input("Patient ID: "))
        did = int(input("Dentist ID: "))
        appt_time = input("Appointment time (YYYY-MM-DD HH:MM): ")
        appt = create_appointment(
            Appointment(patient_id=pid, dentist_id=did, appointment_time=datetime.fromisoformat(appt_time), status="scheduled")
        )
        logger.info("Appointment booked: %s", appt)
