import importlib.util
import base64
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "bin" / "ai-usage-fetch.py"
SPEC = importlib.util.spec_from_file_location("ai_usage_fetch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CodexUsageTest(unittest.TestCase):
    def test_weekly_primary_is_not_mislabeled_as_five_hour(self):
        result = MODULE.parse_codex_usage({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 6,
                    "limit_window_seconds": 604800,
                    "reset_at": 123,
                },
                "secondary_window": None,
            },
        })

        self.assertIsNone(result["five_hour"]["pct"])
        self.assertEqual(result["seven_day"], {"pct": 6.0, "resets_at": 123.0})

    def test_primary_and_secondary_are_classified_by_duration(self):
        result = MODULE.parse_codex_usage({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 12,
                    "limit_window_seconds": 18000,
                    "reset_at": 100,
                },
                "secondary_window": {
                    "used_percent": 34,
                    "limit_window_seconds": 604800,
                    "reset_at": 200,
                },
            },
        })

        self.assertEqual(result["five_hour"]["pct"], 12.0)
        self.assertEqual(result["seven_day"]["pct"], 34.0)

    def test_additional_model_limits_become_scoped_rows(self):
        result = MODULE.parse_codex_usage({
            "rate_limit": {},
            "additional_rate_limits": [{
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 18000,
                        "reset_at": 100,
                    },
                    "secondary_window": {
                        "used_percent": 20,
                        "limit_window_seconds": 604800,
                        "reset_at": 200,
                    },
                },
            }],
        })

        self.assertEqual(
            [item["label"] for item in result["scoped"]],
            ["GPT-5.3-Codex-Spark 5h", "GPT-5.3-Codex-Spark 7d"],
        )


class JwtPayloadTest(unittest.TestCase):
    def test_reads_user_id_for_kimi_account_deduplication(self):
        payload = base64.urlsafe_b64encode(json.dumps({
            "user_id": "same-account",
            "exp": 123,
        }).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"

        self.assertEqual(MODULE.jwt_payload(token)["user_id"], "same-account")
        self.assertEqual(MODULE.jwt_exp(token), 123.0)


if __name__ == "__main__":
    unittest.main()
