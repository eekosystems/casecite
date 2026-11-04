"""AI usage is metered per user, and users on the instance's keys are capped.

Every wrapper the app calls a provider through records the tokens the
provider reported, attributed to the request's user, marked when the
instance's own key carried the call. The quota applies only to users with
no key of their own and is checked before any call is made.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app import rls
from app.models.usage import UsageEvent
from app.services import usage
from app.services.llm_clients import openai_chat, openai_chat_sync

from tests.helpers import db_add, db_count, headers, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")


def _openai_response(prompt=10, completion=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


class TestTokenExtraction:
    def test_openai_anthropic_and_gemini_shapes(self):
        assert usage.tokens_from_response("openai", _openai_response(7, 3)) == (7, 3, 10)
        anthropic = SimpleNamespace(usage=SimpleNamespace(input_tokens=4, output_tokens=6))
        assert usage.tokens_from_response("anthropic", anthropic) == (4, 6, 10)
        gemini = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=2, candidates_token_count=3, total_token_count=5
            )
        )
        assert usage.tokens_from_response("google", gemini) == (2, 3, 5)
        rest = SimpleNamespace(
            payload={"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
        )
        assert usage.tokens_from_response("google", rest) == (1, 1, 2)

    def test_missing_usage_is_zero_not_an_error(self):
        assert usage.tokens_from_response("openai", SimpleNamespace()) == (0, 0, 0)
        assert usage.tokens_from_response("openai", {"usage": None}) == (0, 0, 0)


class TestRecording:
    async def test_openai_chat_records_against_the_request_user(self, client):
        fake = MagicMock()
        fake.api_key = "sk-user-own-key"
        fake.chat.completions.create = AsyncMock(return_value=_openai_response(20, 10))
        before = db_count(UsageEvent, UsageEvent.user_id == "meter-u1")
        with (
            patch("app.services.llm_clients.enforce_openai_client"),
            rls.scoped("meter-u1"),
        ):
            await openai_chat(fake, purpose="answer", model="gpt-x", messages=[])
        assert db_count(UsageEvent, UsageEvent.user_id == "meter-u1") == before + 1
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from tests.helpers import _sync_engine

        engine = _sync_engine()
        with Session(engine) as session:
            row = (
                session.execute(
                    select(UsageEvent)
                    .where(UsageEvent.user_id == "meter-u1")
                    .order_by(UsageEvent.id.desc())
                )
                .scalars()
                .first()
            )
        engine.dispose()
        assert (row.provider, row.model, row.purpose) == ("openai", "gpt-x", "answer")
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (20, 10, 30)
        assert row.server_key is False

    def test_the_sync_wrapper_marks_the_instance_key(self, client):
        fake = MagicMock()
        fake.api_key = "sk-instance"
        fake.chat.completions.create = MagicMock(return_value=_openai_response(1, 1))
        with (
            patch("app.services.llm_clients.enforce_openai_client"),
            patch("app.services.llm_clients.settings") as mock_settings,
            rls.scoped("meter-u2"),
        ):
            mock_settings.openai_api_key = "sk-instance"
            openai_chat_sync(fake, model="gpt-x", messages=[])
        assert (
            db_count(UsageEvent, UsageEvent.user_id == "meter-u2", UsageEvent.server_key.is_(True))
            == 1
        )

    async def test_a_metering_failure_never_fails_the_call(self):
        fake = MagicMock()
        fake.api_key = "k"
        fake.chat.completions.create = AsyncMock(return_value=_openai_response())
        with (
            patch("app.services.llm_clients.enforce_openai_client"),
            patch(
                "app.services.usage.AsyncSessionLocal",
                side_effect=RuntimeError("db down"),
                create=True,
            ),
            patch("app.database.AsyncSessionLocal", side_effect=RuntimeError("db down")),
        ):
            response = await openai_chat(fake, model="gpt-x", messages=[])
        assert response.choices[0].message.content == "hi"


class TestQuota:
    @pytest.fixture
    def quota(self):
        with patch.object(usage, "settings", SimpleNamespace(server_key_daily_token_quota=100)):
            yield

    def _seed(self, user_id: str, tokens: int, *, server_key: bool = True, days_ago: int = 0):
        db_add(
            UsageEvent(
                user_id=user_id,
                created_at=(datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None),
                provider="openai",
                model="gpt-x",
                purpose="answer",
                prompt_tokens=tokens,
                completion_tokens=0,
                total_tokens=tokens,
                server_key=server_key,
            )
        )

    def test_a_user_on_the_instance_key_is_refused_past_the_quota(self, client, quota):
        from app.exceptions import RateLimitedError
        from app.services.user_keys import UserAPIKeys

        self._seed("quota-u1", 60)
        self._seed("quota-u1", 50)
        self._seed("quota-u1", 500, days_ago=1)  # yesterday does not count
        self._seed("quota-u1", 500, server_key=False)  # their own key does not count
        request = MagicMock()
        request.headers = {}
        with (
            patch("app.services.key_storage.get_user_api_key", return_value=None),
            pytest.raises(RateLimitedError) as excinfo,
        ):
            UserAPIKeys.from_request(request, user_id="quota-u1")
        assert "110 of the 100" in str(excinfo.value.detail)
        assert "resets at midnight UTC" in str(excinfo.value.detail)

    def test_under_the_quota_and_byok_users_pass(self, client, quota):
        from app.services.user_keys import UserAPIKeys

        self._seed("quota-u2", 99)
        request = MagicMock()
        request.headers = {}
        with patch("app.services.key_storage.get_user_api_key", return_value=None):
            UserAPIKeys.from_request(request, user_id="quota-u2")  # 99 < 100
        self._seed("quota-u3", 1000)
        request.headers = {"X-OpenAI-Key": "sk-" + "a" * 48}
        UserAPIKeys.from_request(request, user_id="quota-u3")  # own key: never capped

    def test_no_quota_configured_means_no_check(self, client):
        from app.services.user_keys import UserAPIKeys

        self._seed("quota-u4", 10_000_000)
        request = MagicMock()
        request.headers = {}
        with patch("app.services.key_storage.get_user_api_key", return_value=None):
            UserAPIKeys.from_request(request, user_id="quota-u4")


class TestEndpoints:
    def test_my_usage_and_admin_usage(self, client, auth_headers):
        user = make_user("usage-view")
        db_add(
            UsageEvent(
                user_id=user.id,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                provider="openai",
                model="gpt-x",
                purpose="answer",
                prompt_tokens=30,
                completion_tokens=12,
                total_tokens=42,
                server_key=True,
            ),
            UsageEvent(
                user_id=user.id,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                provider="anthropic",
                model="claude",
                purpose="verify",
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                server_key=False,
            ),
        )
        with patch.object(usage, "settings", SimpleNamespace(server_key_daily_token_quota=100)):
            mine = client.get("/api/v1/usage/me?days=7", headers=headers(user))
        assert mine.status_code == 200, mine.text
        body = mine.json()
        assert body["total_tokens"] == 50 and body["server_key_tokens"] == 42
        assert body["calls"] == 2
        assert body["by_purpose"] == {"answer": 42, "verify": 8}
        assert body["server_key_tokens_today"] == 42
        assert body["quota_per_day"] == 100 and body["remaining_today"] == 58
        assert body["by_day"][-1]["tokens"] == 50

        admin = client.get("/api/v1/admin/usage?days=7", headers=auth_headers)
        assert admin.status_code == 200, admin.text
        per_user = {u["user_id"]: u for u in admin.json()["by_user"]}
        assert per_user[user.id]["tokens"] == 50 and per_user[user.id]["calls"] == 2

    def test_admin_usage_needs_the_admin_permission(self, client, non_admin_headers):
        assert client.get("/api/v1/admin/usage", headers=non_admin_headers).status_code == 403
