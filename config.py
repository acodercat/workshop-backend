from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from limits import parse_many
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MINUTES_PER_WEEK = 60 * 24 * 7
MINUTES_PER_WORKDAY = 60 * 8


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
    db_pool_recycle_seconds: int = Field(
        default=300, description="Recycle connections after N seconds"
    )
    db_pool_timeout_seconds: int = Field(
        default=30, description="Wait N seconds for a free connection"
    )
    db_echo: bool = Field(default=False, description="Echo SQL statements (debug only)")
    db_echo_pool: bool = Field(
        default=False, description="Echo connection-pool events (debug only)"
    )

    # Startup readiness — how long to wait for DB / Redis to come up
    # before giving up. Bumped on container orchestrators where
    # dependencies may not be ready when this process starts.
    startup_probe_attempts: int = Field(
        default=20, description="Max DB/Redis ping attempts at startup"
    )
    startup_probe_base_delay_seconds: float = Field(
        default=0.5, description="Initial backoff for startup probes (exponential)"
    )
    startup_probe_max_delay_seconds: float = Field(
        default=10.0, description="Cap for startup-probe backoff"
    )

    # Security
    secret_key: str = Field(min_length=32, description="Secret key for JWT encoding")

    # Fernet key (44-char urlsafe base64) for encrypting Connector credentials
    # at rest. Generate with ``Fernet.generate_key()``. Empty default so a
    # deploy without the Connector feature still boots; connector create/probe
    # fails loudly at call time if unset. MUST match the worker's value.
    mcp_credential_encryption_key: str = Field(
        default="",
        description="Fernet key for encrypting MCP Connector credentials at rest",
    )

    @field_validator("mcp_credential_encryption_key")
    @classmethod
    def _a_fernet_key_when_set(cls, key: str) -> str:
        if key:
            Fernet(key.encode())  # refuses anything but a Fernet key, at boot rather than first use
        return key

    @field_validator("identity_credential_encryption_key")
    @classmethod
    def _a_fernet_key(cls, key: str) -> str:
        # Required, so empty is refused too: a copied ``.env.example`` must
        # fail at boot, not at the first sign-in.
        Fernet(key.encode())
        return key

    # OAuth Connector redirect URI — the public callback the provider sends the
    # browser back to. Must match a route the frontend serves and be registered
    # (for manual OAuth apps). Empty default → OAuth connectors fail loudly at
    # authorize time rather than at startup.
    # e.g. https://app.example.com/api/connectors/oauth/callback
    mcp_oauth_redirect_uri: str = Field(
        default="",
        description="Public OAuth callback URL for MCP Connectors",
    )
    mcp_oauth_state_ttl_seconds: int = Field(
        default=600,
        description="TTL for the pending-OAuth-authorization Redis state",
    )
    # Public URL of our OAuth Client ID Metadata Document (CIMD): the URL *is*
    # the client_id, and the authorization server fetches it instead of us
    # registering. Empty default → fall back to Dynamic Client Registration,
    # which is the only option for servers that don't advertise CIMD anyway.
    #
    # **The app's origin, not this API's.** The document is served by the app's
    # ``/api/connectors/client-metadata`` proxy route, which relays this API's
    # ``GET /connectors/client-metadata`` — the same origin the redirect URI
    # above uses, and the only one published: nginx fronts the app, and this
    # API's own port is not reachable from outside. Reachability is never
    # checked (``is_valid_client_metadata_url`` only asks for HTTPS and a
    # non-root path), and the CIMD branch returns without falling back to
    # registration, so a URL the provider cannot fetch does not degrade to DCR —
    # it fails the authorization outright, which is worse than leaving this empty.
    # e.g. https://app.example.com/api/connectors/client-metadata
    mcp_client_metadata_url: str = Field(
        default="",
        description="Public URL of our OAuth client metadata document (CIMD)",
    )
    # How this client names itself to an authorization server — what the user
    # reads on the consent screen, so it has to be this deployment's own brand.
    #
    # It lives here rather than in ``services/mcp/oauth.py`` because that module
    # must stay byte-identical with master and this is the one line in it each
    # deployment wants different: forking it there is a conflict on every OAuth
    # sync, resolved by hand, where getting it wrong shows another deployment's
    # name to the user and nothing else notices. ``config.py`` is where the two
    # already diverge on purpose, and the **default** carries it — not the
    # ``.env`` — so this deployment is correct without anyone remembering to set
    # it. Master's default reads ``Axon Connector``; a merge conflicts here, and
    # that is the point.
    mcp_client_name: str = Field(
        default="HKGAI Connector",
        description="Client name presented to OAuth authorization servers",
    )
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

    # Admin-console tokens (``scope="staff"``). A workday rather than the
    # customer token's week: the console is the highest-privilege surface
    # and is used from a workstation, so a stolen token's window should
    # close by the next morning.
    admin_access_token_expire_minutes: int = Field(
        default=MINUTES_PER_WORKDAY,
        description="Staff access token lifetime in minutes",
    )

    # Identity provider — sign-in is federated to the Meeting service; see
    # ``app/services/identity``. The backend is the only party that talks
    # to it: the browser and the frontend proxy hold a backend-issued token
    # and never a Meeting credential.
    meeting_service_url: str = Field(
        default="https://hkmeeting.net",
        description=(
            "Origin of the Meeting service. Also the ``issuer`` every identity "
            "it vouches for is filed under, so repointing it at a different "
            "deployment starts a new, empty account namespace rather than "
            "inheriting the old one's users"
        ),
    )

    @field_validator("meeting_service_url")
    @classmethod
    def _a_bare_https_origin(cls, url: str) -> str:
        # Passwords and session cookies travel to this host, and its exact
        # spelling is stored as every identity's issuer — so it is held to
        # one canonical form: https, no path, no trailing slash.
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or url != f"https://{parts.netloc}":
            raise ValueError("must be a bare https origin, e.g. https://hkmeeting.net")
        return url

    meeting_app_id: str = Field(
        default="2",
        description="The application id Meeting knows this backend's requests by",
    )
    meeting_session_cookie_name: str = Field(
        default="passport",
        description="Name of the cookie Meeting carries its session in",
    )
    meeting_accept_language: str = Field(
        default="zh-HK",
        description="Language Meeting writes its own emails and messages in",
    )
    # Meeting's sign-in takes the password RSA-encrypted under this key
    # (PKCS#1 v1.5, base64). It is a public key, so it is not a secret —
    # but it is Meeting's to rotate, which is why it is configuration and
    # not a constant: a rotation is an ``.env`` edit, not a release.
    meeting_login_public_key: str = Field(
        default=(
            "-----BEGIN PUBLIC KEY-----\n"
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsH3ScEMwVN6DD6S9BgdU\n"
            "mUV/oI/J23zoAQsIgNhOjSLsav9TOWuJ3oQ35VfNrP+LKnEX+YL1x6/QJHhxNm2l\n"
            "s/ljV/VyNE7Y0KslZp/p/XAh55fQu6JYzG9chd+wHeW3SyiEplxdQZXqAeh/X7z4\n"
            "VWp3jJfSZ+qDjlr8v/xA4x6Ksd/N/Wlz4B8VE15g9sZ4e+lnDmErJsMSFSDbXmMa\n"
            "RV3GFGuMmQ+EPBVgps7tIj/Wd+RlBGQS0wAarI0SjzA1miwq7QUbUifQyEvmmsVX\n"
            "UTM7WCGv/UGb2YbhgFMaAWT0Cz8f4PSIusNr9q/TEJP4l9oDeDqGzoLVZSPVU2Qu\n"
            "NwIDAQAB\n"
            "-----END PUBLIC KEY-----\n"
        ),
        description="PEM public key Meeting's sign-in expects passwords encrypted under",
    )
    meeting_request_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        description="Timeout for one ordinary call to Meeting",
    )
    # Meeting's generation endpoints stream for minutes, not seconds.
    meeting_overview_timeout_seconds: float = Field(
        default=180.0, gt=0, description="Timeout for Meeting writing a meeting's minutes"
    )
    meeting_translate_timeout_seconds: float = Field(
        default=120.0, gt=0, description="Timeout for Meeting translating a meeting's minutes"
    )
    # The system model for meetings: an OpenAI-compatible endpoint, which is
    # the backend's own LLM unless set. ``meeting_models`` lists the models
    # an administrator may choose between; the first is the default.
    meeting_llm_base_url: str = Field(
        default="", description="OpenAI-compatible base URL for meeting AI; empty → llm_base_url"
    )
    meeting_llm_api_key: str = Field(
        default="", description="API key for meeting_llm_base_url; empty → llm_api_key"
    )
    meeting_models: list[str] = Field(
        default_factory=list,
        description="Models administrators may choose for meetings; empty → [llm_model]",
    )
    meeting_llm_timeout_seconds: float = Field(
        default=120.0, gt=0, description="Timeout for one meeting-AI completion"
    )
    # Recordings are read whole into memory to be handed to Meeting.
    meeting_max_upload_bytes: int = Field(
        default=200 * 1024 * 1024,
        gt=0,
        description="Largest meeting recording that can be uploaded",
    )
    # A live recording that receives no audio for this long without being
    # finished — the browser closed, the network died — is closed by the
    # archiver and archived with what arrived.
    meeting_abandoned_recording_hours: int = Field(
        default=6, gt=0, description="Close an unfinished live recording idle this long"
    )
    # Fernet key for the provider credential each ``identity_session`` row
    # holds. Its own key rather than ``mcp_credential_encryption_key``: that
    # one is shared with the worker, and the worker has no business being
    # able to read a user's Meeting session. Required — without it nobody
    # can sign in — so there is no empty default to boot on.
    identity_credential_encryption_key: str = Field(
        description="Fernet key for encrypting identity-provider credentials at rest",
    )
    # How stale the provider's word about a live session may get. Every
    # ``interval`` a request re-asks Meeting whether the session still
    # stands, so a sign-out or a ban over there reaches us within that
    # long. While Meeting cannot be reached a session keeps working on its
    # last confirmation for ``grace``, then is refused until Meeting
    # answers — an outage of theirs should not sign everyone out, and
    # should not suspend revocation indefinitely either.
    identity_reverify_interval_seconds: int = Field(
        default=300,
        gt=0,
        description="Re-confirm a session with the identity provider this often",
    )
    identity_reverify_grace_seconds: int = Field(
        default=1800,
        gt=0,
        description="Longest a session may run unconfirmed while the provider is unreachable",
    )
    identity_reverify_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Timeout for a re-confirmation. Shorter than the interactive one: it "
            "runs inside an ordinary request, whose caller did not ask for it"
        ),
    )

    @model_validator(mode="after")
    def _grace_covers_an_interval(self) -> "Settings":
        # A session is only re-asked every interval; a grace shorter than
        # that would refuse sessions nobody had tried to confirm.
        if self.identity_reverify_grace_seconds < self.identity_reverify_interval_seconds:
            raise ValueError(
                "identity_reverify_grace_seconds must be at least identity_reverify_interval_seconds"
            )
        return self

    # CORS — explicit allowlist; wildcard is forbidden because we use
    # ``allow_credentials=True`` (browsers reject the combination).
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3300"],
        description="Allowed origins for CORS",
    )
    # The admin console runs as its own app on its own port and must not
    # inherit the customer allowlist — a customer-facing origin has no
    # business calling admin endpoints from a browser.
    admin_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3400"],
        description="Allowed origins for CORS on the admin app",
    )

    # Rate limits. A decorated limit on an authenticated route keys on the
    # user; everything else — pre-login routes and the global floor — on the
    # client IP (see ``app/rate_limit.py``). Sized for human use.
    #
    # ``trust_proxy_headers``: when the API sits behind a reverse proxy,
    # ``request.client.host`` is the proxy's IP (one shared rate-limit
    # bucket for all clients). Enable to key on the first ``X-Forwarded-For``
    # hop instead. Leave OFF unless a proxy that overwrites client-supplied
    # XFF is actually in front — otherwise clients can spoof the header and
    # bypass the limit.
    trust_proxy_headers: bool = Field(
        default=False,
        description="Key rate limits on the first X-Forwarded-For hop (only behind a trusted proxy).",
    )
    rate_limit_default: str = Field(
        default="200/minute",
        description="Global floor for routes without their own limit, per client IP",
    )
    rate_limit_login: str = Field(default="5/minute", description="Per-IP login attempts")
    # Staff sign-in. Tighter than the customer limit: the operator pool is
    # tiny and known, so a burst of attempts is far more likely to be an
    # attack than a forgetful user.
    rate_limit_admin_login: str = Field(
        default="5/minute",
        description="Per-IP admin-console login attempts",
    )
    rate_limit_message_create: str = Field(
        default="30/minute", description="Per-user message-create rate"
    )
    rate_limit_upload_url: str = Field(
        default="300/minute",
        description=(
            "Per-user presigned upload-URL mint rate. The composer mints one "
            "URL per attached file, in parallel, up to the per-message file "
            "cap — so this must sit comfortably above ``max_message_files`` to "
            "leave headroom for a user attaching a full batch (and re-adding / "
            "retrying) within one window. OSS storage abuse is gated by the "
            "per-user size/count quota (``assert_upload_quota``), not "
            "by this request-rate limit."
        ),
    )
    max_concurrent_running_tasks: int = Field(
        default=10,
        ge=1,
        description="Max simultaneously RUNNING tasks (sessions) per user; a new turn past this is rejected (429).",
    )
    rate_limit_share_unlock: str = Field(
        default="5/5minute",
        description="Per-IP share-unlock attempts (password brute-force guard)",
    )
    rate_limit_register_code: str = Field(
        default="3/minute;20/hour",
        description=(
            "Per-IP limit on asking the identity provider to send a "
            "verification code. Two layered limits per OWASP OTP guidance: a "
            "3/minute burst cap + a 20/hour ceiling — generous enough not to "
            "false-positive NAT/CGNAT-shared IPs, on top of the provider's own "
            "throttling. slowapi enforces every ';'-separated limit."
        ),
    )
    rate_limit_register: str = Field(
        default="20/hour",
        description="Per-IP registration and password-reset attempts",
    )

    @field_validator(
        "rate_limit_default",
        "rate_limit_login",
        "rate_limit_admin_login",
        "rate_limit_message_create",
        "rate_limit_upload_url",
        "rate_limit_share_unlock",
        "rate_limit_register_code",
        "rate_limit_register",
    )
    @classmethod
    def _a_rate_limit(cls, rate: str) -> str:
        parse_many(rate)  # slowapi parses it per request; a typo fails boot instead
        return rate

    # Redis — two pools, sized differently because their workloads
    # don't mix safely. ``redis_pool_size`` serves short ops (XADD,
    # PUBLISH, DELETE, PING). ``redis_streaming_pool_size`` serves
    # long-poll XREAD inside SSE generators, where each connection
    # is held for ``block_ms`` (15s) at a time. A single shared pool
    # would let SSE traffic starve the short-op path.
    redis_url: str = Field(description="Redis connection URL")
    redis_pool_size: int = Field(default=30, description="Max connections for short Redis ops")
    redis_streaming_pool_size: int = Field(
        default=100, description="Max connections for SSE long-poll (per worker process)"
    )
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
    redis_pool_timeout_seconds: float = Field(
        default=3.0, description="Wait N seconds for a short-op connection before failing"
    )

    # Observability. Sentry stays a no-op until ``sentry_dsn`` is set, so
    # dev boxes boot without it (same pattern as the SMTP creds);
    # the Prometheus ``/metrics`` endpoint has no external dependency, so
    # it defaults on.
    sentry_dsn: str = Field(default="", description="Sentry DSN; empty disables error tracking")
    sentry_environment: str = Field(
        default="", description="Sentry environment tag (e.g. production, staging)"
    )
    sentry_traces_sample_rate: float = Field(
        default=0.0, description="Fraction of requests traced by Sentry performance monitoring"
    )
    metrics_enabled: bool = Field(default=True, description="Expose Prometheus metrics at /metrics")

    # Alibaba Cloud OSS — credentials required from the environment; no
    # defaults, so a forgotten ``.env`` fails loudly at startup instead
    # of falling back to baked-in keys.
    oss_access_key_id: str = Field(description="OSS access key id")
    oss_access_key_secret: str = Field(description="OSS access key secret")
    oss_document_bucket_name: str = Field(
        default="metaseq-computer", description="OSS document bucket"
    )
    oss_skill_bucket_name: str = Field(
        default="axon-skills", description="OSS bucket for user-uploaded SKILL.md bundles"
    )
    oss_endpoint: str = Field(
        default="https://oss-cn-shenzhen.aliyuncs.com", description="OSS endpoint URL"
    )
    # Custom domain (CNAME) bound to the document bucket, e.g.
    # https://files.metaseq.com.cn. Set this to make inline preview URLs
    # render in the browser: the default ``*.aliyuncs.com`` endpoint forces
    # ``Content-Disposition: attachment`` (download) in mainland regions and
    # forbids overriding it, so a bound custom domain is required to serve
    # HTML inline. Unset → inline URLs fall back to the default endpoint
    # (they'll download, but nothing breaks). Download URLs are unaffected.
    oss_document_domain: str | None = Field(
        default=None,
        description="Custom domain bound to the document bucket for inline previews, e.g. https://files.metaseq.com.cn",
    )
    # Bare host of the wildcard subdomain serving published sites — the same
    # value the worker writes under its ``preview_domain`` key. A site
    # published with slug ``s`` is reachable at ``https://{s}.{this}/``; the
    # ``GET /sessions/sites`` endpoint derives that URL from each row's slug
    # rather than storing it, so a domain change can't strand stale columns.
    oss_preview_domain: str = Field(
        default="live.metaseq.com.cn",
        description="Bare host of the published-site subdomain, e.g. live.metaseq.com.cn",
    )
    # Orphan-upload reaping (M5): presigned session-file uploads are born
    # tagged ``lifecycle=orphan``; a bucket lifecycle rule deletes objects
    # still carrying the tag after ~1 day (set up via
    # ``scripts/setup_oss_lifecycle.py``), and the tag is cleared when the
    # file is bound to a session. OFF by default — turning it on requires
    # the frontend to send the signed ``x-oss-tagging`` header (it does so
    # only when the upload-url response carries ``tagging_header``, so the
    # two stay in lockstep per request), the bucket lifecycle rule, and a
    # CORS ``allowed-header`` for ``x-oss-tagging``. See docs/M5 notes.
    oss_orphan_tagging_enabled: bool = Field(
        default=False,
        description="Tag presigned session-file uploads for lifecycle reaping; clear the tag on bind.",
    )

    # SMTP — used to send operator notices (account activation, credit
    # top-up). Lark Suite Mail by default (smtp.larksuite.com). Defaults
    # are empty strings (instead of required) so dev environments
    # without SMTP access can still boot — calls that need SMTP will
    # fail loudly at send time with a clearer error than a Pydantic
    # validation panic at import.
    #
    # Upstream cap (Lark Suite): 200 sends / 100 s and 6000 sends /
    # day per sender. Operator-triggered notices stay far below both —
    # no app-level throttling needed.
    smtp_host: str = Field(default="smtp.larksuite.com", description="SMTP server hostname")
    smtp_port: int = Field(
        default=465,
        description="SMTP port; 465 for SSL-on-connect, 587 for STARTTLS",
    )
    smtp_use_ssl: bool = Field(
        default=True,
        description="True → SSL-on-connect (port 465). False → STARTTLS (port 587).",
    )
    smtp_username: str = Field(
        default="", description="SMTP login (usually the full sender email address)"
    )
    smtp_password: str = Field(default="", description="SMTP password / app-specific password")
    smtp_from_address: str = Field(
        default="",
        description="``From:`` header email — usually the same as ``smtp_username``",
    )
    smtp_from_alias: str = Field(
        default="HKGAI Agent",
        description="Display name shown in the recipient's inbox; ≤30 chars recommended",
    )
    smtp_timeout_seconds: float = Field(
        default=30.0,
        description="Per-call socket timeout for connect / EHLO / DATA",
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
        default=100,
        description="Max attachments per single user message",
    )
    max_file_bytes: int = Field(
        default=200 * 1024 * 1024,
        description="Per-file upload size cap (bytes)",
    )
    max_session_files_bytes_per_user: int = Field(
        default=1000 * 1024 * 1024 * 1024,
        description=(
            "Per-user total-size cap (bytes) for UPLOAD-origin session files "
            "across all sessions. The primary upload gate — tracks OSS storage "
            "cost. Agent-produced DELIVERABLE / INTERNAL rows are not counted "
            "(they have no upload-time gate)."
        ),
    )
    max_session_files_per_user: int = Field(
        default=500000,
        description=(
            "Backstop cap on the COUNT of UPLOAD-origin session-file rows per "
            "user, guarding against many-tiny-files row explosion that the "
            "size cap alone misses. Same UPLOAD-only scope as the size cap."
        ),
    )

    # Sessions
    supported_session_model_ids: frozenset[str] = Field(
        default=frozenset(
            {
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
                "glm-5.2",
                "MiniMax/MiniMax-M2.7",
                "fable",
            }
        ),
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
            "STANDARD": "glm-5.2",
            "ADVANCED": "deepseek-v4-pro",
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
    #   - ``reserve_est_input_tokens``: realistic estimate of a turn's
    #     *cumulative* input tokens, used at reserve time (per-model p50
    #     from production data — an agent turn re-sends its growing
    #     context on every LLM call, so the cumulative figure is far
    #     above any single call's prompt). Sizes the reserve hold to
    #     ≈ the model's median turn cost: an entry gate + concurrency
    #     throttle, NOT a worst-case bound — mid-turn overruns are the
    #     budget enforcer's job (``app/workers/budget_enforcer.py``).
    #     Missing → falls back to ``credit_reserve_max_input_tokens``.
    # The per-second knob (β) is global, not per-model — see
    # ``credit_per_second_credits`` below.
    credit_rates: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "claude-opus-4-7": {
                "in_per_1k_usd": 0.005,
                "out_per_1k_usd": 0.025,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 4096,
                "reserve_est_input_tokens": 50000,
            },
            "fable": {
                "in_per_1k_usd": 0.005,
                "out_per_1k_usd": 0.025,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 4096,
                "reserve_est_input_tokens": 50000,
            },
            "claude-opus-4-6": {
                "in_per_1k_usd": 0.005,
                "out_per_1k_usd": 0.025,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 4096,
                "reserve_est_input_tokens": 50000,
            },
            "claude-sonnet-4-6": {
                "in_per_1k_usd": 0.003,
                "out_per_1k_usd": 0.015,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 250000,
            },
            "kimi-k2": {
                "in_per_1k_usd": 0.00055,
                "out_per_1k_usd": 0.0022,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 100000,
            },
            "kimi-k2-code": {
                "in_per_1k_usd": 0.00055,
                "out_per_1k_usd": 0.0022,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 225000,
            },
            "HKGAI-V1": {
                "in_per_1k_usd": 0.001,
                "out_per_1k_usd": 0.005,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 100000,
            },
            "HKGAI-V3": {
                "in_per_1k_usd": 0.001,
                "out_per_1k_usd": 0.005,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 100000,
            },
            # DashScope-hosted models. USD COGS derived from Aliyun 百炼 pricing pages
            # (CNY converted at ~1 USD = 7.2 CNY). Conservative: glm-5.1 uses the
            # <32K-context tier; longer turns nominally cost ~33% more on the wire
            # but the 3× ``token_markup`` more than absorbs that gap. qwen3.7-max
            # uses Qwen3.6-Max-Preview rates as proxy (3.7 preview pricing not yet
            # officially published, expected ≤3.6's). deepseek-v4-pro uses post-promo
            # standard rates ($1.74/$3.48 per M) to avoid charging below COGS once
            # the 2026-05-31 promo expires.
            "qwen3.7-max": {
                "in_per_1k_usd": 0.0013,
                "out_per_1k_usd": 0.0078,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 45000,
            },
            "deepseek-v4-pro": {
                "in_per_1k_usd": 0.00174,
                "out_per_1k_usd": 0.00348,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 225000,
            },
            # deepseek-v4-flash budget tier — permanent ¥1/¥2 per M
            # tokens (≈$0.14/$0.28 per M), no promo expiry. Cheaper than
            # v4-pro by ~10×; same DashScope endpoint and reasoning knobs.
            "deepseek-v4-flash": {
                "in_per_1k_usd": 0.00014,
                "out_per_1k_usd": 0.00028,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 2000,
            },
            "glm-5.1": {
                "in_per_1k_usd": 0.00083,
                "out_per_1k_usd": 0.0033,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 425000,
            },
            "glm-5.2": {
                "in_per_1k_usd": 0.00070,
                "out_per_1k_usd": 0.00080,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 425000,
            },
            # MiniMax M2.7 namespaced SKU on DashScope: ¥2.1/¥8.4 per M tokens
            # → $0.000292/$0.00117 per 1K (1 USD ≈ 7.2 CNY). Cached input
            # ¥0.42/M not separately tracked here; settle uses uncached rate.
            "MiniMax/MiniMax-M2.7": {
                "in_per_1k_usd": 0.000292,
                "out_per_1k_usd": 0.00117,
                "token_markup": 3.0,
                "model_fee_credits": 0,
                "reserve_max_output_tokens": 8192,
                "reserve_est_input_tokens": 60000,
            },
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
        default=0.01,
        ge=0.0,
        description="Credits per second of turn duration (β). 0 = disabled.",
    )
    # Pessimistic upper bound on ``turn_seconds`` used at reserve time
    # so the hold covers a fully-occupied worker. Matches the worker's
    # ``max_execution_time`` (1800s) + its hard timeout buffer (~60s).
    credit_reserve_max_turn_seconds: int = Field(
        default=1860,
        gt=0,
        description="Upper bound on turn_seconds assumed at reserve time. Settle uses actual.",
    )
    # Fallback input-token estimate at reserve time, for models whose
    # rate-card entry lacks ``reserve_est_input_tokens`` (the data-driven
    # per-model figure above). Real cost is settled from the worker's
    # actual ``usage.prompt_tokens`` post-turn.
    credit_reserve_max_input_tokens: int = Field(
        default=16_000,
        description="Fallback input-token estimate at reserve time for models without a per-model reserve_est_input_tokens. Settle uses actual usage.",
    )
    # Budget enforcer (``app/workers/budget_enforcer.py``) — the mid-turn
    # half of spend control. Prices each running turn's heartbeat-reported
    # interim usage and stops turns that would leave their user's projected
    # post-settle balance at/below the buffer. On by default now that it's
    # validated; set False to fall back to reserve-gate-only. Requires the
    # worker to be reporting interim usage (deploy it first). Fail-open —
    # disabled or crashed, the system degrades to reserve-gate-only semantics.
    budget_enforcer_enabled: bool = Field(
        default=True,
        description="Run the mid-turn budget enforcer in the background process.",
    )
    budget_enforcer_interval_seconds: int = Field(
        default=5,
        gt=0,
        description="Seconds between budget-enforcer ticks. With the worker's 20s heartbeat, worst-case overrun detection latency is roughly one heartbeat + one tick.",
    )
    budget_enforcer_buffer_credits: int = Field(
        default=80,
        ge=0,
        description="Stop turns once the user's projected post-settle balance falls to this many credits. 0 = stop at exactly zero, tolerating one detection window of overshoot (Manus-style).",
    )
    # Auto-grant at an account's first sign-in, in whole credits at the
    # current peg (1 credit ≈ $0.01 of cost basis). A generous "try the
    # product" allowance; every account behind it is one the identity
    # provider registered. Set to 0 to disable (every new user starts at
    # 0 balance, admin must top up). The grant uses a deterministic
    # idempotency key ``welcome-{user_id}`` so two racing first sign-ins
    # for one account grant once.
    credit_welcome_grant_credits: int = Field(
        default=1000,
        description="Whole credits granted to every new user at first sign-in. Set to 0 to disable.",
    )
    # Daily check-in: a user claims a fixed credit reward once per business
    # day. Set the reward to 0 to disable the feature (403 on claim). The
    # "day" is the calendar day in ``checkin_timezone`` — computed
    # server-side so an early-morning claim lands on the right day.
    checkin_reward_credits: int = Field(
        default=100,
        description="Whole credits granted per daily check-in. Set to 0 to disable check-in.",
    )
    checkin_timezone: str = Field(
        default="Asia/Shanghai",
        description="Business timezone whose calendar day defines one check-in per day.",
    )
    # RMB price of ONE credit, used only to convert an operator top-up
    # stated in ¥ (``scripts/topup_credits.py --cny``, through
    # ``rates.credits_from_cny``) into credits. Distinct from the internal
    # cost peg (1 credit ≈ $0.01 ≈ ¥0.072 of LLM COGS). Does
    # NOT touch billing / settle / balance — those stay on the µ-credit
    # ledger and the USD cost peg; this is pure sell-side pricing.
    credit_price_cny: float = Field(
        default=0.1,
        gt=0.0,
        description="Customer-facing RMB price of 1 credit at top-up. ¥0.1 → ¥100 buys 1000 credits.",
    )


settings = Settings()
