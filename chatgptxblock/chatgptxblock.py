import re

import pkg_resources
from openai import AuthenticationError, OpenAI
from xblock.core import XBlock
from xblock.fields import Float, Integer, List, Scope, String
from xblock.fragment import Fragment
from xblockutils.studio_editable import StudioEditableXBlockMixin


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
        default="swiss-ai/Apertus-v1.5-8B",
        scope=Scope.settings,
        help="Model ID as offered by the API endpoint (free text)",
    )
    api_key = String(
        default="your-openai-api-key-here",
        scope=Scope.settings,
        help="API key for the OpenAI-compatible endpoint",
    )
    description = String(
        default="Description here", scope=Scope.settings, help="Description"
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
        "description",
        "context_text",
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
        Returns a string or None if the runtime doesn't allow course access
        (e.g. workbench, library content, studio preview edge cases).
        """
        try:
            from xmodule.modulestore.django import modulestore

            store = modulestore()
            course_key = getattr(self.runtime, "course_id", None)
            if course_key is None:
                return None

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
                return None

            vertical = ancestors[0]
            subsection = ancestors[1].display_name if len(ancestors) > 1 else None
            section = ancestors[2].display_name if len(ancestors) > 2 else None
            course_name = ancestors[-1].display_name

            def clean(text, limit):
                text = re.sub(
                    r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I
                )
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:limit]

            # Text of the sibling components on the same page (the vertical)
            siblings = []
            remaining = max_chars
            for child_id in vertical.get_children():
                child = store.get_item(child_id)
                if getattr(child, "category", None) == self.category:
                    continue  # don't include this xblock itself
                name = clean(getattr(child, "display_name", "") or "", 120)
                body = ""
                if child.category in ("html", "problem", "video"):
                    body = clean(getattr(child, "data", "") or "", remaining)
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
        except Exception:
            return None

    def student_view(self, context=None):
        """
        The primary view of the ChatgptXBlock, shown to students.
        """
        html = self.resource_string("static/html/chatgptxblock.html")

        # Insert disclaimers or additional instructions (1) and reflection question (6)
        # We place them at the top or bottom of the HTML snippet via format placeholders.
        disclaimer_html = """
        <div class="chatgpt__disclaimer">
          <strong>Disclaimer:</strong> Responses are generated by an AI model.
          They might not always be accurate. Please verify with official course materials.
        </div>
        """
        reflection_html = """
        <div class="chatgpt__reflection">
          <label for="reflection-input">Reflection: How does this answer align with your understanding?</label>
          <textarea id="reflection-input" rows="2" placeholder="Write a short reflection..."></textarea>
          <button id="reflection-submit-btn">Submit Reflection</button>
        </div>
        """

        # Format the final HTML content
        final_html = html.format(
            self=self, disclaimer=disclaimer_html, reflection=reflection_html
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

    def get_openai_client(self):
        """
        Initialize and return an OpenAI client using the API key stored in the XBlock settings.
        """
        api_key = self.api_key
        try:
            client = OpenAI(api_key=api_key)
            return client
        except Exception:
            # Handle the exception as appropriate for your application
            return {"error": "Failed to initialize OpenAI client"}

    @XBlock.json_handler
    def get_answer(self, data, suffix=""):
        """
        Handle the submission of the user's question and return an answer,
        including guardrails such as moderation and error handling.
        """
        question = data.get("question", "").strip()
        if not question:
            return {"answer": "Please enter a question."}

        # Set the OpenAI API key (4 & 8: we handle errors in a try block)
        client = self.get_openai_client()
        if client is None:
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
                "answer": "Authentication error. Please check your API key in studio setting."
            }
        except Exception:
            pass

        # 2. Provide a conversation-like experience:
        # Add user's question to conversation history
        self.conversation_history.append({"role": "user", "content": question})

        # Limit the conversation to the last few messages (avoid excessive token usage)
        # Keep 6 messages max (3 user+3 assistant) plus a system prompt. Adjust as desired.
        if len(self.conversation_history) > 6:
            self.conversation_history.pop(0)

        # Prepare messages: system prompt, optional course context, then the
        # conversation so far.
        messages = [{"role": "system", "content": self.context_text}]
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

        # Call ChatGPT with some max_tokens to control length (4)
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=self.max_tokens,  # limit the size of the response
                temperature=self.temperature,  # slightly creative, but not too random
            )
        except AuthenticationError:
            return {
                "answer": "Authentication error. Please check your API key in studio setting."
            }
        except Exception as e:
            # 8. Fallback or error handling
            return {"answer": f"OpenAI API error: {str(e)}"}

        if not response.choices:
            return {"answer": "No response received from the model."}

        content = response.choices[0].message.content.strip()
        if not content:
            content = "Sorry, I couldn't generate a response. Please try again."

        # Add assistant's answer to the conversation
        self.conversation_history.append({"role": "assistant", "content": content})

        return {"answer": content}

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
