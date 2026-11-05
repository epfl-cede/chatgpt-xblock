"""
Tests for _collect_course_overview (course-level system prompt enrichment).

Same pattern as test_course_context.py: runs in a throwaway python:3.11
container (xmodule.modulestore.django and django.core.cache are unavailable
outside the LMS, so both are satisfied by stub modules injected into
sys.modules):

  docker run --rm -v "$(pwd):/workspace" -w /workspace python:3.11-slim \
    bash -c "pip install -q -e . pytest 'edx-opaque-keys' 'xblock-utils' && \
             pytest test_course_overview.py -v"
"""

import sys
import types
from datetime import datetime, timezone

import pytest
from opaque_keys.edx.keys import CourseKey
from xblock.core import XBlock
from xblock.field_data import DictFieldData
from xblock.fields import ScopeIds

from chatgptxblock.chatgptxblock import (
    COURSE_OVERVIEW_CACHE_TTL,
    COURSE_OVERVIEW_EMPTY_CACHE_TTL,
    COURSE_OVERVIEW_MAX_CHAPTERS,
    COURSE_OVERVIEW_MAX_SUBSECTIONS,
    ChatgptXBlock,
)

COURSE_KEY = CourseKey.from_string("course-v1:epfl+cs101+2026")
CHAP1_LOC = "i4x://epfl/cs101/chapter/chap1"
CHAP2_LOC = "i4x://epfl/cs101/chapter/chap2"
SEQ1_LOC = "i4x://epfl/cs101/sequential/seq1"
SEQ2_LOC = "i4x://epfl/cs101/sequential/seq2"


class FakeBlock:
    def __init__(self, category, name, data="", children=()):
        self.category = category
        self.display_name = name
        self.data = data
        self.children = list(children)

    def get_children(self):
        # Real modulestore get_children() returns child *blocks*, not locators.
        return [BLOCKS[str(loc)] for loc in self.children]


class FakeCourse(FakeBlock):
    def __init__(self):
        super().__init__("course", "Introduction to Botany", children=[CHAP1_LOC, CHAP2_LOC])
        self.start = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.end = datetime(2026, 12, 20, tzinfo=timezone.utc)
        self.enrollment_start = None
        self.enrollment_end = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.self_paced = False


ABOUT = {
    "short_description": "<p>Learn how plants work.</p>",
    "instructor": 'Prof. <a href="mailto:x@epfl.ch">Alice Smith</a>, Bob Jones',
}

BLOCKS = {
    str(COURSE_KEY): FakeCourse(),
    CHAP1_LOC: FakeBlock("chapter", "Week 1", children=[SEQ1_LOC]),
    CHAP2_LOC: FakeBlock("chapter", "Week 2", children=[SEQ2_LOC]),
    SEQ1_LOC: FakeBlock("sequential", "Lesson 1: Leaves"),
    SEQ2_LOC: FakeBlock("sequential", "Lesson 2: Stems"),
    **{
        str(COURSE_KEY.make_usage_key("about", name)): FakeBlock("about", name, data=data)
        for name, data in ABOUT.items()
    },
}


class FakeModulestore:
    def get_course(self, course_key):
        return BLOCKS[str(course_key)]

    def get_item(self, loc):
        return BLOCKS[str(loc)]


def install_modulestore_stub(store):
    mod = types.ModuleType("xmodule.modulestore.django")
    mod.modulestore = lambda: store
    sys.modules["xmodule.modulestore.django"] = mod


class RecordingCache:
    def __init__(self):
        self.data = {}
        self.sets = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, ttl=None):
        self.data[key] = value
        self.sets.append((key, value, ttl))


def install_django_cache_stub(cache_obj):
    django = types.ModuleType("django")
    core = types.ModuleType("django.core")
    cache_mod = types.ModuleType("django.core.cache")
    cache_mod.cache = cache_obj
    django.core = core
    core.cache = cache_mod
    sys.modules["django"] = django
    sys.modules["django.core"] = core
    sys.modules["django.core.cache"] = cache_mod
    return cache_obj


def uninstall(mod_names):
    for name in mod_names:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def with_stubs():
    install_modulestore_stub(FakeModulestore())
    yield
    uninstall(["xmodule.modulestore.django", "django", "django.core", "django.core.cache"])


