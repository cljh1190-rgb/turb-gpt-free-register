# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from core.openai_auth import AuthStateInvalidError, network_preflight, validate_email_otp
from core.session import BrowserSession


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""

    def json(self):
        import json
        return json.loads(self.text)

    def raise_for_status(self):
        raise RuntimeError(self.text)


class _Session:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = 0

    def get_chatgpt_navigate_headers(self, **_kwargs):
        return {}

    def get_auth_navigate_headers(self, **_kwargs):
        return {}

    def get_auth_headers(self, **_kwargs):
        return {}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        value = next(self._responses)
        if isinstance(value, BaseException):
            raise value
        return value


class NetworkPreflightTests(unittest.TestCase):
    def test_http_403_is_connectivity_success(self):
        session = _Session([_Response(403), _Response(403), _Response(403)])
        network_preflight(session)
        self.assertEqual(session.calls, 3)

    def test_http_403_does_not_open_session_circuit(self):
        session = BrowserSession.__new__(BrowserSession)
        session.blocked_until = 0.0
        session.blocked_reason = ""
        session._cf_cookie_seen = {}
        session.session = SimpleNamespace(cookies=SimpleNamespace(jar=[]))
        response = SimpleNamespace(status_code=403, headers={})
        self.assertIs(session._observe_response_for_circuit_breaker(response, "https://chatgpt.com/login"), response)
        self.assertEqual(session.blocked_until, 0.0)

    def test_transport_timeout_is_still_retried_and_raised(self):
        session = _Session([TimeoutError("timed out")] * 3)
        with patch("core.openai_auth.time.sleep"):
            with self.assertRaises(TimeoutError):
                network_preflight(session)
        self.assertEqual(session.calls, 3)

    def test_invalid_state_is_not_retried_as_bad_otp(self):
        session = _Session([])
        session.post = lambda *_args, **_kwargs: _ResponseWithBody(
            401,
            '{"error":{"code":"invalid_state","message":"Your sign-in session is no longer valid."}}',
        )
        with self.assertRaises(AuthStateInvalidError):
            validate_email_otp(session, "123456")


class _ResponseWithBody(_Response):
    def __init__(self, status_code: int, text: str):
        super().__init__(status_code)
        self.text = text


if __name__ == "__main__":
    unittest.main()
