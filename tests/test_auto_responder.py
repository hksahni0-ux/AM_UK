import unittest
from datetime import datetime, timedelta, timezone

from agents.auto_responder import decide


class AutoResponderRegressionTests(unittest.TestCase):
    def _message(self, body):
        return {
            "from": "Sam Kelly <sam@example.com>",
            "headers": {"from": "Sam Kelly <sam@example.com>", "subject": "Re: application"},
            "body": body,
            "date": datetime.now(timezone.utc) - timedelta(minutes=5),
            "self": False,
            "thread_id": "test-thread",
        }

    def test_correct_channels_instruction_uses_portal_reply(self):
        message = self._message(
            "Good morning Prateek,\n\n"
            "I am not the hiring manager for any current live roles and would ask that "
            "you apply for any roles through the correct channels. I wish you all the "
            "very best in your job search.\n\nKind regards,\nSam"
        )

        result = decide(
            [message],
            "owner@example.com",
            "Qdot Technology",
            datetime.now(timezone.utc),
            use_llm=lambda text, company: {
                "reply_type": "apply_via_portal",
                "reason": "They directed the applicant to the correct channels.",
            },
        )

        self.assertEqual(result.action, "reply")
        self.assertEqual(result.reply_type, "apply_via_portal")
        self.assertIn("Applying from overseas", result.plain)

    def test_footer_please_dont_print_does_not_block_forwarded_reply(self):
        message = self._message(
            "Hi Prateek,\n\n"
            "We currently don't have any roles available that would suit your experience, "
            "but I have sent it to HR to keep it on record if anything comes up.\n\n"
            "Many thanks,\nCarmel\n\n"
            "Please don't print this e-mail unless you really need to!"
        )

        result = decide(
            [message],
            "owner@example.com",
            "Shimadzu UK",
            datetime.now(timezone.utc),
            use_llm=lambda text, company: {
                "reply_type": "forwarded",
                "reason": "They forwarded the CV to HR.",
            },
        )

        self.assertEqual(result.action, "reply")
        self.assertEqual(result.reply_type, "forwarded")
        self.assertIn("Thank you for forwarding my CV", result.plain)

    def test_explicit_no_contact_request_still_requires_human(self):
        message = self._message("Please don't contact me again.")

        result = decide(
            [message],
            "owner@example.com",
            "Example Ltd",
            datetime.now(timezone.utc),
            use_llm=lambda text, company: self.fail("LLM must not receive an opt-out"),
        )

        self.assertEqual(result.action, "flag")
        self.assertEqual(result.reason, "asked not to be contacted")


if __name__ == "__main__":
    unittest.main()
