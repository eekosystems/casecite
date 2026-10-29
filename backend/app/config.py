import logging
import os
import secrets
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Prefixes that indicate a placeholder value, not a real secret  # nosec B105
_PLACEHOLDER_PREFIXES = ("change-me", "REPLACE_WITH", "REPLACE-ME", "your-", "sk-your")

# Shapes of the example API keys shipped in .env.example and setup templates.
_PLACEHOLDER_API_KEY_PREFIXES = (
    "your-",
    "your_",
    "sk-your",
    "sk-ant-your",
    "pa-your",
    "change-me",
    "changeme",
    "replace",
    "<",
)


def is_placeholder_api_key(value: str | None) -> bool:
    """True for an example/placeholder API key that was never filled in."""
    val = (value or "").strip().lower()
    return bool(val) and val.startswith(_PLACEHOLDER_API_KEY_PREFIXES)


# Settings removed with the public-demo posture; still present in older .env files.
_REMOVED_SETTINGS = frozenset(
    {
        "demo_mode",
        "demo_daily_query_limit",
        "demo_max_documents",
        "demo_account_email",
        "sales_email",
    }
)


class Settings(BaseSettings):
    # App
    app_name: str = "CaseCite"
    app_display_name: str = "CaseCite"
    debug: bool = False
    environment: str = "development"
    api_prefix: str = "/api/v1"

    # Sender address for outbound email (override for white-label deployments)
    noreply_email: str = "noreply@casecite.com"

    # Explicit flag to disable CSRF (must be True WITH debug=True to skip CSRF)
    csrf_disabled: bool = False

    # Security - MUST be set in production via environment variable
    secret_key: str = ""  # Will be validated on startup
    access_token_expire_minutes: int = 60  # 1 hour (reduced from 24 for security)

    # Self-service registration. Default OFF in production: an internet-reachable
    # instance with open registration lets anyone self-provision a full-access
    # account. Enable deliberately (ALLOW_REGISTRATION=true) for trusted networks.
    # In debug (local development), registration is always open for convenience.
    allow_registration: bool = False
    # Token required to create the FIRST (admin) account on a fresh install, so
    # an attacker who reaches the instance before you cannot seize the
    # first-user-is-admin slot. In production (DEBUG=false) the first registration
    # is REFUSED until REGISTRATION_BOOTSTRAP_TOKEN is set and supplied; in local
    # development it is optional.
    registration_bootstrap_token: str = ""

    # Public demo instance. Off unless BOTH are set. When they are, the account
    # is created (or reset) at every startup with the attorney role and this
    # password; it cannot change its password, be reset by email, enrol MFA or
    # be promoted; it is exempt from the login lockout so one visitor cannot
    # lock the others out; it never counts as the "first user" for the admin
    # bootstrap; and GET /branding publishes the credentials so the sign-in form
    # prefills them. Everything the account does is visible to every visitor —
    # never set this on an instance that holds real client data.
    demo_login_email: str = ""
    demo_login_password: str = ""

    # Require every password account to enrol TOTP MFA before using the API
    # (SSO-only accounts rely on the identity provider's MFA policy).
    require_mfa: bool = False

    # Frontend URL for password reset emails (e.g., https://app.casecite.com)
    frontend_url: str = ""

    # Device-code sign-in (RFC 8628 style) for clients that cannot hold a cookie
    # session, such as the Word add-in's task pane: how long a pending code
    # lives, and the minimum seconds between polls of /auth/device/token.
    device_code_ttl: int = 600
    device_code_interval: int = 5
    # Origin the Word add-in's task pane is served from (e.g.
    # https://addin.yourfirm.com). When set it is appended to the CORS allow
    # list; when empty nothing changes.
    word_addin_origin: str = ""

    # Encryption salt - generated per deployment, stored in .env
    encryption_salt: str = ""  # Will be auto-generated if not set
    # Previous encryption salts for key rotation (comma-separated)
    # Move old ENCRYPTION_SALT here when rotating to a new salt
    encryption_salt_previous: str = ""

    # Email service (SendGrid)
    sendgrid_api_key: str | None = None

    # Redis for session/token management (required for production)
    redis_url: str | None = None  # e.g., redis://localhost:6379/0
    redis_socket_timeout: float = 5.0

    # Session IP binding (rejects requests from a different IP than login).
    # Default OFF: behind a reverse proxy (Coolify/Traefik/nginx) or on mobile
    # networks the client IP varies between requests, which would log users out
    # on refresh. Opt in with ENFORCE_SESSION_IP_BINDING=true only when the
    # client IP is guaranteed stable end-to-end.
    enforce_session_ip_binding: bool = False

    # Absolute session lifetime: a sign-in ends this long after it started no
    # matter how often the token is refreshed (the user signs in again).
    session_absolute_timeout_hours: int = 8
    # Idle timeout: a session with no authenticated request for this long is
    # ended. 0 disables the idle check (the absolute lifetime still applies).
    session_idle_timeout_minutes: int = 120

    # Database (PostgreSQL for production, SQLite for development)
    # Set DATABASE_URL for production: postgresql+asyncpg://user:pass@host:5432/casecite
    database_url: str | None = None  # Defaults to SQLite if not set

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_settings(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Ignore settings removed with the public-demo posture (runs before freeze).

        Earlier releases shipped these keys in .env.example, so existing .env
        files still carry them. pydantic-settings rejects unknown dotenv keys,
        which would turn a routine upgrade into a startup crash; drop exactly
        these keys with a warning instead. The only development flag is DEBUG.
        """
        for key in [k for k in data if str(k).lower() in _REMOVED_SETTINGS]:
            data.pop(key)
            name = str(key).upper()
            hint = " (for local development set DEBUG=true)" if name == "DEMO_MODE" else ""
            logger.warning(
                "%s was removed in this release and is ignored — delete it from .env%s",
                name,
                hint,
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _generate_dev_secrets(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Auto-generate secrets for local development (DEBUG=true; runs before freeze)."""
        debug = data.get("debug", False)
        # Handle raw string values from environment variables
        if isinstance(debug, str):
            debug = debug.lower() in ("true", "1", "yes")
        if not debug:
            return data

        # Persist auto-generated dev secrets to the data dir so they survive
        # restarts. Without this, every container restart regenerates the keys,
        # invalidating all issued tokens/sessions (users get logged out and have
        # to sign in again). Explicit env vars always take precedence.
        import json
        from pathlib import Path

        data_dir = Path(data.get("upload_dir") or "./data/uploads").parent
        secrets_file = data_dir / ".instance_secrets.json"
        persisted: dict[str, str] = {}
        try:
            if secrets_file.exists():
                loaded = json.loads(secrets_file.read_text())
                if isinstance(loaded, dict):
                    persisted = {k: str(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            persisted = {}

        changed = False

        def _resolve(field: str, nbytes: int) -> None:
            nonlocal changed
            val = data.get(field, "")
            if val and not any(val.startswith(p) for p in _PLACEHOLDER_PREFIXES):  # nosec B105
                return  # explicitly set via env — leave it untouched
            if persisted.get(field):
                data[field] = persisted[field]
                return
            generated = secrets.token_hex(nbytes)
            data[field] = generated
            persisted[field] = generated
            changed = True

        _resolve("secret_key", 32)
        _resolve("encryption_salt", 16)
        _resolve("audit_hmac_key", 32)

        if changed:
            logger.warning(
                "SECRET_KEY/ENCRYPTION_SALT/AUDIT_HMAC_KEY not set — auto-generated "
                "for development (DEBUG=true). Set them as env vars for production."
            )
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                secrets_file.write_text(json.dumps(persisted))
                try:
                    os.chmod(secrets_file, 0o600)
                except OSError:
                    pass
            except OSError as e:
                logger.warning("Could not persist instance secrets to %s: %s", secrets_file, e)
        return data

    @field_validator(
        "openai_api_key",
        "anthropic_api_key",
        "voyage_api_key",
        "cohere_api_key",
        "pinecone_api_key",
        mode="before",
    )
    @classmethod
    def _ignore_garbage_keys(cls, v, info):
        """Treat a mangled or placeholder env key as NO key (runs before the frozen model is built).

        A lone "#" (a commented .env line pasted into an env panel), similar
        debris, or an untouched .env.example placeholder ("sk-your-…",
        "your-voyage-api-key") must not masquerade as a configured provider —
        it makes every fallback call fail with a baffling provider 401 instead
        of a clear "no key configured", and breaks BYOK-only instances whose
        real keys live in the app's Settings. A real key never starts with "#",
        is never this short, and never reads "your-…".
        """
        if v is None:
            return v
        val = str(v).strip()
        if val and (val.startswith("#") or len(val) <= 2 or is_placeholder_api_key(val)):
            logger.warning(
                f"{info.field_name.upper()} is set to an obviously invalid value "
                f"({val[:4]!r}…) — ignoring it. Remove the stray value from the "
                "environment, or set a real key."
            )
            return None
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_demo_login(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Lower-case and trim the demo email so it matches the stored account."""
        for key in [k for k in data if str(k).lower() == "demo_login_email"]:
            data[key] = str(data[key] or "").strip().lower()
        return data

    @property
    def demo_login_enabled(self) -> bool:
        """True when a public demo account is configured (both settings set)."""
        return bool(self.demo_login_email and self.demo_login_password)

    @model_validator(mode="after")
    def _validate_demo_login(self) -> "Settings":
        """A half-configured demo account is a misconfiguration, not a quiet default."""
        if bool(self.demo_login_email) != bool(self.demo_login_password):
            raise ValueError(
                "DEMO_LOGIN_EMAIL and DEMO_LOGIN_PASSWORD must be set together "
                "(or both left unset)."
            )
        if self.demo_login_enabled and self.require_mfa:
            raise ValueError(
                "REQUIRE_MFA cannot be combined with DEMO_LOGIN_*: the demo account "
                "can never enrol MFA, so nobody could sign in to it."
            )
        return self

    @model_validator(mode="after")
    def _validate_security_settings(self) -> "Settings":
        """Validate security settings (read-only, model is frozen after this)."""
        # Validate SECRET_KEY (nosec B105 - checking for placeholder, not storing secret)
        if not self.secret_key or any(self.secret_key.startswith(p) for p in _PLACEHOLDER_PREFIXES):  # nosec B105
            raise ValueError(
                "SECURITY ERROR: SECRET_KEY must be set for production. "
                "Generate one with: openssl rand -hex 32"
            )
        elif len(self.secret_key) < 32:
            raise ValueError(
                "SECURITY ERROR: SECRET_KEY must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )

        # Validate encryption salt
        if not self.encryption_salt or any(
            self.encryption_salt.startswith(p) for p in _PLACEHOLDER_PREFIXES
        ):
            raise ValueError(
                "SECURITY ERROR: ENCRYPTION_SALT must be set for production. "
                "Generate one with: openssl rand -hex 16"
            )

        # Validate audit HMAC key
        if not self.audit_hmac_key or any(
            self.audit_hmac_key.startswith(p) for p in _PLACEHOLDER_PREFIXES
        ):
            raise ValueError(
                "SECURITY ERROR: AUDIT_HMAC_KEY must be set for production. "
                "Generate one with: openssl rand -hex 32"
            )
        elif len(self.audit_hmac_key) < 32:
            # The audit chain uses the first 32 bytes of this value as its HMAC
            # key; a short value silently weakens the tamper-evidence the log
            # is relied on for.
            raise ValueError(
                "SECURITY ERROR: AUDIT_HMAC_KEY must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )

        if self.sendgrid_api_key and self.noreply_email == "noreply@casecite.com":
            logger.warning(
                "SENDGRID_API_KEY is set but NOREPLY_EMAIL still has the default "
                "sender (noreply@casecite.com), a domain you do not control. "
                "Password-reset and invitation mail will fail SPF/DMARC checks "
                "until NOREPLY_EMAIL is an address on a domain verified in SendGrid."
            )

        # Enforce Redis for production
        if not self.redis_url and not self.debug:
            raise ValueError(
                "CONFIGURATION ERROR: REDIS_URL must be set for production. "
                "Redis is required for distributed rate limiting and session management. "
                "Set REDIS_URL=redis://host:6379/0 for production deployments."
            )

        # ENVIRONMENT is declarative; DEBUG is what actually relaxes security
        # (dev login, open registration, auto-generated secrets, verbose errors).
        # Refuse the contradictory pair rather than run "production" wide open.
        if self.debug and self.environment.strip().lower() in ("production", "prod"):
            raise ValueError(
                "CONFIGURATION ERROR: ENVIRONMENT is production but DEBUG=true. DEBUG "
                "enables the dev login, open registration and verbose errors and must "
                "be false in production. Set DEBUG=false (or ENVIRONMENT=development)."
            )

        # Warn when DEBUG disables CSRF protection
        if self.debug:
            logger.warning(
                "DEBUG=true disables CSRF protection. "
                "Ensure DEBUG is false in production deployments."
            )

        # Enforce DATABASE_URL for production
        if not self.database_url and not self.debug:
            raise ValueError(
                "CONFIGURATION ERROR: DATABASE_URL must be set for production. "
                "SQLite is not suitable for production workloads. "
                "Set DATABASE_URL=postgresql+asyncpg://user:pass@host/db for production."
            )

        # Data-at-rest: derived privileged content (vector chunks, extracted
        # clause text, chat history) is stored unencrypted. Require the operator
        # to confirm the data volume is encrypted before serving production
        # traffic. Fail closed — a silent warning is not a control.
        if not self.debug and not self.disk_encryption_acknowledged:
            raise ValueError(
                "SECURITY ERROR: derived privileged content (vector chunks, extracted "
                "clause text, chat history) is stored UNENCRYPTED at rest. Enable "
                "full-disk/volume encryption on the data volume (LUKS, BitLocker, or "
                "FileVault), then set DISK_ENCRYPTION_ACKNOWLEDGED=true to confirm. "
                "This gate exists because a stolen disk or laptop would otherwise "
                "expose the plaintext document corpus."
            )

        # A server-level AI key is OPTIONAL: this is a BYOK product — users
        # supply their own keys in Settings, stored encrypted per user. The
        # server env key is only a fallback for instances that want one.
        if not (self.openai_api_key or self.anthropic_api_key):
            logger.info(
                "No server-level AI provider key configured — running BYOK-only. "
                "Users must add their own API keys in Settings; background jobs "
                "use each user's stored keys."
            )

        # Production mode validation summary
        if not self.debug:
            missing_recommended = []
            if not self.redis_url:
                missing_recommended.append("REDIS_URL")
            if not self.database_url:
                missing_recommended.append("DATABASE_URL")
            if missing_recommended:
                logger.warning(
                    f"Production deployment missing recommended configs: {', '.join(missing_recommended)}. "
                    "Some features may not work correctly."
                )

        return self

    # OpenAI
    openai_api_key: str | None = None
    openai_embedding_model: str = (
        "text-embedding-3-small"  # current; text-embedding-3-large for higher quality
    )
    openai_chat_model: str = "gpt-5.5"  # OpenAI flagship (2026)
    openai_utility_model: str = (
        "gpt-5.4-mini"  # cheaper model for high-volume utility/extraction calls
    )
    # Point the OpenAI-compatible client at a custom endpoint to use local/open models
    # (Ollama http://host:11434/v1, LM Studio, vLLM) or hosted OSS gateways (Together,
    # Groq, OpenRouter). Leave unset for OpenAI. Also editable at runtime via the admin UI.
    openai_base_url: str | None = None
    # Context window (tokens) of the chat model, when its name doesn't say — custom
    # or local endpoints. Long-document drafting budgets reference material against
    # it. Unset: inferred from the model name (gpt-5 → 400k, gpt-4o → 128k, ...).
    llm_context_window: int | None = None

    # Anthropic
    anthropic_api_key: str | None = None
    anthropic_model: str = (
        "claude-opus-4-8"  # current Anthropic flagship (use the bare ID, no date suffix)
    )

    # Google Gemini (used via per-user keys)
    gemini_model: str = "gemini-3.1-pro-preview"  # current Gemini Pro

    # Voyage AI (Legal Embeddings) - Optional
    voyage_api_key: str | None = None
    # Default to OpenAI embeddings. Set to voyage-law-2 (legal-optimized, still recommended for
    # legal text) or a voyage-4 model with a Voyage key. NOTE: changing the embedding model changes
    # vector dimensions/space and requires re-indexing existing documents.
    embedding_model: str = "text-embedding-3-small"
    # Send embeddings to an OpenAI-compatible server you run (Ollama, vLLM,
    # text-embeddings-inference) instead of a hosted provider, so document text
    # is embedded on your own network. When set, ALL embeddings go here using
    # EMBEDDING_MODEL, and Voyage/Cohere keys are not used for embeddings.
    # Unset: embeddings go to OpenAI / Voyage / Cohere as configured above
    # (OPENAI_BASE_URL does NOT redirect embeddings). Requires re-indexing.
    embedding_base_url: str | None = None
    # Vector size of EMBEDDING_MODEL when it is not one of the built-in models
    # (only needed to create a Pinecone index for a self-hosted model).
    embedding_dimensions: int | None = None

    # Cohere (Alternative Embeddings)
    cohere_api_key: str | None = None

    # CourtListener (Free Case Law)
    courtlistener_api_token: str | None = None  # Optional, but recommended for higher rate limits

    # Vector Database
    vector_db: str = "chroma"  # "chroma" or "pinecone"
    chroma_persist_dir: str = "./data/chroma"
    pinecone_api_key: str | None = None
    pinecone_environment: str = "us-east-1"
    pinecone_index_name: str = "casecite-legal"

    # RAG Settings - Enterprise Configuration
    chunk_size: int = 512
    chunk_overlap: int = 128
    # "structured": split at articles, sections, clauses and schedules, record
    # each chunk's section path, character span and pages. "fixed": the
    # legacy fixed token windows. Applies to documents indexed from now on;
    # re-index a document to re-chunk it.
    chunking_strategy: str = "structured"
    # Embed each structured chunk with a one-line document context (title,
    # parties) and its section path. The stored chunk text stays verbatim.
    contextual_chunk_headers: bool = True
    top_k: int = 10
    # Cosine-similarity floor for retrieval. SCALE IS MODEL-DEPENDENT: with
    # text-embedding-3-* a relevant chunk typically scores 0.30-0.50, so a
    # threshold above ~0.5 silently discards nearly every real match and the
    # product reads as "your documents don't exist". 0.25 keeps junk out
    # without strangling recall.
    similarity_threshold: float = 0.25
    # Cross-encoder reranking runs ONLY when the optional sentence-transformers
    # package is installed (it is not in requirements.txt). Without it this
    # flag has no effect: results keep their similarity / keyword-blended order.
    rerank_enabled: bool = True
    # Which reranker runs when reranking is enabled: "local" (cross-encoder,
    # needs the optional sentence-transformers package), "cohere" (hosted
    # Rerank API: candidate passages are sent to Cohere, which must be in
    # APPROVED_AI_PROVIDERS), or "none".
    reranker_provider: str = "local"
    reranker_model: str = "rerank-v3.5"
    reranker_timeout: float = 15.0
    rerank_top_k: int = 20  # Candidate pool size for keyword rescoring / reranking
    # "Hybrid" = vector search and a full-corpus keyword search (SQLite FTS5,
    # BM25) run independently and are merged by reciprocal rank fusion, so a
    # passage the vector search ranks low can still be found by its exact
    # words. If the keyword index is unavailable, hybrid falls back to BM25
    # rescoring of the vector candidates only.
    hybrid_search_enabled: bool = True
    lexical_search_enabled: bool = True
    # Keyword index file; default: lexical_index.sqlite3 beside the Chroma dir.
    lexical_index_path: str | None = None
    # Candidates each retriever contributes before fusion (at least top_k).
    hybrid_candidate_pool: int = 30
    # Weight of the keyword ranking relative to the vector ranking in the fusion.
    hybrid_keyword_weight: float = 1.0
    # Reciprocal rank fusion constant: lower values favour each list's top ranks.
    # 10 measured best on evals/ (both offline embedding modes); recheck with
    # the hosted embedding model before changing it.
    hybrid_rrf_k: int = 10
    keyword_weight: float = 0.3  # BM25 weight in the rescoring-only fallback

    # API Resilience Settings
    api_retry_attempts: int = 3
    api_retry_delay: float = 1.0  # Base delay in seconds
    # Per-request timeout (seconds) for every LLM/embedding SDK client, with
    # two SDK retries on top. Long contract reviews and drafting sections can
    # legitimately take a minute or more to produce a first byte; 30s caused
    # spurious retries. Override with API_TIMEOUT.
    api_timeout: float = 180.0
    circuit_breaker_threshold: int = 5  # Failures before circuit opens
    circuit_breaker_timeout: float = 60.0  # Seconds before retry

    # Connector: Google Drive
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # GOOGLE_REDIRECT_URI is required for production - no localhost fallback
    google_redirect_uri: str = os.environ.get("GOOGLE_REDIRECT_URI", "")

    # Connector: Microsoft (OneDrive/SharePoint)
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant_id: str | None = None
    # MICROSOFT_REDIRECT_URI is required for production - no localhost fallback
    microsoft_redirect_uri: str = os.environ.get("MICROSOFT_REDIRECT_URI", "")
    # Azure AD SSO account lifecycle. Roles always come from the local users
    # row; these only govern what happens on a first-time SSO login.
    azure_sso_auto_provision: bool = True  # False: only admin-invited users can sign in
    azure_sso_default_role: str = "viewer"  # role for auto-provisioned users (never admin)

    # Connector: Box
    box_client_id: str | None = None
    box_client_secret: str | None = None
    # BOX_REDIRECT_URI is required for production - no localhost fallback
    box_redirect_uri: str = os.environ.get("BOX_REDIRECT_URI", "")

    # Experimental connectors (iManage, NetDocuments). Implemented against the
    # vendors' published APIs but never validated against a live tenant, so
    # they are not registered — absent from GET /connectors and refused on
    # every route — until this is set.
    experimental_connectors: bool = False

    # Connector: NetDocuments (experimental, see EXPERIMENTAL_CONNECTORS)
    netdocuments_client_id: str | None = None
    netdocuments_client_secret: str | None = None
    # NETDOCUMENTS_REDIRECT_URI is required for production - no localhost fallback
    netdocuments_redirect_uri: str = os.environ.get("NETDOCUMENTS_REDIRECT_URI", "")
    # Region hosts — US vault defaults; EU tenants use vault.eu.netdocuments.com /
    # api.eu.netdocuments.com, AU uses the .au equivalents.
    netdocuments_vault_host: str = "https://vault.netvoyage.com"
    netdocuments_api_host: str = "https://api.vault.netvoyage.com"

    # Connector: iManage (experimental, see EXPERIMENTAL_CONNECTORS)
    imanage_client_id: str | None = None
    imanage_client_secret: str | None = None
    imanage_base_url: str | None = None
    # IMANAGE_REDIRECT_URI is required for production - no localhost fallback
    imanage_redirect_uri: str = os.environ.get("IMANAGE_REDIRECT_URI", "")

    # Connector: Filevine (API key + secret; session handshake, not OAuth).
    # The key is firm-wide, so the connector stays off until FILEVINE_ENABLED
    # is set, and is then usable only by users holding admin.settings.
    filevine_enabled: bool = False
    filevine_api_key: str | None = None
    filevine_api_secret: str | None = None
    filevine_base_url: str = "https://api.filevine.io"

    # Connector: Clio (EU tenants set CLIO_BASE_URL=https://eu.app.clio.com)
    clio_client_id: str | None = None
    clio_client_secret: str | None = None
    clio_base_url: str = "https://app.clio.com"
    # CLIO_REDIRECT_URI is required for production - no localhost fallback
    clio_redirect_uri: str = os.environ.get("CLIO_REDIRECT_URI", "")

    # Connector: Dropbox
    dropbox_app_key: str | None = None
    dropbox_app_secret: str | None = None
    # DROPBOX_REDIRECT_URI is required for production - no localhost fallback
    dropbox_redirect_uri: str = os.environ.get("DROPBOX_REDIRECT_URI", "")

    # ============================================
    # FILE PICKER APIs (No OAuth Verification Required)
    # These use limited scopes that don't need Google/Microsoft verification
    # ============================================

    # Google Picker API - Get from Google Cloud Console (APIs & Services > Credentials)
    # Creates an API key (not OAuth client) for the Picker
    google_api_key: str | None = None  # API key for Google Picker
    google_app_id: str | None = None  # Project number from Google Cloud Console

    # Microsoft OneDrive Picker - Uses same client_id but different scope
    # No additional config needed - uses microsoft_client_id above

    # Box UI Elements - Uses same client_id
    # No additional config needed - uses box_client_id above

    # Dropbox Chooser - Uses app key (same as dropbox_app_key above)

    # Job Queue
    job_result_ttl: int = 1800  # 30 min completed results
    job_failed_ttl: int = 86400  # 24h failed jobs
    job_dead_letter_ttl: int = 604800  # 7d dead letter
    job_max_result_size: int = 1_048_576  # 1MB max result in Redis
    job_shutdown_timeout: int = 30  # Seconds to drain on shutdown
    # Max jobs executing concurrently. Bounds memory/connection use so a burst of
    # submissions (LLM + CourtListener fan-out) can't exhaust the API process.
    job_max_concurrency: int = 5
    # Documents a tabular review extracts from at the same time within one run.
    # Each is one utility-model call; the bound keeps a 200-contract review from
    # opening 200 provider connections at once.
    review_table_concurrency: int = 4
    # Deep research (/deep-research): sub-questions researched at the same time
    # within one run, and the hard caps that keep a run's cost predictable.
    deep_research_concurrency: int = 3
    deep_research_max_subquestions: int = 8
    deep_research_max_tool_calls: int = 24
    deep_research_max_passages: int = 60
    # A sub-question may propose this many follow-up searches (0 disables).
    deep_research_followup_depth: int = 1
    # Workflows (/workflows): steps a workflow may hold, documents one step may
    # analyse or extract from, and citations one citation-check step may scan.
    # Each document and each citation is at least one outbound call.
    workflow_max_steps: int = 12
    workflow_max_documents_per_step: int = 25
    workflow_max_citations_per_step: int = 10
    # A step's stored output is truncated past this many bytes (the fields
    # that were cut are listed on the output).
    workflow_max_output_bytes: int = 524_288
    # Email the runner (and approvers) when a run completes, fails, waits for
    # approval or is rejected. Needs SENDGRID_API_KEY; each user may opt out
    # in Settings. Off by default.
    workflow_notifications: bool = False
    # Jobs kept by the in-memory fallback (Redis unavailable) before the oldest
    # finished ones are evicted. Each can hold up to job_max_result_size.
    job_memory_max_jobs: int = 200

    # Storage
    upload_dir: str = "./data/uploads"
    max_upload_size: int = 100 * 1024 * 1024  # 100MB

    # Text-extraction limits on untrusted uploads. A zip-based Office file is
    # small on the wire and arbitrarily large once inflated; a PDF can declare
    # any number of pages. Past a page/OCR cap the document is still indexed,
    # with a warning recorded on it saying what was not read.
    extraction_max_uncompressed_bytes: int = 500 * 1024 * 1024
    extraction_max_zip_members: int = 10_000
    extraction_max_pdf_pages: int = 5_000
    extraction_max_ocr_pages: int = 300

    # Case-law reading bounds. Opinions and filings are read in windows; these
    # cap the windows per item. Whatever a cap leaves unread is reported in the
    # result (partially_read / coverage), never silently skipped.
    research_max_windows_per_opinion: int = 6  # x 80k chars

    # Data-at-rest acknowledgement. Uploaded originals are app-encrypted
    # (AES-256-GCM), but DERIVED privileged text — vector chunks in the Chroma
    # store, extracted clause text, and chat history in the DB — is stored in
    # cleartext. On a single-tenant self-host that is acceptable ONLY when the
    # underlying volume is encrypted (LUKS / BitLocker / FileVault), otherwise a
    # stolen disk or laptop leaks the whole corpus. The app cannot detect
    # full-disk encryption, so production requires the operator to consciously
    # confirm it here. Set DISK_ENCRYPTION_ACKNOWLEDGED=true after enabling FDE.
    disk_encryption_acknowledged: bool = False

    # AI provider allowlist (dormant unless explicitly enabled). The env name is
    # historical: this is an allowlist, not a HIPAA compliance feature. When on,
    # every chat/utility LLM call and every embedding call made through the RAG
    # pipeline is refused unless its provider is listed (see
    # app/services/provider_policy.py for provider names, e.g. "openai",
    # "anthropic", "google", "voyage", "cohere", "self_hosted").
    hipaa_enforcement_enabled: bool = False
    approved_ai_providers: list[str] = []  # e.g., ["anthropic", "self_hosted"]

    # Dedicated HMAC key for audit chain hashing (separate from secret_key)
    audit_hmac_key: str = ""
    # Set to the OLD key when rotating audit_hmac_key so entries signed under it
    # still verify at startup; clear it once those logs age out of retention.
    audit_hmac_key_previous: str = ""

    # Halt startup if audit chain integrity is broken (tamper detection)
    audit_halt_on_tampering: bool = True

    # When an audit entry cannot be written to its JSONL file, fail the request
    # that produced it instead of continuing. Off by default: the event is
    # still logged at CRITICAL and mirrored to the database.
    audit_fail_closed: bool = False

    # Audit-log retention: JSONL files older than this are gzip-compressed; files
    # older than 2x this are deleted. A daily background task applies this.
    audit_retention_days: int = 90

    model_config = SettingsConfigDict(
        env_file=["../.env", ".env"],  # Check parent directory first, then current
        env_file_encoding="utf-8",
        frozen=True,
        # The same .env is read by docker compose and by middleware/database code
        # via os.environ (DOMAIN, POSTGRES_PASSWORD, CORS_ORIGINS, ALLOWED_HOSTS,
        # TRUSTED_PROXIES, DB_SSL, ...). Those are not Settings fields, and the
        # pydantic-settings default (forbid) turns them into a startup crash for
        # any non-Docker run (uvicorn, alembic) that loads the file directly.
        extra="ignore",
    )


settings = Settings()

# Ensure directories exist
os.makedirs(settings.chroma_persist_dir, exist_ok=True)
os.makedirs(settings.upload_dir, exist_ok=True)
