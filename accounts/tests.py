from urllib.parse import quote, urlencode

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.email_verification import ensure_unverified_state, mark_user_email_verified
from accounts.models import OAuthClient
from small_model.models import SmallModelConfig, UserSmallModelAccess, UserSmallModelQuota


class OAuthAuthorizeVerificationGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="new-user",
            email="new@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.oauth_client = OAuthClient.objects.create(
            client_id="stx_test_client",
            client_name="Test MCP Client",
            redirect_uris=["https://example.com/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="smarttex:read smarttex:write",
        )
        self.authorize_url = reverse("oauth-authorize")
        self.authorize_params = {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": self.oauth_client.redirect_uris[0],
            "scope": self.oauth_client.scope,
            "state": "test-state",
            "code_challenge": "test-challenge",
            "code_challenge_method": "plain",
        }

    def test_unverified_user_can_open_oauth_authorize_page(self):
        ensure_unverified_state(self.user)

        response = self.client.get(self.authorize_url, self.authorize_params)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/oauth_authorize.html")

    def test_unverified_user_can_approve_oauth_authorize(self):
        ensure_unverified_state(self.user)

        response = self.client.post(
            self.authorize_url,
            {
                **self.authorize_params,
                "action": "approve",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("code=", response["Location"])
        self.assertIn("state=test-state", response["Location"])

    def test_verified_user_can_still_open_oauth_authorize_page(self):
        mark_user_email_verified(self.user)

        response = self.client.get(self.authorize_url, self.authorize_params)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/oauth_authorize.html")


class OAuthLoginRedirectTests(TestCase):
    def setUp(self):
        self.password = "secret123"
        self.user = User.objects.create_user(
            username="oauth-user",
            email="oauth@example.com",
            password=self.password,
        )
        self.oauth_client = OAuthClient.objects.create(
            client_id="stx_login_test_client",
            client_name="Login Redirect Client",
            redirect_uris=["https://example.com/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="smarttex:read smarttex:write",
        )
        self.next_url = (
            f"{reverse('oauth-authorize')}?"
            f"{urlencode({
                'response_type': 'code',
                'client_id': self.oauth_client.client_id,
                'redirect_uri': self.oauth_client.redirect_uris[0],
                'scope': self.oauth_client.scope,
                'state': 'login-state',
                'code_challenge': 'login-challenge',
                'code_challenge_method': 'plain',
            })}"
        )

    def test_login_respects_next_for_oauth_authorize(self):
        mark_user_email_verified(self.user)

        response = self.client.post(
            f"{reverse('login')}?next={quote(self.next_url, safe='')}",
            {
                "username": self.user.email,
                "password": self.password,
                "next": self.next_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.next_url)

    def test_unverified_login_respects_next_for_oauth_authorize(self):
        ensure_unverified_state(self.user)

        response = self.client.post(
            f"{reverse('login')}?next={quote(self.next_url, safe='')}",
            {
                "username": self.user.email,
                "password": self.password,
                "next": self.next_url,
            },
        )


class ProfileViewTests(TestCase):
    def test_profile_shows_ai_limits_for_small_model_user(self):
        user = User.objects.create_user(username="profile-user", email="profile@example.com", password="secret123")
        self.client.force_login(user)
        model_cfg = SmallModelConfig.objects.create(provider="gemini", model_name="gemini-2.5-flash")
        access = UserSmallModelAccess.objects.create(user=user, enabled=True, model_config=model_cfg)
        from decimal import Decimal
        UserSmallModelQuota.objects.create(
            user=user,
            credits_limit=Decimal("1.000000"),
            credits_used=Decimal("0.250000"),
        )

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Credits")
        self.assertContains(response, "gemini-2.5-flash")
        self.assertContains(response, "0.7500")
