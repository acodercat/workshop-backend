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
    rate_limit_register_code: str = Field(
        default="5/hour",
        description="Per-IP registration code-send attempts (mail-spam guard, on top of the per-email cooldown)",
    )
    rate_limit_register: str = Field(
        default="20/hour",
        description="Per-IP registration completion attempts",
    )

    # Registration / email verification — values picked to match
    # mainstream consumer products (60s resend cooldown, 10min code
    # TTL, 5 wrong-attempt cap before invalidation). Tweak to taste;
    # all on one config knob avoids hardcoded magic numbers in service
    # code.
    register_code_ttl_seconds: int = Field(
        default=600,
        description="Registration verification code lifetime (s)",
    )
    register_code_resend_cooldown_seconds: int = Field(
        default=60,
        description="Min seconds between two send-code calls for the same email",
    )
    register_code_max_attempts: int = Field(
        default=5,
        description="Wrong code attempts allowed per code; exceeding invalidates the code",
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
    # Read timeout for the STREAMING pool. An SSE XREAD blocks up to
    # ``read_stream``'s ``block_ms`` (15s), so this MUST exceed it — else
    # the blocking read trips the socket timeout and the SSE dies mid-poll.
    # (The short-op pool sets its own, much shorter, socket_timeout — see
    # ``redis_socket_timeout_seconds``.) The headroom above 15s is what lets
    # a genuinely dead connection still surface as a timeout instead of
    # hanging until TCP keepalive notices.
    redis_streaming_socket_timeout_seconds: float = Field(
        default=30.0,
        description="Socket read timeout for the SSE streaming pool; must exceed read_stream block_ms (15s).",
    )
    # Socket read/write timeout for the SHORT-OP pool. Its commands are
    # sub-millisecond, so a short timeout just bounds how long a silently-
    # dead connection (e.g. one the Redis server closed for idleness)
    # stalls a request before it surfaces as a TimeoutError — which
    # ``retry`` then transparently recovers by reconnecting. Without it a
    # dead socket waits out the kernel's TCP retransmits (~tens of seconds,
    # errno 110), failing the request outright.
    redis_socket_timeout_seconds: float = Field(
        default=5.0,
        description="Socket read/write timeout for the short-op pool; bounds dead-connection stalls before retry reconnects.",
    )
    # Short-op pool uses BlockingConnectionPool: when full, callers wait
    # this long for a connection before raising. Trades a tail-latency
    # spike for fewer 5xx during traffic bursts.
    redis_pool_timeout_seconds: float = Field(default=3.0, description="Wait N seconds for a short-op connection before failing")

    # Observability. Sentry stays a no-op until ``sentry_dsn`` is set, so
    # dev boxes boot without it (same pattern as the DirectMail creds);
    # the Prometheus ``/metrics`` endpoint has no external dependency, so
    # it defaults on.
    sentry_dsn: str = Field(default="", description="Sentry DSN; empty disables error tracking")
    sentry_environment: str = Field(default="", description="Sentry environment tag (e.g. production, staging)")
    sentry_traces_sample_rate: float = Field(default=0.0, description="Fraction of requests traced by Sentry performance monitoring")
    metrics_enabled: bool = Field(default=True, description="Expose Prometheus metrics at /metrics")

    # Alibaba Cloud OSS — credentials required from the environment; no
    # defaults, so a forgotten ``.env`` fails loudly at startup instead
    # of falling back to baked-in keys.
    oss_access_key_id: str = Field(description="OSS access key id")
    oss_access_key_secret: str = Field(description="OSS access key secret")
    oss_document_bucket_name: str = Field(default="metaseq-computer", description="OSS document bucket")
    oss_skill_bucket_name: str = Field(default="axon-skills", description="OSS bucket for user-uploaded SKILL.md bundles")
    oss_endpoint: str = Field(default="https://oss-cn-shenzhen.aliyuncs.com", description="OSS endpoint URL")

    # Alibaba Cloud DirectMail — used to send the registration
    # verification code. Independent AK pair from OSS so the RAM
    # subaccount can be locked down to ``AliyunDirectMailFullAccess``
    # only. Defaults are empty strings (instead of required) so dev
    # environments without DM access can still boot — calls that need
    # DM will fail loudly with a clearer error than a Pydantic
    # validation panic at import.
    aliyun_dm_access_key_id: str = Field(default="", description="DirectMail RAM AccessKey ID")
    aliyun_dm_access_key_secret: str = Field(default="", description="DirectMail RAM AccessKey Secret")
    aliyun_dm_region: str = Field(
        default="cn-hangzhou",
        description="DirectMail service region; ``ap-southeast-1`` for the Singapore node",
    )
    aliyun_dm_account_name: str = Field(
        default="",
        description="Verified sender address (must be approved + SPF/DKIM passed in DM console)",
    )
    aliyun_dm_from_alias: str = Field(
        default="AXON",
        description="``发送人名称`` shown in the recipient's inbox; ≤30 chars",
    )

    # LLM — used only for light-weight synchronous calls from the
    # backend itself (e.g. session-title generation). Agent turns run
    # in the worker and use its own copy of these settings.
    llm_base_url: str = Field(description="OpenAI-compatible API base URL")
    llm_api_key: str = Field(description="LLM API key")
    llm_model: str = Field(default="qwen3.5-397b-a17b", description="Default chat model")

    # File limits — per-message attachment count, per-file byte cap,
    # and per-user total session-file accumulation. Tuned for typical
    # human use; raise when a clear case (e.g. enterprise workloads)
    # appears. The byte cap also bounds OSS upload presign size and is
    # baked into the per-file ``size_bytes`` Pydantic validator on
    # ``MessageCreate``.
    max_message_files: int = Field(
        default=30,
        description="Max attachments per single user message",
    )
    max_file_bytes: int = Field(
        default=100 * 1024 * 1024,
        description="Per-file upload size cap (bytes)",
    )
    max_session_files_per_user: int = Field(
        default=1000,
        description="Total session-file rows allowed per user across all sessions",
    )

    # Sessions
    supported_session_model_ids: frozenset[str] = Field(
        default=frozenset({
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "kimi-k2",
            "HKGAI-V1",
            "HKGAI-V3",
            "qwen3.7-max",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "glm-5.1",
            "MiniMax/MiniMax-M2.7",
        }),
        description="Whitelist of model_ids that POST /api/sessions accepts. Must match keys in agent-worker/models.toml; mismatches surface as a worker-side ValueError on the next turn.",
    )
    # Capability tiers shown in the UI → the concrete model_id each resolves
    # to. The client picks a ``tier`` (FAST / STANDARD / ADVANCED); the model
    # name is never sent over the wire. ``create_session`` looks the tier up
    # here and stamps the resolved id on ``session.model_id``. Every value
    # must be in ``supported_session_model_ids`` above and priced in
    # ``credit_rates`` below — ``create_session`` re-checks the whitelist.
    session_tier_models: dict[str, str] = Field(
        default_factory=lambda: {
            "FAST": "deepseek-v4-flash",
            "STANDARD": "kimi-k2",
            "ADVANCED": "qwen3.7-max",
        },
        description="Maps the UI capability tier (FAST/STANDARD/ADVANCED) to the model_id stamped on the session. Values must be in supported_session_model_ids.",
    )

    # Credit / quota rate card. The peg is ``1 credit = $0.01 USD of
    # cost``; one credit is split into 10_000_000 µ-credits at the storage
    # layer so all arithmetic stays in ``bigint`` (see
    # :mod:`app.services.credits`). One entry per model_id in
    # ``supported_session_model_ids`` — missing entries make the reserve
    # path raise loudly rather than silently charging zero.
    #
    # Three-factor billing per turn (see :mod:`app.services.credits.rates`):
    #   cost = α·tokens_cost + β·turn_seconds + γ·model_fee
    # Per-model knobs here:
    #   - ``in_per_1k_usd`` / ``out_per_1k_usd``: provider COGS in USD.
    #   - ``token_markup`` (α): multiplier on top of token COGS. Bakes
    #     gross margin + FX buffer for the LLM-cost portion only.
    #   - ``model_fee_credits`` (γ): per-turn fixed fee charged for
    #     choosing this model; applies even on a near-zero-token turn.
    #     0 disables the fee for that model.
    #   - ``reserve_max_output_tokens``: pessimistic upper bound on
    #     output, used at reserve time. Settle multi-refunds the unused
    #     portion from actual usage.
    # The per-second knob (β) is global, not per-model — see
    # ``credit_per_second_credits`` below.
    credit_rates: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "claude-opus-4-7":   {"in_per_1k_usd": 0.005,   "out_per_1k_usd": 0.025,  "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 4096},
            "claude-opus-4-6":   {"in_per_1k_usd": 0.005,   "out_per_1k_usd": 0.025,  "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 4096},
            "claude-sonnet-4-6": {"in_per_1k_usd": 0.003,   "out_per_1k_usd": 0.015,  "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            "kimi-k2":           {"in_per_1k_usd": 0.00055, "out_per_1k_usd": 0.0022, "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            "HKGAI-V1":          {"in_per_1k_usd": 0.001,   "out_per_1k_usd": 0.005,  "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            "HKGAI-V3":          {"in_per_1k_usd": 0.001,   "out_per_1k_usd": 0.005,  "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            # DashScope-hosted models. USD COGS derived from Aliyun 百炼 pricing pages
            # (CNY converted at ~1 USD = 7.2 CNY). Conservative: glm-5.1 uses the
            # <32K-context tier; longer turns nominally cost ~33% more on the wire
            # but the 3× ``token_markup`` more than absorbs that gap. qwen3.7-max
            # uses Qwen3.6-Max-Preview rates as proxy (3.7 preview pricing not yet
            # officially published, expected ≤3.6's). deepseek-v4-pro uses post-promo
            # standard rates ($1.74/$3.48 per M) to avoid charging below COGS once
            # the 2026-05-31 promo expires.
            "qwen3.7-max":       {"in_per_1k_usd": 0.0013,  "out_per_1k_usd": 0.0078, "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            "deepseek-v4-pro":   {"in_per_1k_usd": 0.00174, "out_per_1k_usd": 0.00348,"token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            # deepseek-v4-flash budget tier — permanent ¥1/¥2 per M
            # tokens (≈$0.14/$0.28 per M), no promo expiry. Cheaper than
            # v4-pro by ~10×; same DashScope endpoint and reasoning knobs.
            "deepseek-v4-flash": {"in_per_1k_usd": 0.00014, "out_per_1k_usd": 0.00028,"token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            "glm-5.1":           {"in_per_1k_usd": 0.00083, "out_per_1k_usd": 0.0033, "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
            # MiniMax M2.7 namespaced SKU on DashScope: ¥2.1/¥8.4 per M tokens
            # → $0.000292/$0.00117 per 1K (1 USD ≈ 7.2 CNY). Cached input
            # ¥0.42/M not separately tracked here; settle uses uncached rate.
            "MiniMax/MiniMax-M2.7": {"in_per_1k_usd": 0.000292, "out_per_1k_usd": 0.00117, "token_markup": 3.0, "model_fee_credits": 0, "reserve_max_output_tokens": 8192},
        },
        description="Per-model rate card for credit billing. Snapshotted onto each credit_reservation at reserve time so a config change can't re-price open turns.",
    )
    # β (per-second infrastructure occupancy fee). Whole credits per
    # second of ``terminated_at - created_at``. Global, not per-model
    # — sandbox/worker capacity doesn't care which LLM is running.
    # Enabled in the 2026-05 unit-economics review
    # (``scripts/pricing_analysis.py``): the duration fee covers sandbox
    # occupancy on long-but-low-token turns, which were the entire
    # loss-making tail (e.g. sonnet 16.7% of turns sold below cost when
    # β=0). At ≈$0.0001/s ($0.36/sandbox-hour) it clears that tail.
    # Re-run the script against real sandbox cost to recalibrate.
    # Value is 0.01 (not 0.1) because the 2026-05 redenomination made
    # 1 credit worth $0.01 (10× the prior $0.001) — see
    # ``_MICRO_PER_CREDIT`` in credits/rates.py; the USD/s fee is
    # unchanged. Per-message reserve hold from duration = 1860s × 0.01 ≈
    # 19 credits ($0.19); watch for low-balance users hitting
    # InsufficientCreditsError on send.
    credit_per_second_credits: float = Field(
        default=0.01, ge=0.0,
        description="Credits per second of turn duration (β). 0 = disabled.",
    )
    # Pessimistic upper bound on ``turn_seconds`` used at reserve time
    # so the hold covers a fully-occupied worker. Matches the worker's
    # ``max_execution_time`` (1800s) + its hard timeout buffer (~60s).
    credit_reserve_max_turn_seconds: int = Field(
        default=1860, gt=0,
        description="Upper bound on turn_seconds assumed at reserve time. Settle uses actual.",
    )
    # Pessimistic estimate for the prompt size at reserve time, used when
    # we can't measure the worker's compacted context cheaply from the
    # backend side. Real cost is settled from the worker's actual
    # ``usage.prompt_tokens`` post-turn.
    credit_reserve_max_input_tokens: int = Field(
        default=16_000,
        description="Upper bound on input tokens assumed at reserve time. Settle uses actual usage.",
    )
    # Auto-grant on registration. 500 credits at the post-redenomination
    # peg (1 credit = $0.01) is $5 of headroom — same USD value as the
    # pre-2026-05 5_000-credit grant, just renumbered with the 10×
    # denomination change. Generous "try the product" allowance; note
    # this is large enough that signup-farming abuse is a real
    # consideration (gate behind email/phone verification + rate limits).
    # Set to 0 to disable (every new user starts at 0 balance, admin must
    # top up). The grant uses a deterministic idempotency key
    # ``welcome-{user_id}`` so a registration retry after partial
    # failure can't double-grant.
    credit_welcome_grant_credits: int = Field(
        default=500,
        description="Whole credits granted to every new user at registration. Set to 0 to disable.",
    )
    # Customer-facing RMB sale price of ONE credit, used only to convert a
    # top-up payment (¥) into credits — the domestic-market recharge knob.
    # Deliberately distinct from the internal cost peg (1 credit ≈ $0.01 ≈
    # ¥0.072 of LLM COGS): selling at ¥0.10 against a ¥0.072 cost basis
    # bakes ~28% margin into the recharge step itself, on top of the
    # per-turn token markup. Consumed by ``rates.credits_from_cny`` (admin
    # ``topup_credits --cny`` today, self-serve Alipay/WeChat later). Does
    # NOT touch billing / settle / balance — those stay on the µ-credit
    # ledger and the USD cost peg; this is pure sell-side pricing.
    credit_price_cny: float = Field(
        default=0.1, gt=0.0,
        description="Customer-facing RMB price of 1 credit at top-up. ¥0.1 → ¥100 buys 1000 credits.",
    )


settings = Settings()
