"""
Aggregation queries behind the staff-facing clinic dashboard (see
app/dashboard.py). Kept in its own module, separate from shared/db.py's
one-row-at-a-time CRUD queries, since these are read-only reporting
queries that combine several tables at once.

Like the rest of shared/, these functions run against whichever database
shared/db.get_db_path() currently resolves to. The caller (app/dashboard.py)
is responsible for calling shared.db.set_current_db_path(clinic["db_path"])
first, exactly the way a ClinicWorker does -- because that's a
contextvars.ContextVar, one FastAPI request reading clinic A's dashboard can
never see clinic B's data, even if both are requested at the same instant.

What counts as "this period" throughout: conversations by their started_at,
appointments by when they were BOOKED (created_at) -- not appointment_time,
which is the future slot the patient is booked into -- and patients by when
they first messaged (created_at). Rows written before these created_at
columns existed are NULL and are excluded from period-based counts rather
than guessed at; see the migration notes in shared/db.py.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from shared.db import db

logger = logging.getLogger(__name__)

# How many rows the (possibly filtered) recent-conversations feed will
# return at most. A clinic with a long history + a wide date range could
# have a lot more than this; the note in the UI tells staff to narrow the
# filter rather than silently truncating without saying so.
RECENT_CONVERSATIONS_CAP = 100

# How many trailing calendar months the "conversations by month" chart
# covers. Independent of the days= period selector and of the name/date
# list filters -- it's a trend view, not a period KPI or a search result.
TREND_MONTHS = 6


def _period_cutoff(days: Optional[int]) -> Optional[str]:
    """None means all-time (no cutoff). Otherwise an ISO timestamp for
    "days ago from now", used as a WHERE started_at/created_at >= ? bound."""
    if days is None:
        return None
    return (datetime.now() - timedelta(days=days)).isoformat()


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """'2026-09-18' -> a datetime at midnight that day, or None if
    date_str is empty/malformed. A bad query param shouldn't 500 the
    dashboard -- it just falls back to "no bound on this side"."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _booked_during(conn, patient_id: Optional[int], started_at: str, ended_at: Optional[str]) -> bool:
    """Heuristic: did this patient have an appointment created during this
    conversation's own timeframe (its started_at through its ended_at, or
    through now if still open)? Good enough for a front-desk feed; not
    meant as an exact attribution system."""
    if patient_id is None:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM appointments
        WHERE patient_id = ? AND created_at IS NOT NULL AND created_at >= ?
          AND (? IS NULL OR created_at <= ?)
        LIMIT 1
        """,
        (patient_id, started_at, ended_at, ended_at),
    ).fetchone()
    return row is not None


def get_dashboard_summary(
    days: Optional[int] = 7,
    name_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    schedule_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds everything the staff dashboard shows, in one call: top-line KPIs,
    today's live schedule, conversations needing staff attention, a recent
    conversations feed (including patients who reached out but never
    booked), the most-requested services, bookings broken down by doctor
    and by specialty, message volume by hour of day, a conversation-outcomes
    breakdown, and a 6-month conversation-volume trend -- all scoped to the
    last `days` days, or all-time if `days` is None.

    name_filter / date_from / date_to narrow the two conversation LISTS
    only (needs_attention, recent_conversations) -- they never affect the
    KPI numbers or the insight charts, which stay scoped to `days`.
    date_from/date_to (each "YYYY-MM-DD", either or both) pick an exact day
    (set both to the same date) or an inclusive range, and OVERRIDE the
    `days` scope for those two lists -- a clinic can have thousands of
    conversations, so a day/range pick is exact rather than a coarse
    monthly bucket. With no date given, name_filter searches the patient's
    ENTIRE history rather than just the current `days` window -- a name
    search that silently came back empty just because the match happened
    to fall outside "last 7 days" is a search that doesn't work.

    This does more, smaller queries than a single giant SQL statement would
    need -- deliberately, so each piece stays readable and easy to adjust as
    the dashboard grows, rather than one query trying to do everything at
    once. Clinic-scale data (dozens to low hundreds of conversations per
    period) makes this a non-issue performance-wise.

    schedule_date ("YYYY-MM-DD", optional) picks which single day's
    appointments the schedule section shows -- defaults to today when
    absent/malformed. Independent of every other filter on this page: it's
    a "what does tomorrow look like" lookup, not a report window.
    """
    cutoff = _period_cutoff(days)
    name_filter_norm = (name_filter or "").strip().lower() or None
    from_dt = _parse_date(date_from)
    to_dt = _parse_date(date_to)
    has_date_range = bool(from_dt or to_dt)
    schedule_dt = _parse_date(schedule_date) or datetime.now()

    with db() as conn:
        # ---- Conversations in period (KPI + insight-chart scope) ----
        if cutoff:
            conv_rows = conn.execute(
                "SELECT * FROM conversations WHERE started_at >= ? ORDER BY started_at DESC", (cutoff,)
            ).fetchall()
        else:
            conv_rows = conn.execute("SELECT * FROM conversations ORDER BY started_at DESC").fetchall()
        conversations = [dict(r) for r in conv_rows]
        total_conversations = len(conversations)
        flagged = [c for c in conversations if c["flagged_for_staff"]]

        # ---- Appointments (bookings) in period, by created_at (when Sia
        # actually booked it) -- see module docstring on why not
        # appointment_time. ----
        if cutoff:
            appt_rows = conn.execute(
                "SELECT * FROM appointments WHERE created_at IS NOT NULL AND created_at >= ? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        else:
            appt_rows = conn.execute(
                "SELECT * FROM appointments WHERE created_at IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        appointments = [dict(r) for r in appt_rows]
        bookings_made = len(appointments)
        cancelled = len([a for a in appointments if a["status"] == "cancelled"])

        # ---- Patients/contacts in period ----
        if cutoff:
            patient_rows = conn.execute(
                "SELECT * FROM patients WHERE created_at IS NOT NULL AND created_at >= ?", (cutoff,)
            ).fetchall()
        else:
            patient_rows = conn.execute("SELECT * FROM patients WHERE created_at IS NOT NULL").fetchall()
        new_contacts = [dict(r) for r in patient_rows]
        # A patient row is created the moment a new WhatsApp number first
        # messages, with a placeholder name of "New Patient" until they
        # actually complete registration -- so "new_contacts" is everyone
        # who reached out, and "new_patients_registered" is the subset who
        # actually gave their name (closer to what "new customers" means).
        new_patients_registered = [p for p in new_contacts if p["name"] and p["name"] != "New Patient"]

        # ---- Messages: avg-per-conversation + hourly distribution ----
        conv_ids = [c["id"] for c in conversations]
        message_count_by_conv: Dict[int, int] = {}
        hourly = Counter()
        if conv_ids:
            placeholders = ",".join("?" for _ in conv_ids)
            msg_rows = conn.execute(
                f"SELECT conversation_id, created_at FROM messages WHERE conversation_id IN ({placeholders}) "
                f"AND sender IN ('user','agent')",
                conv_ids,
            ).fetchall()
            for m in msg_rows:
                message_count_by_conv[m["conversation_id"]] = message_count_by_conv.get(m["conversation_id"], 0) + 1
                try:
                    hourly[datetime.fromisoformat(m["created_at"]).hour] += 1
                except (ValueError, TypeError):
                    pass
        avg_messages = (sum(message_count_by_conv.values()) / len(conv_ids)) if conv_ids else 0.0

        # ---- Schedule for schedule_dt (defaults to today) -- a specific
        # day's live front-desk view, not a period report; independent of
        # the days= selector and the name/date-range list filters. Includes
        # the patient's age/gender alongside name/phone so reception can
        # see who's coming in without opening each conversation. ----
        schedule_start = schedule_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        schedule_end = schedule_dt.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        schedule_rows = conn.execute(
            """
            SELECT a.id, a.appointment_time, a.status, a.service_name, a.price_sar,
                   d.name as dentist_name, p.name as patient_name, p.phone_number,
                   p.age as patient_age, p.gender as patient_gender
            FROM appointments a
            JOIN dentists d ON d.id = a.dentist_id
            JOIN patients p ON p.id = a.patient_id
            WHERE a.appointment_time >= ? AND a.appointment_time <= ? AND a.status != 'cancelled'
            ORDER BY a.appointment_time
            """,
            (schedule_start, schedule_end),
        ).fetchall()
        schedule_appointments = [dict(r) for r in schedule_rows]

        # ---- Conversation outcomes -- booked / flagged / no booking, over
        # the FULL period (not capped), for the donut chart. Mirrors the
        # same booked-heuristic and flagged-takes-precedence rule the
        # recent-conversations feed uses below, so the chart and the list
        # never disagree about what counts as what. ----
        outcome_booked = outcome_flagged = outcome_no_booking = 0
        for c in conversations:
            if c["flagged_for_staff"]:
                outcome_flagged += 1
            elif _booked_during(conn, c["patient_id"], c["started_at"], c["ended_at"]):
                outcome_booked += 1
            else:
                outcome_no_booking += 1

        # ---- Conversations-by-month trend (last TREND_MONTHS calendar
        # months) -- always this fixed window, independent of every other
        # filter on this page; it's a trend view, not a search result. ----
        now = datetime.now()
        month_keys = []
        y, m = now.year, now.month
        for _ in range(TREND_MONTHS):
            month_keys.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        month_keys.reverse()
        trend_cutoff = datetime(month_keys[0][0], month_keys[0][1], 1).isoformat()
        trend_rows = conn.execute(
            "SELECT substr(started_at, 1, 7) as ym, COUNT(*) as cnt FROM conversations "
            "WHERE started_at >= ? GROUP BY ym",
            (trend_cutoff,),
        ).fetchall()
        counts_by_ym = {r["ym"]: r["cnt"] for r in trend_rows}
        monthly_trend = []
        for (y, m) in month_keys:
            ym = f"{y:04d}-{m:02d}"
            monthly_trend.append({
                "month": ym,
                "label": datetime(y, m, 1).strftime("%b"),
                "count": counts_by_ym.get(ym, 0),
            })

        # ---- The two filterable lists: needs-attention and recent
        # conversations.
        #   - A date range (date_from and/or date_to) picks its own exact
        #     scope and overrides `days` for these two lists -- a specific
        #     day (from==to), an open-ended "since", or a bounded range.
        #   - No date range but a name_filter: search the clinic's WHOLE
        #     history, not just the current `days` window -- see the
        #     docstring above on why.
        #   - Neither: reuse the same days-scoped `conversations` already
        #     fetched above (the original, unfiltered behavior). ----
        if has_date_range:
            clauses, params = [], []
            if from_dt:
                clauses.append("started_at >= ?")
                params.append(from_dt.isoformat())
            if to_dt:
                clauses.append("started_at < ?")
                params.append((to_dt + timedelta(days=1)).isoformat())
            where = " AND ".join(clauses)
            list_rows = conn.execute(
                f"SELECT * FROM conversations WHERE {where} ORDER BY started_at DESC", params
            ).fetchall()
            list_conversations = [dict(r) for r in list_rows]
        elif name_filter_norm:
            list_rows = conn.execute("SELECT * FROM conversations ORDER BY started_at DESC").fetchall()
            list_conversations = [dict(r) for r in list_rows]
        else:
            list_conversations = conversations

        list_conv_ids = [c["id"] for c in list_conversations]
        list_message_count_by_conv: Dict[int, int] = {}
        if list_conv_ids:
            placeholders = ",".join("?" for _ in list_conv_ids)
            for row in conn.execute(
                f"SELECT conversation_id, COUNT(*) as cnt FROM messages "
                f"WHERE conversation_id IN ({placeholders}) AND sender IN ('user','agent') "
                f"GROUP BY conversation_id",
                list_conv_ids,
            ).fetchall():
                list_message_count_by_conv[row["conversation_id"]] = row["cnt"]

        def _patient_lookup(patient_id):
            """Returns name/phone/age/gender for a patient, so both the
            needs-attention and recent-conversations lists can show who's
            coming in (age/gender) without a separate query per row."""
            if patient_id is None:
                return {"name": "Unknown", "phone_number": None, "age": None, "gender": None}
            row = conn.execute(
                "SELECT name, phone_number, age, gender FROM patients WHERE id=?", (patient_id,)
            ).fetchone()
            if not row:
                return {"name": "Unknown", "phone_number": None, "age": None, "gender": None}
            return {
                "name": row["name"] or "Unknown",
                "phone_number": row["phone_number"],
                "age": row["age"],
                "gender": row["gender"],
            }

        needs_attention = []
        for c in [c for c in list_conversations if c["flagged_for_staff"]]:
            patient = _patient_lookup(c["patient_id"])
            if name_filter_norm and name_filter_norm not in patient["name"].lower():
                continue
            needs_attention.append({
                "conversation_id": c["id"],
                "patient_name": patient["name"],
                "phone_number": patient["phone_number"],
                "patient_age": patient["age"],
                "patient_gender": patient["gender"],
                "reason": c["flag_reason"],
                "channel": c["channel"] if "channel" in c.keys() else "whatsapp",
                "started_at": c["started_at"],
                "status": c["status"],
            })
        needs_attention.sort(key=lambda x: x["started_at"], reverse=True)

        recent_conversations = []
        recent_truncated = False
        for c in list_conversations:
            patient = _patient_lookup(c["patient_id"])
            if name_filter_norm and name_filter_norm not in patient["name"].lower():
                continue
            if len(recent_conversations) >= RECENT_CONVERSATIONS_CAP:
                recent_truncated = True
                break
            booked = _booked_during(conn, c["patient_id"], c["started_at"], c["ended_at"])
            recent_conversations.append({
                "conversation_id": c["id"],
                "patient_name": patient["name"],
                "phone_number": patient["phone_number"],
                "patient_age": patient["age"],
                "patient_gender": patient["gender"],
                "started_at": c["started_at"],
                "ended_at": c["ended_at"],
                "status": c["status"],
                "message_count": list_message_count_by_conv.get(c["id"], 0),
                "flagged": bool(c["flagged_for_staff"]),
                "booked": booked,
                "channel": c["channel"] if "channel" in c.keys() else "whatsapp",
            })

        # ---- Most-requested services (only reflects bookings made after
        # this field shipped -- service_name is NULL on older rows) ----
        service_counter = Counter(a["service_name"] for a in appointments if a["service_name"])
        top_services = [{"service_name": name, "count": count} for name, count in service_counter.most_common(8)]

        # ---- Bookings by doctor / by specialty -- same spirit as
        # top_services above (all bookings in period, including cancelled,
        # so the charts agree on what counts as a "booking"), just grouped
        # by who did the booking instead of what was booked. Lets a clinic
        # see which dentist/specialty is getting the volume. A small,
        # clinic-wide lookup -- one query covers every appointment here. ----
        dentist_rows = conn.execute("SELECT id, name, specialization FROM dentists").fetchall()
        dentist_lookup = {d["id"]: {"name": d["name"], "specialization": d["specialization"]} for d in dentist_rows}
        doctor_counter = Counter()
        specialty_counter = Counter()
        for a in appointments:
            dentist = dentist_lookup.get(a["dentist_id"])
            if not dentist:
                continue
            doctor_counter[(dentist["name"], dentist["specialization"])] += 1
            if dentist["specialization"]:
                specialty_counter[dentist["specialization"]] += 1
        top_doctors = [
            {"dentist_name": name, "specialization": spec, "count": count}
            for (name, spec), count in doctor_counter.most_common(8)
        ]
        top_specialties = [
            {"specialization": spec, "count": count} for spec, count in specialty_counter.most_common(8)
        ]

        # ---- Estimated revenue booked: sum of prices on non-cancelled
        # bookings in period. An estimate of pipeline value, NOT confirmed
        # or collected revenue -- it has no idea about no-shows or whether
        # the patient actually paid. ----
        priced = [a["price_sar"] for a in appointments if a["price_sar"] is not None and a["status"] != "cancelled"]
        estimated_revenue = sum(priced) if priced else None

    conversion_rate = (bookings_made / total_conversations * 100) if total_conversations else 0.0
    escalation_rate = (len(flagged) / total_conversations * 100) if total_conversations else 0.0

    return {
        "period_days": days,
        "period_label": f"Last {days} days" if days else "All time",
        "generated_at": datetime.now().isoformat(),
        "kpis": {
            "conversations": total_conversations,
            "bookings_made": bookings_made,
            "cancelled_appointments": cancelled,
            "conversion_rate_pct": round(conversion_rate, 1),
            "new_contacts": len(new_contacts),
            "new_patients_registered": len(new_patients_registered),
            "flagged_conversations": len(flagged),
            "escalation_rate_pct": round(escalation_rate, 1),
            "avg_messages_per_conversation": round(avg_messages, 1),
            "estimated_revenue_booked_sar": estimated_revenue,
        },
        "schedule_date": schedule_dt.strftime("%Y-%m-%d"),
        "schedule_appointments": schedule_appointments,
        "needs_attention": needs_attention,
        "recent_conversations": recent_conversations,
        "recent_conversations_truncated": recent_truncated,
        "top_services": top_services,
        "top_doctors": top_doctors,
        "top_specialties": top_specialties,
        "hourly_distribution": [{"hour": h, "message_count": hourly.get(h, 0)} for h in range(24)],
        "outcome_breakdown": {
            "booked": outcome_booked,
            "flagged": outcome_flagged,
            "no_booking": outcome_no_booking,
        },
        "monthly_trend": monthly_trend,
        "filters": {
            "name": name_filter or "",
            "from": date_from if from_dt else "",
            "to": date_to if to_dt else "",
            "schedule_date": schedule_dt.strftime("%Y-%m-%d"),
        },
    }
