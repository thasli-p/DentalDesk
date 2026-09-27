"""
Clinic types and departments.

Mawaid started as dental-only. A clinic now has a TYPE (dental,
dermatology, laser, beauty) and can have extra DEPARTMENTS of other types
under the SAME WhatsApp number -- e.g. a dental clinic that also runs a
laser section. The billing rule is simple:

  * One WhatsApp number = one clinic record = one base subscription. That
    base subscription covers the clinic's MAIN department (its own type)
    and, when it runs out, the whole number stops answering (unchanged
    behaviour, see app/main.py's is_subscription_active).
  * Every EXTRA department has its own plan and start date. When an extra
    department's plan runs out, Sia stops booking that department (and says
    so), while the rest of the clinic keeps working.

Everything here is pure data + small helpers with no database access, so
it can be used from the main FastAPI process (admin page, dashboard, voice
endpoints) and from each clinic's MCP tool subprocess alike.

A clinic with no clinic_type and no departments is a plain dental clinic
-- that is every clinic that existed before this module, and they behave
exactly as before.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

DEFAULT_TYPE = "dental"

# Days per subscription plan -- kept in sync with
# shared/clinics_store.SUBSCRIPTION_PLAN_DAYS (duplicated rather than
# imported so the MCP subprocess never has to import the control-plane
# store).
PLAN_DAYS = {
    "trial": 7,
    "1_month": 30,
    "3_months": 90,
    "6_months": 180,
    "12_months": 365,
}

CLINIC_TYPES: Dict[str, Dict[str, str]] = {
    "dental": {
        "label_en": "Dental",
        "label_ar": "الأسنان",
        "practitioner_en": "Dentist",
        "practitioner_ar": "الطبيب",
    },
    "dermatology": {
        "label_en": "Dermatology / Skin",
        "label_ar": "الجلدية",
        "practitioner_en": "Doctor",
        "practitioner_ar": "الطبيب",
    },
    "laser": {
        "label_en": "Laser",
        "label_ar": "الليزر",
        "practitioner_en": "Specialist",
        "practitioner_ar": "الأخصائي",
    },
    "beauty": {
        "label_en": "Beauty / Aesthetics",
        "label_ar": "التجميل",
        "practitioner_en": "Specialist",
        "practitioner_ar": "الأخصائي",
    },
}

# What Sia needs to know per type: how patients talk about services, what
# to check before booking, and which symptoms mean "go to the ER now".
# Written as instructions for the model. Sia never judges medical
# suitability herself -- anything that needs a clinician goes to staff.
TYPE_PROMPT_PACKS: Dict[str, str] = {
    "dental": (
        "Common wording: تنظيف = cleaning, حشوة = filling, عصب/علاج عصب = root canal, تبييض = whitening, "
        "تقويم = orthodontics/braces, خلع = extraction, تركيبة/تلبيسة = crown, زراعة = implant, "
        "كشف/فحص/طبيب أسنان = check-up.\n"
        "Emergencies: severe or uncontrolled bleeding, facial or gum swelling especially with fever, "
        "difficulty breathing or swallowing, a knocked-out or badly broken tooth, a severe injury to the mouth "
        "or jaw, or any symptom the patient describes as severe, worsening fast, or accompanied by fever."
    ),
    "dermatology": (
        "Common wording: كشف جلدية = dermatology consultation, حبوب/حب الشباب = acne, كلف/تصبغات/نمش = "
        "pigmentation, حكة/طفح = itching/rash, إكزيما = eczema, صدفية = psoriasis, تساقط الشعر = hair loss, "
        "شامة/وحمة = mole, ثآليل = warts.\n"
        "Before booking: if the patient is new to this department or the problem is new, book a consultation "
        "(use the consultation service from get_clinic_info if there is one). Never comment on what a skin "
        "problem might be or how serious it is.\n"
        "A mole that is changing, bleeding or growing: book the earliest consultation and call flag_for_staff.\n"
        "Emergencies: swelling of the face, lips, tongue or throat, difficulty breathing, a rash spreading fast "
        "with fever, large areas of blistering or peeling skin, a severe burn, or any symptom the patient "
        "describes as severe or worsening fast."
    ),
    "laser": (
        "Common wording: ليزر/إزالة شعر = laser hair removal, جلسة = session, فل بدي = full body, "
        "ليزر كربوني = carbon laser peel, تقشير = peel, آثار حبوب = acne scars.\n"
        "Before booking a first laser session: ask if this is their first laser session at this clinic. First-"
        "timers get a consultation/patch test first when the services list has one. If the patient mentions "
        "pregnancy or breastfeeding, a recent sun tan, a skin condition, or skin medication (e.g. Roaccutane), "
        "do NOT decide whether laser is safe for them: book a consultation (or tell them staff will confirm) "
        "and call flag_for_staff with what they said.\n"
        "Laser is usually a course of sessions a few weeks apart. If the patient asks, book their next "
        "session like any other appointment; the specialist decides the exact plan.\n"
        "Emergencies after a session: burns or blisters, severe pain or swelling, or signs of infection "
        "(spreading redness, pus, fever), or any symptom the patient describes as severe or worsening fast."
    ),
    "beauty": (
        "Common wording: بوتوكس = botox, فيلر = filler, نضارة/هيدرافيشل = facial/hydrafacial, "
        "تنظيف بشرة = skin cleaning, بلازما = PRP, خيوط = thread lift, ميزوثيرابي = mesotherapy.\n"
        "Before booking injectables (botox, filler, threads) for someone new: book a consultation first when "
        "the services list has one. Injectables are for adults only -- if the patient says they are under 18, "
        "do not book them; tell them staff will follow up and call flag_for_staff. Never promise results or "
        "comment on whether a procedure is safe for them.\n"
        "Emergencies after a procedure: vision changes, skin turning white, blue or grey, severe or worsening "
        "pain or swelling, difficulty breathing or swallowing, or signs of infection with fever."
    ),
}


def normalize_type(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return value if value in CLINIC_TYPES else DEFAULT_TYPE


def type_label(type_key: str, lang: str = "en") -> str:
    info = CLINIC_TYPES.get(normalize_type(type_key), CLINIC_TYPES[DEFAULT_TYPE])
    return info["label_ar"] if lang == "ar" else info["label_en"]


def dept_key(name: str) -> str:
    import re
    key = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return key or "department"


def plan_active(plan: Optional[str], started_at: Optional[str], now: Optional[datetime] = None) -> bool:
    """Same rule as the whole-clinic subscription: unknown/missing values
    count as active so a typo never blocks a paying clinic."""
    days = PLAN_DAYS.get((plan or "").strip())
    if not days or not started_at:
        return True
    try:
        start = datetime.strptime(str(started_at).strip(), "%Y-%m-%d")
    except ValueError:
        return True
    return (now or datetime.now()) < start + timedelta(days=days)


def plan_ends_on(plan: Optional[str], started_at: Optional[str]) -> Optional[str]:
    days = PLAN_DAYS.get((plan or "").strip())
    if not days or not started_at:
        return None
    try:
        return (datetime.strptime(str(started_at).strip(), "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def departments(clinic: Dict[str, Any], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every department of this clinic, main one first.

    `clinic` is either a clinics_store row (keys clinic_type, departments)
    or the `clinic` section of a clinic_config.yaml merged with its
    top-level `departments` list -- both use the same key names.

    Each entry: key, name, type, main (bool), plan, started_at, ends_on,
    active (bool), women_only (bool).
    """
    main_type = normalize_type(clinic.get("clinic_type"))
    result = [{
        "key": main_type,
        "name": clinic.get("main_department_name") or type_label(main_type),
        "type": main_type,
        "main": True,
        "plan": None,
        "started_at": None,
        "ends_on": None,
        "active": True,  # the base subscription is enforced for the whole number in app/main.py
        "women_only": bool(clinic.get("main_women_only")),
    }]
    seen = {main_type}
    for raw in clinic.get("departments") or []:
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        key = dept_key(raw.get("key") or name)
        if key in seen:
            continue
        seen.add(key)
        plan = raw.get("subscription_plan") or raw.get("plan")
        started = raw.get("subscription_started_at") or raw.get("started_at")
        result.append({
            "key": key,
            "name": name,
            "type": normalize_type(raw.get("type")),
            "main": False,
            "plan": plan,
            "started_at": started,
            "ends_on": plan_ends_on(plan, started),
            "active": plan_active(plan, started, now),
            "women_only": bool(raw.get("women_only")),
        })
    return result


