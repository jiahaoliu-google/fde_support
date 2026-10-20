"""The FAQ model must never see a concrete example order ID it could copy.

Regression: the prompt said "format like #12345"; asked "Where is my package?" the live
agent called get_order_status("12345") instead of asking for the order ID.
"""
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from agents.faq_assistant import agent as faq_agent  # noqa: E402
from agents.faq_assistant import tools_impl  # noqa: E402

# Order-ID-like tokens: "#123", "ORD-123", or a quoted bare number of 4+ digits.
_ORDER_ID_LIKE = re.compile(r"#\d{3,}|ORD-\d+|\"\d{4,}\"")


class TestNoExampleOrderIds(unittest.TestCase):
    def _assert_clean(self, text, where):
        self.assertEqual(_ORDER_ID_LIKE.findall(text or ""), [], f"concrete order ID in {where}")

    def test_instruction_has_no_example_order_id(self):
        self._assert_clean(faq_agent.faq_assistant_agent.instruction, "agent instruction")

    def test_order_tool_declaration_has_no_example_order_id(self):
        # The docstring becomes the function declaration sent to the model.
        self._assert_clean(tools_impl.get_order_status.__doc__, "get_order_status docstring")

    def test_invalid_id_message_has_no_example_order_id(self):
        result = tools_impl.get_order_status("not-an-order")
        self.assertEqual(result["status"], "invalid_order_id")
        self._assert_clean(result["message"], "invalid_order_id message")


class TestOrderIdGuard(unittest.TestCase):
    """The code-level guard: prompt edits alone did not stop the live model inventing #12345."""

    def _ctx(self, *user_messages, agent_text=None):
        from types import SimpleNamespace

        from google.adk.events import Event
        from google.adk.sessions import Session
        from google.genai import types

        def text_event(author, text):
            return Event(author=author, content=types.Content(role="user" if author == "user" else "model",
                                                              parts=[types.Part(text=text)]))

        events = [text_event("user", m) for m in user_messages[:-1]]
        if agent_text:
            events.append(text_event("faq_assistant_agent", agent_text))
        session = Session(id="s", app_name="a", user_id="u", events=events)
        current = types.Content(role="user", parts=[types.Part(text=user_messages[-1])])
        return SimpleNamespace(session=session, user_content=current)

    def _guard(self, order_id, ctx, tool_name="get_order_status"):
        from types import SimpleNamespace
        return tools_impl.order_id_guard(SimpleNamespace(name=tool_name), {"order_id": order_id}, ctx)

    def test_blocks_invented_id_when_user_gave_none(self):
        """Exact live failure: 'Where is my package?' -> get_order_status('#12345')."""
        ctx = self._ctx("Where is my package?")
        for invented in ("#12345", "12345", ""):
            result = self._guard(invented, ctx)
            self.assertEqual(result["status"], "order_id_not_provided", invented)
            self._assert_no_order_id(result["message"])

    def test_allows_id_typed_in_current_message(self):
        for typed, arg in (("It's 10301", "10301"), ("order #10301 please", "#10301"),
                           ("ORD-10301", "#10301")):
            self.assertIsNone(self._guard(arg, self._ctx(typed)), typed)

    def test_allows_id_typed_in_an_earlier_turn(self):
        ctx = self._ctx("My order is #10301", "Has it shipped yet?")
        self.assertIsNone(self._guard("#10301", ctx))

    def test_id_mentioned_only_by_the_agent_does_not_count(self):
        ctx = self._ctx("hi", "where is it?", agent_text="Your order #10201 is in transit.")
        self.assertEqual(self._guard("#10201", ctx)["status"], "order_id_not_provided")

    def test_partial_digit_match_does_not_count(self):
        # "103" is inside "10301" but is not what the user typed.
        self.assertEqual(self._guard("#103", self._ctx("It's 10301"))["status"], "order_id_not_provided")

    def test_other_tools_are_not_affected(self):
        self.assertIsNone(self._guard("", self._ctx("hi"), tool_name="search_support_records"))

    def test_guard_is_wired_on_the_agent(self):
        self.assertIs(faq_agent.faq_assistant_agent.before_tool_callback, tools_impl.order_id_guard)

    def _assert_no_order_id(self, text):
        self.assertEqual(_ORDER_ID_LIKE.findall(text), [])


if __name__ == "__main__":
    unittest.main()
