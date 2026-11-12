"""
Tests for the shared AI endpoint resolution (chatgptxblock.ai_endpoint).

Same pattern as test_course_overview.py: runs in a throwaway python:3.11
container (xmodule.modulestore.django is unavailable outside the LMS, so it
is satisfied by a stub module injected into sys.modules):

  docker run --rm -v "$(pwd):/workspace" -w /workspace python:3.11-slim \
    bash -c "pip install -q -e . pytest 'edx-opaque-keys' 'xblock-utils' && \
             pytest test_ai_endpoint.py -v"
"""

import sys
import types

import pytest
from opaque_keys.edx.keys import CourseKey
from xblock.field_data import DictFieldData
from xblock.fields import ScopeIds

from chatgptxblock.ai_endpoint import (
    AI_SETTINGS_KEY,
    PLACEHOLDER_API_KEY,
    UNCONFIGURED_ERROR,
    get_course_ai_settings,
    get_course_block,
    resolve_endpoint,
)


COURSE_KEY = CourseKey.from_string("course-v1:epfl+cs101+2026")


class FakeCourse:
    def __init__(self, other_course_settings=None):
        self.other_course_settings = other_course_settings


class FakeModulestore:
    def __init__(self, course):
        self.course = course

    def get_course(self, course_key):
        return self.course


def install_modulestore_stub(store):
    mod = types.ModuleType("xmodule.modulestore.django")
    mod.modulestore = lambda: store
    sys.modules["xmodule.modulestore.django"] = mod


STUBBED_MODULES = ["xmodule.modulestore.django"]


def uninstall(mod_names):
    for name in mod_names:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def with_stubs():
    install_modulestore_stub(FakeModulestore(FakeCourse()))
    yield
    uninstall(STUBBED_MODULES)


def course_with(settings_section):
    course = FakeCourse({AI_SETTINGS_KEY: settings_section} if settings_section else None)
    install_modulestore_stub(FakeModulestore(course))
    return course


# ---------------------------------------------------------------------------
# resolve_endpoint layering
# ---------------------------------------------------------------------------


def test_unit_settings_win_over_everything():
    course_with({"base_url": "https://course.example.com/v1", "api_key": "course-key"})
    base_url, api_key, error = resolve_endpoint(
        course=None, unit_base_url="https://unit.example.com/v1", unit_api_key="unit-key"
    )
    assert error is None
    assert base_url == "https://unit.example.com/v1"
    assert api_key == "unit-key"


def test_course_advanced_settings_used_when_unit_empty():
    course = course_with({"base_url": "https://course.example.com/v1", "api_key": "course-key"})
    base_url, api_key, error = resolve_endpoint(course=course)
    assert error is None
    assert base_url == "https://course.example.com/v1"
    assert api_key == "course-key"


def test_unconfigured_returns_error_not_public_openai():
    course_with(None)
    base_url, api_key, error = resolve_endpoint(course=None)
    assert base_url is None
    assert api_key is None
    assert error == UNCONFIGURED_ERROR


def test_placeholder_api_key_is_treated_as_unset():
    # The Studio default must not shadow the course layer.
    course = course_with({"api_key": "course-key"})
    _base_url, api_key, error = resolve_endpoint(
        course=course, unit_base_url="https://unit.example.com/v1", unit_api_key=PLACEHOLDER_API_KEY
    )
    assert error is None
    assert api_key == "course-key"


def test_api_key_may_be_empty_when_only_base_url_configured():
    course = course_with({"base_url": "https://course.example.com/v1"})
    _base_url, api_key, error = resolve_endpoint(course=course)
    assert error is None
    assert api_key == ""


def test_whitespace_only_values_are_ignored():
    course_with({"base_url": "   ", "api_key": "  "})
    base_url, api_key, error = resolve_endpoint(
        course=None, unit_base_url="  ", unit_api_key=" "
    )
    assert error == UNCONFIGURED_ERROR
    assert base_url is None


def test_malformed_course_settings_fail_soft():
    course = FakeCourse({AI_SETTINGS_KEY: "not-a-dict"})
    install_modulestore_stub(FakeModulestore(course))
    base_url, api_key, error = resolve_endpoint(course=course)
    assert error == UNCONFIGURED_ERROR


def test_non_dict_section_is_rejected():
    course = FakeCourse({AI_SETTINGS_KEY: ["x"]})
    assert get_course_ai_settings(course) == {}


# ---------------------------------------------------------------------------
# get_course_block
# ---------------------------------------------------------------------------


def test_get_course_block_returns_course():
    course = FakeCourse()
    install_modulestore_stub(FakeModulestore(course))
    assert get_course_block(COURSE_KEY) is course


def test_get_course_block_none_without_key():
    assert get_course_block(None) is None


def test_get_course_block_fails_soft_without_modulestore():
    uninstall(["xmodule.modulestore.django"])
    assert get_course_block(COURSE_KEY) is None


# ---------------------------------------------------------------------------
# XBlock integration: get_openai_client uses the shared resolver
# ---------------------------------------------------------------------------


@pytest.fixture
def xblock_env():
    from chatgptxblock.chatgptxblock import ChatgptXBlock

    def make(fields):
        runtime = types.SimpleNamespace(course_id=COURSE_KEY)
        scope_ids = ScopeIds(
            user_id="student1",
            block_type="chatgptxblock",
            def_id="def1",
            usage_id="i4x://epfl/cs101/chatgptxblock/bot1",
        )
        return ChatgptXBlock(runtime, DictFieldData(fields), scope_ids)

    return make


def test_xblock_client_uses_unit_fields(xblock_env):
    block = xblock_env(
        {"base_url": "https://unit.example.com/v1", "api_key": "unit-key"}
    )
    client = block.get_openai_client()
    assert type(client).__name__ == "OpenAI"
    assert client.base_url.host == "unit.example.com"
    assert client.api_key == "unit-key"


def test_xblock_client_falls_back_to_course_settings(xblock_env):
    course_with({"base_url": "https://course.example.com/v1", "api_key": "course-key"})
    block = xblock_env({"api_key": PLACEHOLDER_API_KEY})
    client = block.get_openai_client()
    assert type(client).__name__ == "OpenAI"
    assert client.base_url.host == "course.example.com"
    assert client.api_key == "course-key"


def test_xblock_client_unconfigured_returns_error(xblock_env):
    block = xblock_env({"api_key": PLACEHOLDER_API_KEY})
    result = block.get_openai_client()
    assert result == {"error": UNCONFIGURED_ERROR}


def test_xblock_empty_api_key_uses_empty_sentinel(xblock_env):
    course_with({"base_url": "https://course.example.com/v1"})
    block = xblock_env({"api_key": ""})
    client = block.get_openai_client()
    assert type(client).__name__ == "OpenAI"
    assert client.api_key == "EMPTY"
