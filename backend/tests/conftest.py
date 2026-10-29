"""
Test configuration and fixtures for CaseCite.

Everything the suite needs from the environment is set HERE, before the app is
imported, so `python -m pytest` behaves the same on a laptop and in CI:

- All on-disk state (uploads, vector store, SQLite DB, audit logs, sessions,
  revoked tokens, key stores — every one derives from UPLOAD_DIR's parent) goes
  to a throwaway per-session directory. The suite never touches backend/data.
- Provider keys are dummies, so a developer's real key in .env is never used
  (and never billed) by a test run.
- Outbound network is blocked: resolving any public hostname fails like an
  offline machine. Set CASECITE_TEST_ALLOW_NETWORK=1 to lift the block.
"""

import atexit
import datetime as _dt
import ipaddress
import os
import shutil
import socket
import tempfile
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---- Environment (must run before anything imports app.config) --------------

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"
# CSRF middleware is now ALWAYS registered; skipping validation requires the
# explicit debug+csrf_disabled pair (see app/middleware/csrf.py).
os.environ["CSRF_DISABLED"] = "true"

# Isolated data directory. setdefault: a caller that points these somewhere
# explicitly (CI's Postgres job, a debugging session) is respected; the
# defaults in app/config.py (./data/...) are never used by a test run.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="casecite-tests-")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)
os.environ.setdefault("UPLOAD_DIR", os.path.join(_TEST_DATA_DIR, "uploads"))
os.environ.setdefault("CHROMA_PERSIST_DIR", os.path.join(_TEST_DATA_DIR, "chroma"))
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+aiosqlite:///" + os.path.join(_TEST_DATA_DIR, "casecite.db").replace(os.sep, "/"),
)

# Dummy provider keys. Environment variables take precedence over .env, so a
# real key in the developer's .env cannot leak into a test run.
os.environ["OPENAI_API_KEY"] = "sk-test-dummy-key-not-real"
for _key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"):
    os.environ[_key] = ""

# ---- Network guard -----------------------------------------------------------

_real_getaddrinfo = socket.getaddrinfo


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        return True  # IP literals (loopback, CI service containers) need no DNS
    except ValueError:
        pass
    # localhost, TestClient's fake hosts, and dotless names (compose services).
    return host == "" or "." not in host or host.endswith(".localhost")


