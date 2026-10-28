import io
import json
import os
import unittest
from unittest.mock import patch

from app.ai_review import (
    AIReviewError,
    _normalize_response,
    run_openrouter_review,
)


class NormalizeResponseTests(unittest.TestCase):
    def setUp(self):
        self.documents = [{"document": "proof.pdf"}]
        self.review = {
            "summary": "The details are consistent.",
            "findings": [],
        }

    def test_normalizes_json_response(self):
        result = _normalize_response(
            '{"summary":"The details are consistent.","findings":[]}',
            self.documents,
        )

        self.assertEqual(result, self.review)

    def test_normalizes_fenced_json_with_surrounding_text(self):
        content = (
            "Review result:\n```json\n"
            '{"summary":"The details are consistent.","findings":[]}'
            "\n```\n"
        )

        result = _normalize_response(content, self.documents)

        self.assertEqual(result, self.review)

    def test_normalizes_text_block_content(self):
        content = [
            {"type": "text", "text": '{"summary":"The details are '},
            {"type": "text", "text": 'consistent.","findings":[]} '},
        ]

        result = _normalize_response(content, self.documents)

        self.assertEqual(result, self.review)

    def test_normalizes_structured_json_content(self):
        result = _normalize_response(self.review, self.documents)

        self.assertEqual(result, self.review)

    def test_rejects_response_without_json(self):
        with self.assertRaisesRegex(AIReviewError, "unreadable review response"):
            _normalize_response("I could not complete this review.", self.documents)

    def test_reports_output_limit_instead_of_unreadable_json(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"summary":"partial'},
                }
            ]
        }
        response = io.BytesIO(json.dumps(response_data).encode("utf-8"))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("app.ai_review.urlopen", return_value=response):
                with self.assertRaisesRegex(AIReviewError, "output limit"):
                    run_openrouter_review(
                        {"pathway": "new"},
                        [{"original_name": "proof.pdf", "extracted_text": "Readable proof"}],
                    )

    def test_accepts_provider_parsed_response(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"parsed": self.review},
                }
            ]
        }
        response = io.BytesIO(json.dumps(response_data).encode("utf-8"))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("app.ai_review.urlopen", return_value=response):
                result = run_openrouter_review(
                    {"pathway": "new"},
                    [{"original_name": "proof.pdf", "extracted_text": "Readable proof"}],
                )

        self.assertEqual(result["summary"], self.review["summary"])
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