def make_block(course_key=COURSE_KEY):
    runtime = types.SimpleNamespace(course_id=course_key)
    scope_ids = ScopeIds(
        user_id="student1",
        block_type="chatgptxblock",
        def_id="def1",
        usage_id="i4x://epfl/cs101/chatgptxblock/bot1",
    )
    return ChatgptXBlock(runtime, DictFieldData({}), scope_ids)


def test_full_overview():
    block = make_block()
    ctx = block._collect_course_overview(COURSE_KEY)
    assert "Course: Introduction to Botany (epfl cs101, run 2026)" in ctx
    assert "Start: 15 Sep 2026" in ctx
    assert "End: 20 Dec 2026" in ctx
    assert "Enrollment end: 30 Sep 2026" in ctx
    assert "Enrollment start" not in ctx  # None date omitted
    assert "Description: Learn how plants work." in ctx
    assert "Instructors: Prof. Alice Smith, Bob Jones" in ctx  # tags stripped
    assert "mailto:x@epfl.ch" not in ctx
    assert "- Week 1: Lesson 1: Leaves" in ctx
    assert "- Week 2: Lesson 2: Stems" in ctx


def test_self_paced_flagged():
    course = BLOCKS[str(COURSE_KEY)]
    course.self_paced = True
    try:
        assert "(self-paced)" in make_block()._collect_course_overview(COURSE_KEY)
    finally:
        course.self_paced = False


def test_sentinel_start_omitted():
    """Unset start (2030-01-01 sentinel) must never surface as a real date."""
    # xmodule is not a real package outside edx-platform - stub it like the
    # modulestore stub, with the documented sentinel default.
    xmodule_mod = types.ModuleType("xmodule")
    course_module_mod = types.ModuleType("xmodule.course_module")
    course_module_mod.DEFAULT_START_DATE = datetime(2030, 1, 1)
    sys.modules.setdefault("xmodule", xmodule_mod)
    sys.modules["xmodule.course_module"] = course_module_mod

    course = BLOCKS[str(COURSE_KEY)]
    original_start = course.start
    original_enrollment_end = course.enrollment_end
    course.start = datetime(2030, 1, 1)
    course.enrollment_end = datetime(2030, 1, 1)
    try:
        ctx = make_block()._collect_course_overview(COURSE_KEY)
        assert "Start:" not in ctx
        assert "Enrollment end:" not in ctx
        assert "2030" not in ctx
        assert "End: 20 Dec 2026" in ctx  # real dates survive
    finally:
        course.start = original_start
        course.enrollment_end = original_enrollment_end
        sys.modules.pop("xmodule.course_module", None)


def test_sentinel_check_without_xmodule():
    """The sentinel check must not depend on xmodule being importable."""
    import builtins

    sys.modules.pop("xmodule.course_module", None)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "xmodule.course_module":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "__import__", fake_import)
        from chatgptxblock.chatgptxblock import _is_default_start

        assert _is_default_start(datetime(2030, 1, 1))
        assert not _is_default_start(datetime(2026, 9, 15, tzinfo=timezone.utc))
        assert not _is_default_start(None)
        assert not _is_default_start("Fall 2026")


def test_missing_about_items_omitted():
    BLOCKS.pop(str(COURSE_KEY.make_usage_key("about", "instructor")))
    try:
        ctx = make_block()._collect_course_overview(COURSE_KEY)
        assert "Instructors" not in ctx
        assert "Description" in ctx
    finally:
        BLOCKS[str(COURSE_KEY.make_usage_key("about", "instructor"))] = FakeBlock(
            "about", "instructor", data=ABOUT["instructor"]
        )


def test_overview_char_cap():
    ctx = make_block()._collect_course_overview(COURSE_KEY, max_chars=80)
    assert len(ctx) <= 80


