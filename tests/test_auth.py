"""Sign-in rules: every page needs the password when one is set, the
health check stays open, and on Railway the app refuses to run unprotected.
None of these requests reach the database (the sign-in check runs first)."""

import os
import unittest
from unittest import mock

import app as webapp


class AuthTests(unittest.TestCase):
    def setUp(self):
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        patches = [mock.patch.object(webapp, "APP_PASSWORD", "correct horse"),
                   mock.patch.object(webapp, "ON_RAILWAY", False),
                   mock.patch.object(webapp.time, "sleep")]            # skip the 1 s delay
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_pages_redirect_to_sign_in(self):
        r = self.client.get("/searches/11")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=/searches/11", r.headers["Location"])

    def test_posts_are_blocked_too(self):
        r = self.client.post("/searches", data={"buyer": "x", "target_sector": "y", "geography": "z"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_health_check_and_sign_in_page_are_open(self):
        self.assertEqual(self.client.get("/healthz").get_data(as_text=True), "ok")
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_wrong_password(self):
        r = self.client.post("/login", data={"password": "nope", "next": "/"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("That password isn", r.get_data(as_text=True))     # apostrophe is HTML-escaped
        self.assertEqual(self.client.get("/searches/11").status_code, 302)    # still locked

    def test_right_password_signs_in_and_returns_to_page(self):
        r = self.client.post("/login", data={"password": "correct horse", "next": "/searches/11"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/searches/11"))
        with self.client.session_transaction() as s:
            self.assertTrue(s["signed_in"])

    def test_next_cannot_leave_the_site(self):
        for evil in ("https://evil.example", "//evil.example"):
            r = self.client.post("/login", data={"password": "correct horse", "next": evil})
            self.assertEqual(r.headers["Location"], "/")

    def test_sign_out(self):
        self.client.post("/login", data={"password": "correct horse", "next": "/"})
        self.client.post("/logout")
        self.assertEqual(self.client.get("/searches/11").status_code, 302)

    def test_railway_refuses_without_password_or_secret(self):
        with mock.patch.object(webapp, "ON_RAILWAY", True), mock.patch.object(webapp, "APP_PASSWORD", ""):
            self.assertEqual(self.client.get("/").status_code, 503)
        with mock.patch.object(webapp, "ON_RAILWAY", True), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.get("/").status_code, 503)     # no FLASK_SECRET_KEY
            self.assertEqual(self.client.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
