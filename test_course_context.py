"""
Temporary test for _collect_course_context (commit 7aa9bfe).

Runs in a throwaway python:3.11 container:
  docker run --rm -v "$(pwd):/workspace" -w /workspace python:3.11-slim \
    bash -c "pip install -q -e . pytest && pytest test_course_context.py -v"

The real xmodule.modulestore.django is unavailable outside the LMS, so the
import inside _collect_course_context is satisfied by a stub module injected
into sys.modules that mimics split-modulestore behaviour (parent walk,
get_item, get_children) with real opaque-key locators.
"""

import sys
import types

import pytest

from xblock.core import XBlock
from xblock.fields import ScopeIds
from xblock.field_data import DictFieldData

from chatgptxblock.chatgptxblock import ChatgptXBlock


# Plain string keys: _collect_course_context treats locations opaquely, so
# fake locators are enough to exercise the parent walk.
COURSE = "course-v1:epfl+cs101+2026"
VERT_LOC = "i4x://epfl/cs101/vertical/vert1"
SEQ_LOC = "i4x://epfl/cs101/sequential/seq1"
CHAP_LOC = "i4x://epfl/cs101/chapter/chap1"
COURSE_LOC = "i4x://epfl/cs101/course/course"
SELF_LOC = "i4x://epfl/cs101/chatgptxblock/bot1"
HTML1_LOC = "i4x://epfl/cs101/html/html1"
PROB1_LOC = "i4x://epfl/cs101/problem/prob1"


class FakeBlock:
    def __init__(self, category, name, data="", children=()):
        self.category = category
        self.display_name = name
        self.data = data
        self.children = list(children)

    def get_children(self):
        return self.children


HTML_DATA = (
    "<h2>Photosynthesis</h2>"
    "<script>alert('evil')</script>"
    "<style>.x{color:red}</style>"
    "<p>Plants convert light into chemical energy.</p>"
)


class FakeModulestore:
    """Mimics the parts of split modulestore used by _collect_course_context."""

    PARENTS = {
        SELF_LOC: VERT_LOC,
        VERT_LOC: SEQ_LOC,
        SEQ_LOC: CHAP_LOC,
        CHAP_LOC: COURSE_LOC,
    }
    BLOCKS = {
        COURSE_LOC: FakeBlock("course", "Introduction to Botany"),
        CHAP_LOC: FakeBlock("chapter", "Week 1"),
        SEQ_LOC: FakeBlock("sequential", "Lesson 1: Leaves"),
        VERT_LOC: FakeBlock(
            "vertical",
            "Photosynthesis unit",
            children=[HTML1_LOC, SELF_LOC, PROB1_LOC],
        ),
        HTML1_LOC: FakeBlock("html", "Reading", data=HTML_DATA),
        SELF_LOC: FakeBlock("chatgptxblock", "AI Assistant"),
        PROB1_LOC: FakeBlock(
            "problem", "Check yourself", data="<problem>What is chlorophyll?</problem>"
        ),
    }

    def get_parent_location(self, loc):
        return self.PARENTS.get(loc)

    def get_item(self, loc):
        return self.BLOCKS[loc]


def install_modulestore_stub(store):
    mod = types.ModuleType("xmodule.modulestore.django")
    mod.modulestore = lambda: store
    sys.modules["xmodule.modulestore.django"] = mod


def make_block(runtime_course_id=COURSE):
    runtime = types.SimpleNamespace(course_id=runtime_course_id)
    scope_ids = ScopeIds(
        user_id="student1",
        block_type="chatgptxblock",
        def_id="def1",
        usage_id=SELF_LOC,
    )
    return ChatgptXBlock(runtime, DictFieldData({}), scope_ids)


@pytest.fixture(autouse=True)
def with_modulestore():
    install_modulestore_stub(FakeModulestore())
    yield
    del sys.modules["xmodule.modulestore.django"]


def test_context_collected():
    block = make_block()
    ctx = block._collect_course_context()
    assert ctx, "expected course context, got None (self.category bug?)"
    assert "Introduction to Botany" in ctx
    assert "Week 1" in ctx
    assert "Lesson 1: Leaves" in ctx
    assert "Plants convert light into chemical energy." in ctx
    assert "What is chlorophyll?" in ctx
    assert "alert('evil')" not in ctx  # script stripped
    assert "color:red" not in ctx  # style stripped


def test_no_course_id_gets_placeholder():
    block = make_block(runtime_course_id=None)
    assert block._collect_course_context() == (
        "(No surrounding course page could be determined.)"
    )


def test_context_length_capped():
    block = make_block()
    ctx = block._collect_course_context(max_chars=50)
    assert ctx is not None
    # 50-char body cap + name + ": " + "- " + newline
    assert len(ctx.split("Content on the current page:")[1]) <= 64
