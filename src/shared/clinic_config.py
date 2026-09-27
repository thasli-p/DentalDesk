"""
Loads the clinic's editable settings from config/clinic_config.yaml —
services & prices, general hours, parking, phone, holidays, and the list
of dentists.

Non-developers edit config/clinic_config.yaml directly to change any of
this; this module just reads that file so the rest of the app can use it.
The file is re-read fresh each time it's requested (see note on caching
below), so edits take effect the next time the app is started.
"""
import os
import logging
from typing import Any, Dict, List

import yaml

logger = logging.getLogger(__name__)

# Multi-clinic support: each clinic's MCP tool process is launched (see
# app/agent.py's ClinicWorker) with CLINIC_CONFIG_PATH set in its own
# environment, pointing at that one clinic's own clinic_config.yaml file
# (written by the Add Clinic admin page into data/clinics/<slug>/). Since
# each clinic gets its own separate OS process for this, reading a plain
# environment variable here is safe -- unlike shared/db.py, this module is
# never called from the main multi-clinic FastAPI process, only from
# inside a single clinic's own MCP subprocess, so no contextvars are
# needed. Falls back to the original single-clinic path when unset, which
# is what keeps local development and any non-migrated deployment working
# exactly as before.
CONFIG_PATH = os.environ.get("CLINIC_CONFIG_PATH") or os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "clinic_config.yaml"
)

# Cached after the first read for this run of the app. Restart the app
# (or the MCP server) to pick up edits made to clinic_config.yaml.
_cached_config: Dict[str, Any] | None = None


def load_clinic_config() -> Dict[str, Any]:
    """Reads (and caches) config/clinic_config.yaml."""
    global _cached_config
    if _cached_config is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cached_config = yaml.safe_load(f) or {}
        logger.info("Loaded clinic config from %s", CONFIG_PATH)
    return _cached_config


def get_clinic_info() -> Dict[str, Any]:
    """Clinic-level info: name, general_hours, parking, phone, holidays."""
    return load_clinic_config().get("clinic", {})


def get_services() -> List[Dict[str, Any]]:
    """The list of services and prices."""
    return load_clinic_config().get("services", [])


def get_clinic_profile() -> Dict[str, Any]:
    """Clinic type + departments in the shape shared/clinic_types.py
    expects (the `clinic` section plus the top-level `departments` list).
    A config written before clinic types existed has neither, which means
    a plain dental clinic."""
    config = load_clinic_config()
    return {**(config.get("clinic") or {}), "departments": config.get("departments") or []}


def get_dentists_config() -> List[Dict[str, Any]]:
    """The list of dentists defined in the config file."""
    return load_clinic_config().get("dentists", [])


def get_offers_image_url() -> str | None:
    """
    Builds the full public URL for the offers/promotions brochure image
    (served by the app as a static file from the `static/` folder), so it
    can be sent to patients over WhatsApp — WhatsApp requires a real,
    publicly reachable URL, not a local file path.

    Returns None if there's no public base URL to build from. In
    production on Railway this is automatic: once a public domain is
    generated, Railway sets RAILWAY_PUBLIC_DOMAIN and this "just works".
    For local dev (e.g. testing through ngrok), set PUBLIC_BASE_URL in
    .env to your ngrok URL (e.g. "https://your-tunnel.ngrok-free.dev") to
    test this feature locally — otherwise it's simply skipped locally.
    """
    clinic = get_clinic_info()
    filename = clinic.get("offers_image_filename")
    if not filename:
        return None

    base_url = os.environ.get("PUBLIC_BASE_URL")
    if not base_url:
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        if railway_domain:
            base_url = f"https://{railway_domain}"
    if not base_url:
        return None

    # Legacy single-clinic deployments serve from /static (see app/main.py's
    # original StaticFiles mount). A clinic added through the Add Clinic
    # admin page instead has its own uploads folder, served at its own URL
    # prefix -- CLINIC_STATIC_URL_PREFIX is set in that clinic's MCP
    # subprocess environment (see app/agent.py) to something like
    # "/clinic-static/<slug>" so its brochure image doesn't collide with
    # any other clinic's file of the same name.
    url_prefix = os.environ.get("CLINIC_STATIC_URL_PREFIX", "/static")
    return f"{base_url.rstrip('/')}{url_prefix.rstrip('/')}/{filename}"