def is_multi(clinic: Dict[str, Any]) -> bool:
    return len(departments(clinic)) > 1


def is_plain_dental(clinic: Dict[str, Any]) -> bool:
    """True for a dental-only clinic -- the original Mawaid setup, which
    keeps its original wording everywhere."""
    depts = departments(clinic)
    return len(depts) == 1 and depts[0]["type"] == "dental"


def find_department(clinic: Dict[str, Any], value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Match a practitioner's/service's department field (key or name, any
    case) to a department. Blank = the main department."""
    depts = departments(clinic)
    value = (value or "").strip()
    if not value:
        return depts[0]
    lowered = value.lower()
    for d in depts:
        if lowered in (d["key"], d["name"].lower(), d["type"]) or dept_key(value) == d["key"]:
            return d
    return None


def practitioner_label(clinic: Dict[str, Any], lang: str = "en") -> str:
    if is_plain_dental(clinic):
        return "Dentist" if lang == "en" else "الطبيب"
    return "Practitioner" if lang == "en" else "الأخصائي / الطبيب"


def is_male(gender: Optional[str]) -> bool:
    return (gender or "").strip().lower() in ("male", "m", "man", "ذكر", "رجل", "ولد")


def booking_block(clinic: Dict[str, Any], practitioner_department: Optional[str],
                  patient_gender: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Why this practitioner can't be booked right now because of their
    DEPARTMENT (not their schedule), or None if fine."""
    if not is_multi(clinic):
        return None
    dept = find_department(clinic, practitioner_department)
    if dept is None:
        return None  # unknown department text -- don't block a booking over a typo
    if not dept["active"]:
        return {
            "error": "department_unavailable",
            "details": f"Bookings for the {dept['name']} department are not available through this channel "
                       f"right now. Tell the patient to call the clinic directly to book it. Do not book "
                       f"another department's practitioner instead.",
            "department": dept["name"],
        }
    if dept["women_only"] and is_male(patient_gender):
        return {
            "error": "women_only_department",
            "details": f"The {dept['name']} department serves women only. Tell the patient politely; do not book it.",
            "department": dept["name"],
        }
    return None


def prompt_section(clinic: Dict[str, Any]) -> str:
    """The extra system-prompt block for a clinic that isn't plain dental:
    which departments exist, how to route, and each type's rules."""
    depts = departments(clinic)
    lines = ["CLINIC DEPARTMENTS — this clinic has these departments on this one WhatsApp number:"]
    for d in depts:
        status = "available" if d["active"] else "NOT bookable here right now (patient must call the clinic)"
        extra = ", women only" if d["women_only"] else ""
        lines.append(f"- {d['name']} (department key '{d['key']}', type: {type_label(d['type'])}{extra}) — {status}")
    lines.append(
        "\nROUTING — work out from what the patient asks which department they need (ask one short question "
        "only if it's genuinely unclear). Use `list_dentists` with `department` set to that department key to "
        "get its practitioners (the tool is called list_dentists for every department — its results are "
        "that department's doctors/specialists). Only book a practitioner from the department that provides "
        "the service; never book one department's practitioner for another department's service. In get_clinic_info, "
        "services that list a `department` belong to that department; services without one belong to the "
        "main department. If `book_appointment` returns department_unavailable or women_only_department, "
        "follow its details exactly.\n"
        "If a patient asks for a female (or male) practitioner, respect it: check each practitioner's gender "
        "in list_dentists. Never guess a patient's gender from their name alone."
    )
    for type_key in dict.fromkeys(d["type"] for d in depts):
        lines.append(f"\n{type_label(type_key).upper()} RULES —\n{TYPE_PROMPT_PACKS[type_key]}")
    lines.append(
        "\nFor any emergency listed above, follow the RED-FLAG EMERGENCIES steps below exactly."
    )
    return "\n".join(lines)
