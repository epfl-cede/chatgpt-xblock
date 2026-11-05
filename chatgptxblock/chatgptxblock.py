import re
from datetime import datetime

import pkg_resources
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from xblock.core import XBlock
from xblock.fields import Boolean, Float, Integer, List, Scope, String
from xblock.fragment import Fragment
from xblockutils.studio_editable import StudioEditableXBlockMixin


# Hard caps for the course overview injected into the system prompt.
COURSE_OVERVIEW_MAX_CHARS = 2500
COURSE_OVERVIEW_ABOUT_ITEM_CHARS = 500
COURSE_OVERVIEW_MAX_CHAPTERS = 30
COURSE_OVERVIEW_MAX_SUBSECTIONS = 12
# Django-cache TTLs for the rendered course overview. A full overview is
# stable (dates/description change rarely; the syllabus walk is not free).
# An EMPTY overview is usually a transient failure (modulestore hiccup), so
# it is cached only briefly - enough to deduplicate a burst, short enough to
# recover quickly instead of disabling the feature for an hour.
COURSE_OVERVIEW_CACHE_TTL = 3600
COURSE_OVERVIEW_EMPTY_CACHE_TTL = 60

_CACHE_MISS = object()


def _clean_html(text, limit):
    """Strip script/style blocks and tags, squash whitespace, cap length."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # A tag boundary before punctuation leaves 'Smith ,' - close the gap.
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text[:limit]


def _fmt_date(value):
    """Format a (possibly None) datetime as e.g. '15 Sep 2026'; None otherwise."""
    try:
        return value.strftime("%d %b %Y")
    except Exception:
        return None


def _is_default_start(value):
    """
    True if value is Open edX's 'no real start date' sentinel: CourseFields
    sets start (and anything falling back to it) to 2030-01-01 when the course
    team never picked a date. Wrong facts in the system prompt are the worst
    failure mode, so the sentinel must never surface as 'Start: 01 Jan 2030'.
    Compares tz-naive to survive aware/naive mixes.
    """
    if not isinstance(value, datetime):
        return False
    try:
        from xmodule.course_module import DEFAULT_START_DATE
    except Exception:
        DEFAULT_START_DATE = datetime(2030, 1, 1)
    value = value.replace(tzinfo=None) if value.tzinfo else value
    if DEFAULT_START_DATE.tzinfo:
        DEFAULT_START_DATE = DEFAULT_START_DATE.replace(tzinfo=None)
    return value == DEFAULT_START_DATE


def _clean_name(block):
    """Cleaned, length-capped display_name of a course block."""
    return _clean_html(getattr(block, "display_name", "") or "", 120)


class ChatgptXBlock(StudioEditableXBlockMixin, XBlock):
    """
    An XBlock that integrates with OpenAI's ChatCompletion (ChatGPT).
    Includes conversation, moderation, disclaimers, reflection, and error handling.
    """

    # Studio-editable fields
    display_name = String(
        display_name="Display Name",
        help="Display name for this module",
        default="AI Assistant",
        scope=Scope.settings,
    )
    model_name = String(
        display_name="Model name",
        default="",
        scope=Scope.settings,
        help="Model ID as offered by the API endpoint (free text). Required - ask your platform admin for the available models.",
    )
    api_key = String(
        default="your-openai-api-key-here",
        scope=Scope.settings,
        help="API key for the OpenAI-compatible endpoint",
    )
    base_url = String(
        display_name="API base URL",
        # Resolved per-request by _resolve_base_url(): Studio value wins, then
        # SiteConfiguration (CHATGPT_XBLOCK_BASE_URL key). If the site
        # configuration has no default, this field is mandatory - questions
        # are rejected otherwise rather than hitting the public OpenAI API.
        default="",
        scope=Scope.settings,
        help=(
            "Base URL of an OpenAI-compatible API endpoint (e.g. "
            "https://gateway.example.com/v1). Required unless the platform "
            "admin has set CHATGPT_XBLOCK_BASE_URL in the site configuration."
        ),
    )
    description = String(
        display_name="Description",
        default="",
        scope=Scope.settings,
        help=(
            "Optional short text shown above the chat "
            "(e.g. instructions for the students). Leave empty to hide it."
        ),
    )

    max_tokens = Integer(
        display_name="Max tokens",
        default=150,
        scope=Scope.settings,
        help="The maximum number of tokens to generate.",
    )

    context_text = String(
        default="You are a helpful teaching assistant. Provide concise, factual answers.",
        scope=Scope.settings,
        help="System prompt to guide the AI's behavior.",
    )

    include_course_overview = Boolean(
        display_name="Include course overview",
        default=True,
        scope=Scope.settings,
        help=(
            "Automatically add general course information (name, dates, "
            "description, instructors, course structure) to the AI's "
            "system prompt, in addition to the current page content."
        ),
    )

    temperature = Float(default=0.5, scope=Scope.settings, help="Controls Randomness.")
    # Student-specific fields
    # This will store the conversation as a list of message dictionaries:
    # e.g. [{"role": "user", "content": "My question?"}, {"role": "assistant", "content": "..."}]
    conversation_history = List(
        default=[],
        scope=Scope.user_state,
        help="Keeps track of user and assistant messages for a conversation-like experience.",
    )

    # Make sure the fields that need to be edited in Studio are here
    editable_fields = [
        "display_name",
        "model_name",
        "api_key",
        "base_url",
        "description",
        "context_text",
        "include_course_overview",
        "max_tokens",
        "temperature",
    ]

    def resource_string(self, path):
        """Helper for loading resources from the static folder."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    def _collect_course_context(self, max_chars=4000):
        """
        Collect the course context around this unit: course/section/subsection
        titles plus the text of sibling components in the same vertical, so the
        model can ground its answers in the material the student is looking at.
        Returns a string; a short placeholder if no context is available
        (e.g. workbench, library content, studio preview edge cases).
        """
        store = self._modulestore()
        course_key = self._resolve_course_key()
        if course_key is None:
            return "(No surrounding course page could be determined.)"
        try:
            # Walk up the parent chain: vertical -> sequential -> chapter -> course
            location = self.scope_ids.usage_id
            ancestors = []
            while True:
                parent_loc = store.get_parent_location(location)
                if parent_loc is None:
                    break
                parent = store.get_item(parent_loc)
                ancestors.append(parent)
                location = parent_loc
            if not ancestors:
                return "(No surrounding course page could be determined.)"

            vertical = ancestors[0]
            subsection = ancestors[1].display_name if len(ancestors) > 1 else None
            section = ancestors[2].display_name if len(ancestors) > 2 else None
            course_name = ancestors[-1].display_name

            # Text of the sibling components on the same page (the vertical)
            siblings = []
            remaining = max_chars
            for child_id in vertical.get_children():
                child = store.get_item(child_id)
                if getattr(child, "category", None) == self.scope_ids.block_type:
                    continue  # don't include this xblock itself
                name = _clean_name(child)
                body = ""
                if remaining > 0 and child.category in ("html", "problem", "video"):
                    body = _clean_html(getattr(child, "data", "") or "", remaining)
                if name or body:
                    part = name + (": " + body if body else "")
                    siblings.append("- " + part)
                    remaining -= len(part)
                    if remaining <= 0:
                        break

            parts = ["Course: " + (course_name or "")]
            if section:
                parts.append("Section: " + section)
            if subsection:
                parts.append("Subsection: " + subsection)
            if siblings:
                parts.append("Content on the current page:\n" + "\n".join(siblings))
            return "\n".join(parts)
        except Exception as exc:
            return f"(No surrounding course page could be determined: {type(exc).__name__}: {exc})"

    def _modulestore(self):
        """Return the modulestore, or None outside the LMS/CMS (workbench)."""
        try:
            from xmodule.modulestore.django import modulestore

            return modulestore()
        except Exception:
            return None

    def _resolve_course_key(self):
        """
        Resolve the course key from the usage id (context_key covers both old
        (BlockUsageLocator) and new (UsageKeyV2) runtimes) or the runtime.
        Returns None when unavailable (workbench, library content, ...).
        """
        course_key = getattr(self.scope_ids.usage_id, "context_key", None)
        if course_key is None:
            course_key = getattr(self.runtime, "course_id", None)
        return course_key

    def _about_line(self, store, course_key, about_name, label):
        """
        '<Label>: <cleaned about-page item>' (e.g. 'Description: ...').
        Empty string if the item is missing or anything goes wrong.
        """
        try:
            item = store.get_item(course_key.make_usage_key("about", about_name))
            text = _clean_html(getattr(item, "data", "") or "", COURSE_OVERVIEW_ABOUT_ITEM_CHARS)
            return "{}: {}".format(label, text) if text else ""
        except Exception:
            return ""

    def _course_overview_heading(self, course, course_key):
        """'Course: <name> (<org> <code>, run <run>)', + ' (self-paced)' flag."""
        name = _clean_html(getattr(course, "display_name", "") or "", 200)
        heading = "Course: {} ({} {}, run {})".format(
            name or "(unnamed)", course_key.org, course_key.course, course_key.run
        )
        if getattr(course, "self_paced", False):
            heading += " (self-paced)"
        return heading

    def _course_dates(self, course):
        """
        'Dates: ...' line from the date fields that are actually set. The
        2030-01-01 sentinel (unset start, or an enrollment date falling back
        to it) is treated as unset and omitted - never printed as a fact.
        """
        date_parts = []
        for label, value in (
            ("Start", getattr(course, "start", None)),
            ("End", getattr(course, "end", None)),
            ("Enrollment start", getattr(course, "enrollment_start", None)),
            ("Enrollment end", getattr(course, "enrollment_end", None)),
        ):
            if _is_default_start(value):
                continue
            formatted = _fmt_date(value)
            if formatted:
                date_parts.append("{}: {}".format(label, formatted))
        return "Dates: " + "; ".join(date_parts) if date_parts else ""

    def _course_structure(self, course):
        """
        'Course structure:' block: chapter titles, each followed by its
        subsection titles, capped by COURSE_OVERVIEW_MAX_CHAPTERS and
        COURSE_OVERVIEW_MAX_SUBSECTIONS. Empty string if nothing found.
        """
        structure = []
        try:
            for chapter in course.get_children():
                chapter_name = _clean_name(chapter)
                if chapter_name:
                    subsections = []
                    try:
                        for sequential in chapter.get_children():
                            sub_name = _clean_name(sequential)
                            if sub_name:
                                subsections.append(sub_name)
                            if len(subsections) >= COURSE_OVERVIEW_MAX_SUBSECTIONS:
                                break
                    except Exception:
                        pass
                    line = "- " + chapter_name
                    if subsections:
                        line += ": " + "; ".join(subsections)
                    structure.append(line)
                if len(structure) >= COURSE_OVERVIEW_MAX_CHAPTERS:
                    break
        except Exception:
            pass
        return "Course structure:\n" + "\n".join(structure) if structure else ""

    def _collect_course_overview(self, course_key, max_chars=COURSE_OVERVIEW_MAX_CHARS):
        """
        Collect course-level facts for the system prompt: name, org/run,
        dates, description and instructors (from the course about pages) and
        the course structure (chapter titles, each followed by its subsection
        titles). Every piece is best-effort: missing data (no about page,
        workbench run, ...) is simply omitted. Returns a string, possibly empty.
        """
        try:
            store = self._modulestore()
            if store is None or course_key is None:
                return ""
            course = store.get_course(course_key)

            lines = [self._course_overview_heading(course, course_key)]
            for part in (
                self._course_dates(course),
                self._about_line(store, course_key, "short_description", "Description"),
                self._about_line(store, course_key, "instructor", "Instructors"),
                self._course_structure(course),
            ):
                if part:
                    lines.append(part)

            return "\n".join(lines)[:max_chars]
        except Exception:
            # Overview is optional enrichment - never block a chat request.
            return ""

    def _course_overview_cached(self):
        """
        Rendered course overview, cached per course in the Django cache.
        Empty renders are cached too (with a short TTL) so a course whose
        overview fails to build does not re-run the syllabus walk on every
        student question - but recovers within a minute. Falls back to a
        direct (uncached) render when the cache layer is unavailable
        (workbench, tests).
        """
        course_key = self._resolve_course_key()
        if course_key is None:
            return ""
        cache_key = "chatgpt-xblock:course-overview:{}".format(course_key)
        try:
            from django.core.cache import cache as django_cache

            overview = django_cache.get(cache_key, _CACHE_MISS)
            if overview is _CACHE_MISS:
                overview = self._collect_course_overview(course_key)
                ttl = COURSE_OVERVIEW_CACHE_TTL if overview else COURSE_OVERVIEW_EMPTY_CACHE_TTL
                django_cache.set(cache_key, overview, ttl)
            return overview or ""
        except Exception:
            return self._collect_course_overview(course_key)

    def student_view(self, context=None):
        """
        The primary view of the ChatgptXBlock, shown to students.
        """
        html = self.resource_string("static/html/chatgptxblock.html")

        # Insert disclaimer (1). Reflection block was removed from the
        # student UI; the submit_reflection handler is kept but unused.
        disclaimer_html = """
        <div class="chatgpt__disclaimer">
          <strong>Disclaimer:</strong> Responses are generated by an AI model.
          They might not always be accurate. Please verify with official course materials.
          To help it answer, general course information (name, dates, description,
          instructors, structure) and the content of the current course page
          (titles and text of the components you are viewing) are automatically
          added to the AI's instructions (system prompt).
        </div>
        """

        # Description heading only when the course team actually set one, so
        # blocks with the empty default show no placeholder text.
        description = (self.description or "").strip()
        description_html = (
            '<h3 class="chatgpt__description">{}</h3>'.format(description)
            if description
            else ""
        )

        # Format the final HTML content
        final_html = html.format(
            self=self, disclaimer=disclaimer_html, description_html=description_html
        )

        frag = Fragment(final_html)
        frag.add_css(self.resource_string("static/css/chatgptxblock.css"))
        # Vendor libs for markdown rendering of model answers (loaded before
        # the block JS, which uses global `marked` and `DOMPurify`).
        frag.add_javascript(self.resource_string("static/js/vendor/marked.min.js"))
        frag.add_javascript(self.resource_string("static/js/vendor/purify.min.js"))
        frag.add_javascript(self.resource_string("static/js/src/chatgptxblock.js"))
        frag.initialize_js("ChatgptXBlock")
        return frag

    def _resolve_base_url(self):
        """
        Resolve the AI endpoint base URL. Two layers, nothing more:
        1. Per-unit Studio setting (self.base_url) - course-team override.
        2. SiteConfiguration JSON, key CHATGPT_XBLOCK_BASE_URL - the per-tenant
           default (DB-backed, read at request time, so each Django Site can
           point at its own gateway).
        Returns a non-empty URL string or None. None means unconfigured: when
        the platform default is absent, the per-unit value is mandatory - the
        block must never silently fall back to the public OpenAI API.
        """
        url = (self.base_url or "").strip()
        if url:
            return url
        try:
            # LMS/CMS-only import: unavailable in the workbench, so fail soft.
            from openedx.core.djangoapps.site_configuration.helpers import get_value

            # String default selects get_value's raw-value fast path.
            url = (get_value("CHATGPT_XBLOCK_BASE_URL", "") or "").strip()
        except Exception:
            url = ""
        return url or None

    def get_openai_client(self):
        """
        Initialize and return an OpenAI client using the API key stored in the
        XBlock settings and the base URL resolved from Studio or
        SiteConfiguration.
        """
        api_key = self.api_key
        base_url = self._resolve_base_url()
        if not base_url:
            # Mandatory: with no SiteConfiguration default, the unit must set
            # its own endpoint - never fall back to the public OpenAI API.
            return {
                "error": (
                    "No AI endpoint configured. Set the API base URL in the "
                    "component settings (or ask the platform admin to set "
                    "CHATGPT_XBLOCK_BASE_URL in the site configuration)."
                )
            }
        try:
            # Hard 15s timeout: a hung endpoint must never block a request
            # thread indefinitely. No automatic retries - the student repeats
            # the question themselves via the UI.
            client = OpenAI(
                api_key=api_key, base_url=base_url, timeout=15.0, max_retries=0
            )
            return client
        except Exception:
            # Handle the exception as appropriate for your application
            return {"error": "Failed to initialize OpenAI client"}

    def _build_messages(self, question):
        """
        Assemble the chat messages: system prompt, course overview (dates,
        description, instructors, structure - if the toggle is on), the
        current page context, then the conversation so far with the pending
        question appended (not yet persisted in conversation_history).
        """
        messages = [{"role": "system", "content": self.context_text}]
        if self.include_course_overview:
            overview = self._course_overview_cached()
            if overview:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "General information about this course:\n" + overview
                        ),
                    }
                )
        course_context = self._collect_course_context()
        if course_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The student is currently viewing the following course page. "
                        "Ground your answer in this material when relevant:\n"
                        + course_context
                    ),
                }
            )
        messages.extend(self.conversation_history)
        messages.append({"role": "user", "content": question})
        return messages

    @XBlock.json_handler
    def get_answer(self, data, suffix=""):
        """
        Handle the submission of the user's question and return an answer,
        including guardrails such as moderation and error handling.
        Never raises: any unexpected failure is returned as a graceful
        {"error": ...} payload the UI can show, with the request repeatable.
        """
        try:
            return self._get_answer(data)
        except Exception as e:
            return {
                "error": (
                    "Something went wrong while contacting the AI service. "
                    "Please try again. ({}: {})".format(type(e).__name__, e)
                )
            }

    def _get_answer(self, data):
        question = data.get("question", "").strip()
        if not question:
            return {"answer": "Please enter a question."}

        # Endpoint must be resolved (unit setting or SiteConfiguration) before
        # anything is sent anywhere; get_openai_client refuses to build a
        # client against the public OpenAI default.
        client = self.get_openai_client()
        if not isinstance(client, OpenAI):
            return {
                "error": "Unable to initialize OpenAI client. Please check configuration."
            }

        # 3. Guardrails: Check user prompt with OpenAI Moderation. The chat.rcp
        # gateway rejects /moderations with 403 (passthrough disabled), so fail
        # open there rather than blocking all questions.
        try:
            mod_resp = client.moderations.create(input=question)
            if mod_resp.results[0].flagged:
                return {
                    "answer": "Your question may contain disallowed content. Please revise your question."
                }
        except AuthenticationError:
            return {
                "error": "Authentication error. Please check your API key in studio setting."
            }
        except Exception:
            pass

        # 2. Provide a conversation-like experience:
        # Limit the conversation to the last few messages (avoid excessive token usage)
        # Keep 6 messages max (3 user+3 assistant) plus a system prompt. Adjust as desired.
        if len(self.conversation_history) > 6:
            self.conversation_history.pop(0)

        # The pending question is passed to the model but NOT yet persisted in
        # conversation_history: if the request fails or times out, the history
        # stays clean so the student can simply repeat the question.
        messages = self._build_messages(question)

        if not (self.model_name or "").strip():
            return {
                "answer": "No model configured. Please set the model name in the component settings."
            }

        # Call ChatGPT with some max_tokens to control length (4).
        # All failures return a user-friendly "error" message without touching
        # conversation_history, so the student can repeat the question.
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,  # limit the size of the response
                temperature=self.temperature,  # slightly creative, but not too random
            )
        except APITimeoutError:
            return {
                "error": (
                    "The AI service did not respond within 15 seconds. "
                    "Please try again."
                )
            }
        except APIConnectionError:
            return {
                "error": (
                    "Could not reach the AI service. Please check your "
                    "connection and try again."
                )
            }
        except RateLimitError:
            return {
                "error": (
                    "The AI service is receiving too many requests right now. "
                    "Please wait a moment and try again."
                )
            }
        except AuthenticationError:
            return {
                "error": "Authentication error. Please check your API key in studio setting."
            }
        except Exception as e:
            # 8. Fallback or error handling
            return {"error": "AI service error: {}".format(str(e))}

        if not response.choices:
            return {"error": "No response received from the model. Please try again."}

        content = response.choices[0].message.content.strip() if (
            response.choices[0].message.content
        ) else ""
        if not content:
            return {
                "error": (
                    "Sorry, the model returned an empty response. Please try again."
                )
            }

        # Success only now: persist the question and answer in the conversation
        self.conversation_history.append({"role": "user", "content": question})
        self.conversation_history.append({"role": "assistant", "content": content})

        return {"answer": content}

    @XBlock.json_handler
    def get_conversation(self, data, suffix=""):
        """
        Return the stored conversation history for this student, so the UI
        can render the full session transcript on load.
        """
        return {"messages": self.conversation_history}

    @XBlock.json_handler
    def reset_conversation(self, data, suffix=""):
        """
        Start a new conversation: clear the stored transcript server-side, so
        the model no longer sees the previous exchanges. Explicit user action,
        non-recoverable.
        """
        self.conversation_history = []
        return {"status": "ok"}

    @XBlock.json_handler
    def submit_reflection(self, data, suffix=""):
        """
        Optional: Store learner reflections (6) for additional analytics or feedback.
        """
        reflection = data.get("reflection", "").strip()
        # Here you could store reflection in a different field, send to an analytics service, etc.
        if reflection:
            # TO-DO: Implement reflection storage or analytics
            # For demo, we simply return a success message.
            # In production, you might store it in XBlock fields or external DB.
            return {
                "status": "success",
                "message": "Reflection submitted successfully.",
            }
        else:
            return {"status": "error", "message": "Reflection text is empty."}

    @staticmethod
    def workbench_scenarios():
        """
        Scenarios for display in the XBlock workbench.
        """
        return [
            (
                "ChatgptXBlock",
                """<chatgptxblock/>
             """,
            ),
            (
                "Multiple ChatgptXBlock",
                """<vertical_demo>
                <chatgptxblock/>
                <chatgptxblock/>
                </vertical_demo>
             """,
            ),
        ]
