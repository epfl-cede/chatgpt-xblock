"""
Shared resolution of the AI endpoint (base URL + API key) for every chat
surface in this package: XBlock units today, the course-tab backend later.

Resolution order, most specific first:

1. Unit-level Studio settings (XBlock ``api_key`` / ``base_url`` fields) -
   XBlock only; the course-tab backend has no unit and starts at layer 2.
2. Course-level Advanced Settings: ``other_course_settings`` JSON, key
   ``ai_assistant`` -> ``{"base_url": ..., "api_key": ...}``.

Hard rule: nothing here falls back to the public OpenAI API. If no layer
resolves a base URL the caller gets ``None`` and must reject the request
with a clear message. The Studio placeholder API key is treated as unset
so it cannot shadow the course layer.
"""

# Key inside the course's Advanced Settings "other_course_settings" JSON.
AI_SETTINGS_KEY = "ai_assistant"

# Historical placeholder shipped as the api_key field default; must never
# be sent to any endpoint.
PLACEHOLDER_API_KEY = "your-openai-api-key-here"

# Error returned when no layer resolves a base URL. Wording covers both
# surfaces (unit settings, and the course Advanced Settings shared with
# the course-tab backend).
UNCONFIGURED_ERROR = (
    "No AI endpoint configured. Set the API base URL in the component "
    "settings, or in the course Advanced Settings under "
    "other_course_settings (key ai_assistant)."
)


def _clean(value):
    """None-safe strip; "" stays falsy."""
    return (value or "").strip()


def _clean_key(value):
    """Strip, and treat the Studio placeholder as unset at its own layer,
    so the course-settings layer can supply the key."""
    value = _clean(value)
    return "" if value == PLACEHOLDER_API_KEY else value


def get_course_ai_settings(course):
    """
    The course-level "ai_assistant" dict from the course's Advanced Settings
    (other_course_settings JSON). Empty dict on any failure - course-level
    settings are optional enrichment, never a hard dependency.
    """
    if course is None:
        return {}
    try:
        raw = getattr(course, "other_course_settings", None) or {}
        section = raw.get(AI_SETTINGS_KEY) or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def get_course_block(course_key):
    """
    Fetch the course block for course-level settings. Returns None outside
    the LMS/CMS or if the modulestore hiccups - callers must degrade to the
    remaining resolution layers.
    """
    if course_key is None:
        return None
    try:
        from xmodule.modulestore.django import modulestore

        return modulestore().get_course(course_key)
    except Exception:
        return None


def resolve_endpoint(course=None, unit_base_url="", unit_api_key=""):
    """
    Resolve (base_url, api_key, error) for one request.

    course is the CourseBlock (or anything carrying other_course_settings);
    the unit_* arguments are the XBlock field values, "" when absent (e.g.
    the course-tab backend, which has no unit).

    Returns (base_url, api_key, None) on success; (None, None, UNCONFIGURED_ERROR)
    when no layer resolves a base URL. The API key may be "" when only a
    base URL is configured (some gateways authenticate by network route).
    """
    course_settings = get_course_ai_settings(course)

    base_url = (
        _clean(unit_base_url)
        or _clean(course_settings.get("base_url"))
    )

    api_key = (
        _clean_key(unit_api_key)
        or _clean_key(course_settings.get("api_key"))
    )

    if not base_url:
        return None, None, UNCONFIGURED_ERROR

    return base_url, api_key, None
