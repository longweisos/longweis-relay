import json
import unittest

from longweis_relay_client.client_auth import RelayAuthClient, _NoAuthRedirects
from longweis_relay_client.client_errors import ProviderResponseError


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class RelayAuthClientTests(unittest.TestCase):
    def test_auth_transport_refuses_redirects(self):
        self.assertIsNone(
            _NoAuthRedirects().redirect_request(
                object(), object(), 307, "Redirect", {}, "https://attacker.example"
            )
        )

    def test_email_registration_contract_never_sends_provider_key(self):
        transport = RecordingTransport(
            [
                (
                    200,
                    json.dumps(
                        {
                            "object": "longweis_relay.email_challenge",
                            "challenge_id": "email_one",
                            "masked_email": "t***@example.com",
                            "expires_at": "2026-09-06T12:10:00+00:00",
                        }
                    ).encode(),
                ),
                (200, b'{"object":"longweis_relay.account_activation","relay_key":"psk_test_prefix.secret","account":{"masked_email":"t***@example.com"},"balance":{"available_usd":"10.000000000"}}'),
            ]
        )
        client = RelayAuthClient("https://api.longweisos.com/v1", transport)
        challenge = client.begin_email("tester@example.com")
        result = client.complete_email(challenge["challenge_id"], "123456")
        self.assertEqual(result["relay_key"], "psk_test_prefix.secret")
        first_body = json.loads(transport.requests[0][0].data)
        second_body = json.loads(transport.requests[1][0].data)
        self.assertEqual(first_body, {"email": "tester@example.com"})
        self.assertEqual(second_body["verification_code"], "123456")
        serialized = b"".join(request[0].data for request in transport.requests).decode()
        self.assertNotIn("OpenRouter", serialized)
        self.assertNotIn("premium", serialized)

    def test_invalid_server_response_fails_closed(self):
        client = RelayAuthClient(
            "https://api.longweisos.com/v1",
            RecordingTransport([(200, b'{"object":"wrong","relay_key":"psk_test_bad"}')]),
        )
        with self.assertRaises(ProviderResponseError):
            client.complete_email("email_one", "123456")

    def test_browser_oauth_flow_polls_until_account_key_is_ready(self):
        transport = RecordingTransport(
            [
                (
                    200,
                    json.dumps(
                        {
                            "object": "longweis_relay.oauth_flow",
                            "provider": "google",
                            "flow_id": "oauth_one",
                            "poll_token": "poll-secret",
                            "authorization_url": "https://accounts.google.com/example",
                            "expires_at": "2026-09-06T12:10:00+00:00",
                        }
                    ).encode(),
                ),
                (200, b'{"object":"longweis_relay.oauth_poll","status":"pending"}'),
                (200, b'{"object":"longweis_relay.account_activation","relay_key":"psk_test_prefix.secret","account":{"masked_email":"t***@example.com"},"balance":{"available_usd":"10.000000000"}}'),
            ]
        )
        client = RelayAuthClient("https://api.longweisos.com/v1", transport)
        flow = client.begin_oauth("google")
        self.assertEqual(
            client.poll_oauth(flow["flow_id"], flow["poll_token"])["status"],
            "pending",
        )
        result = client.poll_oauth(flow["flow_id"], flow["poll_token"])
        self.assertEqual(result["relay_key"], "psk_test_prefix.secret")

    def test_restricted_email_registration_response_fails_closed(self):
        client = RelayAuthClient(
            "https://api.longweisos.com/v1",
            RecordingTransport(
                [(200, b'{"object":"longweis_relay.email_activation_required"}')]
            ),
        )
        with self.assertRaisesRegex(ProviderResponseError, "restricted"):
            client.complete_email("email_one", "123456")

    def test_restricted_oauth_registration_response_fails_closed(self):
        client = RelayAuthClient(
            "https://api.longweisos.com/v1",
            RecordingTransport(
                [
                    (
                        200,
                        b'{"object":"longweis_relay.oauth_poll","status":"activation_required"}',
                    )
                ]
            ),
        )
        with self.assertRaisesRegex(ProviderResponseError, "restricted"):
            client.poll_oauth("oauth_one", "poll-secret")


if __name__ == "__main__":
    unittest.main()