def _guarded_getaddrinfo(host, *args, **kwargs):
    if _is_local_host(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    # Same failure a machine with no network would produce, so code under test
    # exercises its real "provider unreachable" path instead of calling out.
    raise socket.gaierror(socket.EAI_NONAME, f"network access is blocked in tests: {host}")


if os.environ.get("CASECITE_TEST_ALLOW_NETWORK") != "1":
    socket.getaddrinfo = _guarded_getaddrinfo


@pytest.fixture(scope="session", autouse=True)
def _seed_without_embeddings():
    """App startup seeds the clause taxonomy and, when OPENAI_API_KEY is set,
    embeds it — a provider call on every TestClient startup. Seed the rows but
    skip the embedding step for the whole session."""
    from app.services.clause_intel import seeder

    real_seed = seeder.seed_clause_intelligence

    async def _seed(session, embed_fn=None, embedding_model=None):
        return await real_seed(session, embed_fn=None, embedding_model=embedding_model)

    with patch.object(seeder, "seed_clause_intelligence", _seed):
        yield


@pytest.fixture(scope="session")
def app():
    """Create FastAPI app instance for testing."""
    from app.main import app

    return app


@pytest.fixture
def client(app) -> Generator:
    """Create test client."""
    with TestClient(app) as client:
        yield client


def make_auth_headers(user) -> dict:
    """Mint an access token AND register its session.

    get_current_user enforces session validation (session_manager.validate_session
    by JTI), so a bare token 401s — every test token must have a matching session.
    TestClient requests arrive with client host "testclient".
    """
    from app.middleware.security import session_manager
    from app.services.auth import auth_service

    token = auth_service.create_access_token(user)
    token_data = auth_service.verify_token(token)
    session_manager.create_session(
        user_id=user.id,
        ip_address="testclient",
        user_agent="pytest",
        jti=token_data.jti,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_headers() -> dict:
    """Create authentication headers with a valid test token."""
    from datetime import datetime

    from app.services.auth import User, UserRole

    # Create a test user
    test_user = User(
        id="test-user-123",
        email="test@casecite.legal",
        name="Test User",
        roles=[UserRole.ATTORNEY, UserRole.ADMIN],
        last_login=datetime.now(_dt.UTC),
    )

    return make_auth_headers(test_user)


@pytest.fixture
def non_admin_headers() -> dict:
    """Create authentication headers for a non-admin user."""
    from datetime import datetime

    from app.services.auth import User, UserRole

    test_user = User(
        id="test-viewer-456",
        email="viewer@casecite.legal",
        name="Test Viewer",
        roles=[UserRole.VIEWER],
        last_login=datetime.now(_dt.UTC),
    )

    return make_auth_headers(test_user)


@pytest.fixture
def mock_openai():
    """Mock OpenAI client for testing."""
    with patch("openai.OpenAI") as mock:
        mock_client = MagicMock()
        mock.return_value = mock_client

        # Mock chat completion
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Test response from AI"
        mock_client.chat.completions.create.return_value = mock_response

        # Mock embeddings
        mock_embedding_response = MagicMock()
        mock_embedding_response.data = [MagicMock()]
        mock_embedding_response.data[0].embedding = [0.1] * 1536  # OpenAI embedding dimension
        mock_client.embeddings.create.return_value = mock_embedding_response

        yield mock_client


@pytest.fixture
def audit_service_isolated(tmp_path):
    """Create an isolated AuditService with a temp log directory."""
    from app.services.audit import AuditLog, AuditService

    # Reset the chain hash for isolation
    original_hash = AuditLog._last_hash
    AuditLog._last_hash = None

    service = AuditService.__new__(AuditService)
    service.log_dir = tmp_path / "audit_logs"
    service.log_dir.mkdir(parents=True, exist_ok=True)
    service._current_log_file = None
    service._current_date = None

    yield service

    # Restore original hash
    AuditLog._last_hash = original_hash


@pytest.fixture
def chroma_db(tmp_path):
    """Create an isolated ChromaDB instance with a temp directory."""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    chroma_path = str(tmp_path / "chroma")
    os.makedirs(chroma_path, exist_ok=True)

    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection(
        name="test_legal_documents",
        metadata={"hnsw:space": "cosine"},
    )

    from app.services.lexical_index import LexicalIndex
    from app.services.vectordb import ChromaDB

    # The real ChromaDB class over the test collection, with its own keyword
    # index: skip __init__ (it opens settings.chroma_persist_dir) and attach
    # the isolated client, collection and index instead.
    class TestChromaDB(ChromaDB):
        def __init__(self):
            pass

    db = TestChromaDB()
    db.client = client
    db.collection = collection
    db.lexical = LexicalIndex(str(tmp_path / "lexical.sqlite3"))
    yield db
    db.lexical.close()


@pytest.fixture
def no_rate_limit():
    """Bypass the per-IP rate limiter.

    Every TestClient request comes from the same "testclient" IP, so under the
    full suite a bucket can already be spent and a request 429s before reaching
    the logic under test. Opt in with ``pytest.mark.usefixtures``.
    """
    with patch(
        "app.middleware.security.RateLimiter.is_allowed",
        return_value=(True, {"limit": 100, "remaining": 99}),
    ):
        yield


@pytest.fixture
def audit_events():
    """Capture audit events emitted through audit_service.log_event.

    Yields a list of the keyword arguments of every call, so a test can assert
    that an action was audited without touching the audit log files.
    """
    from app.services.audit import audit_service

    events: list[dict] = []

    async def _capture(**kwargs):
        events.append(kwargs)

    with patch.object(audit_service, "log_event", _capture):
        yield events
