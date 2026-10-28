import io
import json
import os
import unittest
from unittest.mock import patch

from app.main import (
    TurnstileSetupError,
    TurnstileVerificationError,
    app,
    verify_turnstile,
)


class TurnstileVerificationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"CLOUDFLARE_TURNSTILE_SECRET_KEY": "test-secret"},
        )
        self.environment.start()
        self.app_context = app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": "192.0.2.10"}
        )
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()
        self.environment.stop()

    def test_accepts_successful_response_for_expected_action(self):
        response = io.BytesIO(
            json.dumps({"success": True, "action": "LOGIN"}).encode("utf-8")
        )
        with patch("app.main.urlopen", return_value=response) as urlopen:
            self.assertTrue(verify_turnstile("valid-token", "LOGIN"))

        request = urlopen.call_args.args[0]
        self.assertIn(b"secret=test-secret", request.data)
        self.assertIn(b"response=valid-token", request.data)
        self.assertIn(b"remoteip=192.0.2.10", request.data)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)

    def test_rejects_unsuccessful_or_wrong_action_response(self):
        responses = (
            {"success": False, "action": "LOGIN"},
            {"success": True, "action": "REGISTER"},
        )
        for result in responses:
            with self.subTest(result=result), patch(
                "app.main.urlopen",
                return_value=io.BytesIO(json.dumps(result).encode("utf-8")),
            ):
                self.assertFalse(verify_turnstile("token", "LOGIN"))

    def test_rejects_missing_token_without_calling_cloudflare(self):
        with patch("app.main.urlopen") as urlopen:
            self.assertFalse(verify_turnstile("", "LOGIN"))
        urlopen.assert_not_called()

    def test_requires_server_secret(self):
        with patch.dict(os.environ, {"CLOUDFLARE_TURNSTILE_SECRET_KEY": ""}):
            with self.assertRaises(TurnstileSetupError):
                verify_turnstile("token", "LOGIN")

    def test_surfaces_siteverify_network_failure(self):
        with patch("app.main.urlopen", side_effect=OSError("network unavailable")):
            with self.assertRaises(TurnstileVerificationError):
                verify_turnstile("token", "LOGIN")

    def test_auth_session_rejects_missing_challenge_before_firebase(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            with patch("app.main.firebase_admin_app") as firebase_app:
                response = client.post(
                    "/auth/session",
                    json={"idToken": "token", "mode": "login", "turnstileToken": ""},
                )

        self.assertEqual(response.status_code, 403)
        firebase_app.assert_not_called()

    def test_challenge_endpoint_verifies_and_binds_expected_action(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            with patch("app.main.verify_turnstile", return_value=True) as verify:
                response = client.post(
                    "/auth/turnstile",
                    json={"mode": "register", "turnstileToken": "token"},
                )
            with client.session_transaction() as user_session:
                self.assertEqual(
                    user_session["turnstile_verified_action"], "REGISTER"
                )
                self.assertIsInstance(user_session["turnstile_verified_at"], float)

        self.assertEqual(response.status_code, 200)
        verify.assert_called_once_with("token", "REGISTER")

    def test_auth_session_rejects_expired_or_wrong_action_proof(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            with client.session_transaction() as user_session:
                user_session["turnstile_verified_at"] = 1
                user_session["turnstile_verified_action"] = "REGISTER"
            with patch("app.main.firebase_admin_app") as firebase_app:
                response = client.post(
                    "/auth/session",
                    json={"idToken": "token", "mode": "login"},
                )

        self.assertEqual(response.status_code, 403)
        firebase_app.assert_not_called()

    def test_auth_pages_render_turnstile_and_keep_submit_disabled(self):
        app.config["TESTING"] = True
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_TURNSTILE_SITE_KEY": "public-site-key"},
        ):
            with app.test_client() as client:
                for path in ("/login", "/register"):
                    with self.subTest(path=path):
                        response = client.get(path)
                        page = response.get_data(as_text=True)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('id="turnstile-widget"', page)
                        self.assertIn('disabled>', page)
                        self.assertIn("turnstile/v0/api.js?render=explicit", page)


if __name__ == "__main__":
    unittest.main()
