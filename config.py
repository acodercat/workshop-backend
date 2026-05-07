from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

MINUTES_PER_WEEK = 60 * 24 * 7


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Database
    db_uri: str = Field(description="PostgreSQL connection URI")
    db_pool_size: int = Field(default=30, description="SQLAlchemy pool size")
    db_max_overflow: int = Field(default=10, description="SQLAlchemy max overflow connections")
    db_pool_recycle_seconds: int = Field(default=300, description="Recycle connections after N seconds")
    db_pool_timeout_seconds: int = Field(default=30, description="Wait N seconds for a free connection")
    db_echo: bool = Field(default=False, description="Echo SQL statements (debug only)")
    db_echo_pool: bool = Field(default=False, description="Echo connection-pool events (debug only)")

    # Startup readiness — how long to wait for DB / Redis to come up
    # before giving up. Bumped on container orchestrators where
    # dependencies may not be ready when this process starts.
    startup_probe_attempts: int = Field(default=20, description="Max DB/Redis ping attempts at startup")
    startup_probe_base_delay_seconds: float = Field(default=0.5, description="Initial backoff for startup probes (exponential)")
    startup_probe_max_delay_seconds: float = Field(default=10.0, description="Cap for startup-probe backoff")

    # Security
    secret_key: str = Field(description="Secret key for JWT encoding")
    algorithm: str = Field(default="HS256", description="Algorithm for JWT")
    access_token_expire_minutes: int = Field(
        default=MINUTES_PER_WEEK,
        description="Access token lifetime in minutes",
    )
    # Share-view tokens are minted after a recipient enters the share's
    # password. Short TTL because the recipient is expected to be
    # actively viewing — re-prompting after 15 min idle is acceptable
    # and limits the blast radius of a leaked token.
    share_view_token_expire_minutes: int = Field(
        default=15,
        description="Share-view token lifetime in minutes",
    )
    bcrypt_rounds: int = Field(default=12, description="Bcrypt work factor (10-14 typical)")

    # CORS — explicit allowlist; wildcard is forbidden because we use
    # ``allow_credentials=True`` (browsers reject the combination).
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3300"],
        description="Allowed origins for CORS",
    )

    # Rate limits — slowapi key is the client IP. Sized for human use;
    # raise behind authenticated identity if abuse appears.
    rate_limit_default: str = Field(default="200/minute", description="Default per-IP rate limit")
    rate_limit_login: str = Field(default="5/minute", description="Per-IP login attempts")
    rate_limit_message_create: str = Field(default="30/minute", description="Per-IP message-create rate")
    rate_limit_share_unlock: str = Field(
        default="5/5minute",
        description="Per-IP share-unlock attempts (password brute-force guard)",
    )

    # Redis — two pools, sized differently because their workloads
    # don't mix safely. ``redis_pool_size`` serves short ops (XADD,
    # PUBLISH, DELETE, PING). ``redis_streaming_pool_size`` serves
    # long-poll XREAD inside SSE generators, where each connection
    # is held for ``block_ms`` (15s) at a time. A single shared pool
    # would let SSE traffic starve the short-op path.
    redis_url: str = Field(description="Redis connection URL")
    redis_pool_size: int = Field(default=30, description="Max connections for short Redis ops")
    redis_streaming_pool_size: int = Field(default=100, description="Max connections for SSE long-poll (per worker process)")
    # Short-op pool uses BlockingConnectionPool: when full, callers wait
    # this long for a connection before raising. Trades a tail-latency
    # spike for fewer 5xx during traffic bursts.
    redis_pool_timeout_seconds: float = Field(default=3.0, description="Wait N seconds for a short-op connection before failing")

    # Alibaba Cloud OSS — credentials required from the environment; no
    # defaults, so a forgotten ``.env`` fails loudly at startup instead
    # of falling back to baked-in keys.
    oss_access_key_id: str = Field(description="OSS access key id")
    oss_access_key_secret: str = Field(description="OSS access key secret")
    oss_document_bucket_name: str = Field(default="metaseq-computer", description="OSS document bucket")
    oss_endpoint: str = Field(default="https://oss-cn-shenzhen.aliyuncs.com", description="OSS endpoint URL")

    # LLM — used only for light-weight synchronous calls from the
    # backend itself (e.g. session-title generation). Agent turns run
    # in the worker and use its own copy of these settings.
    llm_base_url: str = Field(description="OpenAI-compatible API base URL")
    llm_api_key: str = Field(description="LLM API key")
    llm_model: str = Field(default="qwen3.5-397b-a17b", description="Default chat model")

    # Sessions
    default_session_model_id: str = Field(
        default="claude-opus-4-6",
        description="Model id stamped on a new session when the client doesn't pick one explicitly. Recorded on session.model_id at creation time and immutable for the rest of the session.",
    )
    supported_session_model_ids: frozenset[str] = Field(
        default=frozenset({
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "kimi-k2",
        }),
        description="Whitelist of model_ids that POST /api/sessions accepts. Must match keys in agent-worker/models.toml; mismatches surface as a worker-side ValueError on the next turn.",
    )


settings = Settings()