def test_subsection_cap():
    chapter_loc = "i4x://epfl/cs101/chapter/capped"
    sub_locs = [
        "i4x://epfl/cs101/sequential/capped{}".format(i)
        for i in range(COURSE_OVERVIEW_MAX_SUBSECTIONS + 3)
    ]
    for i, loc in enumerate(sub_locs):
        BLOCKS[loc] = FakeBlock("sequential", "Sub {}".format(i))
    BLOCKS[chapter_loc] = FakeBlock(
        "chapter", "Capped chapter", children=sub_locs
    )
    course = BLOCKS[str(COURSE_KEY)]
    original = course.children
    course.children = [chapter_loc]
    try:
        ctx = make_block()._collect_course_overview(COURSE_KEY)
        assert "Sub {}".format(COURSE_OVERVIEW_MAX_SUBSECTIONS - 1) in ctx
        assert "Sub {}".format(COURSE_OVERVIEW_MAX_SUBSECTIONS) not in ctx
    finally:
        course.children = original
        BLOCKS.pop(chapter_loc, None)
        for loc in sub_locs:
            BLOCKS.pop(loc, None)


def test_build_messages_toggle_on():
    block = make_block()
    block._course_overview_cached = lambda: "COURSE OVERVIEW"
    block._collect_course_context = lambda: "PAGE CONTEXT"
    block.conversation_history = [{"role": "user", "content": "earlier"}]

    messages = block._build_messages("Q?")
    assert messages == [
        {"role": "system", "content": block.context_text},
        {
            "role": "system",
            "content": "General information about this course:\nCOURSE OVERVIEW",
        },
        {
            "role": "system",
            "content": (
                "The student is currently viewing the following course page. "
                "Ground your answer in this material when relevant:\nPAGE CONTEXT"
            ),
        },
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "Q?"},
    ]


def test_build_messages_toggle_off():
    block = make_block()
    block.include_course_overview = False
    called = []

    def render():
        called.append(1)
        return "COURSE OVERVIEW"

    block._course_overview_cached = render
    block._collect_course_context = lambda: "PAGE CONTEXT"

    messages = block._build_messages("Q?")
    assert not called, "overview must not be rendered when the toggle is off"
    assert all("COURSE OVERVIEW" not in m["content"] for m in messages)
    assert any("PAGE CONTEXT" in m["content"] for m in messages)


def test_cache_failure_falls_back_to_direct_render():
    class BrokenCache:
        def get(self, key):
            raise ConnectionError("cache down")

        def set(self, key, value, ttl=None):
            raise ConnectionError("cache down")

    install_django_cache_stub(BrokenCache())
    block = make_block()
    assert "Introduction to Botany" in block._course_overview_cached()


def test_cached_overview_set_and_reused():
    cache = install_django_cache_stub(RecordingCache())
    block = make_block()
    first = block._course_overview_cached()
    assert cache.sets, "expected cache.set on first render"
    key, value, ttl = cache.sets[0]
    assert "chatgpt-xblock:course-overview:" in key
    assert ttl == COURSE_OVERVIEW_CACHE_TTL
    assert value == first
    # Second call served from cache without re-rendering.
    cache.sets.clear()
    assert block._course_overview_cached() == first
    assert not cache.sets


def test_empty_overview_cached_with_short_ttl():
    """An empty (failed) overview must be cached too - with the short
    recovery TTL, not the full hour - and served from cache afterwards."""
    cache = install_django_cache_stub(RecordingCache())
    block = make_block()
    block._collect_course_overview = lambda course_key: ""

    assert block._course_overview_cached() == ""
    assert cache.sets, "empty overview must be cached"
    key, value, ttl = cache.sets[0]
    assert value == ""
    assert ttl == COURSE_OVERVIEW_EMPTY_CACHE_TTL

    # Second call served from cache without re-rendering.
    cache.sets.clear()
    assert block._course_overview_cached() == ""
    assert not cache.sets


def test_chapter_cap():
    course = BLOCKS[str(COURSE_KEY)]
    original = course.children
    extra = [
        "i4x://epfl/cs101/chapter/extra{}".format(i) for i in range(COURSE_OVERVIEW_MAX_CHAPTERS + 5)
    ]
    for loc in extra:
        BLOCKS[loc] = FakeBlock("chapter", "Extra {}".format(loc[-2:]))
    course.children = original + extra
    try:
        ctx = make_block()._collect_course_overview(COURSE_KEY)
        assert ctx.count("\n- ") == COURSE_OVERVIEW_MAX_CHAPTERS
    finally:
        course.children = original
        for loc in extra:
            BLOCKS.pop(loc, None)
