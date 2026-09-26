"""Link pairing core for viosc (e42s01).

When pairing is enabled (the default, user decision 2026-09-13) a peer must
present the code shown at start before its OSC input — including the `/vimix`
forward — or its `:8686` HTTP requests are accepted. This module owns the pure,
testable part: the code generator. The daemon holds the live state (the code,
the bound-peer registry) and enforces it; nothing here imports the daemon, so it
is safe to import from tests and from either transport.

Honest scope: this is a *pairing* measure, not encryption — the code travels in
plaintext over UDP/HTTP, so it keeps casual and misconfigured peers out, not a
determined attacker. Rate limiting and code rotation (e42s03) are what make a
4-digit code impractical to guess.
"""

import hmac
import secrets
import time
from collections.abc import Callable

MIN_CODE_LENGTH = 1
MAX_CODE_LENGTH = 12
DEFAULT_CODE_LENGTH = 4
TOKEN_BYTES = 32
DEFAULT_TOKEN_TTL = 3600
DEFAULT_LOCK_AFTER = 5  # consecutive failures before a peer is locked
DEFAULT_LOCK_SECONDS = 60.0  # lock duration, and the global failure window
DEFAULT_GLOBAL_LOCK_AFTER = 20  # total failures in the window before regeneration


def generate_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    """A zero-padded random numeric pairing code of the requested length.

    Uniform over ``10**length`` values (``secrets``, not ``random``), so
    ``length=4`` yields 0000..9999. The length is clamped into
    ``[MIN_CODE_LENGTH, MAX_CODE_LENGTH]`` so a corrupt config cannot produce an
    empty or unbounded code.
    """
    try:
        length = int(length)
    except (TypeError, ValueError):
        length = DEFAULT_CODE_LENGTH
    length = max(MIN_CODE_LENGTH, min(MAX_CODE_LENGTH, length))
    return f"{secrets.randbelow(10**length):0{length}d}"


def persisted_code_if_valid(stored: object, length: int) -> str | None:
    """Reuse ``stored`` only when it is a numeric string of exactly ``length``.

    A persisted code that is empty, corrupt or no longer matches the configured
    length returns None so the caller generates and persists a fresh one. This
    is what makes a restart (os.execv) keep the same code instead of rotating it.
    """
    if not isinstance(stored, str) or not stored:
        return None
    try:
        length = int(length)
    except (TypeError, ValueError):
        length = DEFAULT_CODE_LENGTH
    length = max(MIN_CODE_LENGTH, min(MAX_CODE_LENGTH, length))
    if len(stored) == length and stored.isdigit():
        return stored
    return None


class PeerRegistry:
    """Who may talk to viOSC while pairing is on (e42s01).

    A peer is bound by a successful authentication and stays bound for
    ``lease_seconds``; a configured trusted peer is bound without presenting
    the code (the escape for third-party OSC clients). Time is injected so the
    lease/expiry logic is deterministic in tests and needs no sleeping.
    """

    def __init__(
        self,
        lease_seconds: float = 3600,
        trusted: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lease = max(0.0, float(lease_seconds))
        self._trusted = set(trusted or [])
        self._bound: dict[str, float] = {}
        self._clock = clock

    def bind(self, ip: str) -> None:
        """Bind ``ip`` until now + the lease (rebinding renews it)."""
        self._bound[ip] = self._clock() + self._lease

    def unbind(self, ip: str) -> None:
        """Drop a bound peer immediately (a trusted peer stays bound)."""
        self._bound.pop(ip, None)

    def is_bound(self, ip: str) -> bool:
        """True when ``ip`` is trusted or holds a live lease."""
        if ip in self._trusted:
            return True
        expiry = self._bound.get(ip)
        if expiry is None:
            return False
        if expiry <= self._clock():
            del self._bound[ip]
            return False
        return True

    def sweep(self) -> None:
        """Drop every expired lease (bounds the registry across churn)."""
        now = self._clock()
        expired = [ip for ip, expiry in self._bound.items() if expiry <= now]
        for ip in expired:
            del self._bound[ip]

    def bound_count(self) -> int:
        """Number of live (non-expired) leases; trusted peers excluded."""
        self.sweep()
        return len(self._bound)


def generate_token(nbytes: int = TOKEN_BYTES) -> str:
    """A random URL-safe bearer token (HTTP data-plane authentication)."""
    return secrets.token_urlsafe(nbytes)


class PairingGate:
    """The authenticator shared by the HTTP and OSC surfaces (e42s01/s03).

    ``authenticate`` is the HTTP path: it checks the code in constant time, mints
    a bearer token with a TTL and binds the peer IP. ``authenticate_peer`` is the
    OSC path: the same check and binding, no token (UDP has no session).
    ``verify`` validates a bearer token; ``is_bound`` decides whether a peer may
    use the OSC planes. With ``enabled`` False every check passes and nothing is
    required, so the pre-e42 behaviour is preserved exactly. Time is injected.

    Abuse resistance (e42s03): ``lock_after`` consecutive failures lock a peer
    for ``lock_seconds``; ``global_lock_after`` failures across all peers within
    ``lock_seconds`` regenerate the code and freeze authentication, so a 4-digit
    code cannot be brute-forced. ``on_regenerate`` lets the daemon re-display and
    log the new code.
    """

    def __init__(
        self,
        enabled: bool = True,
        code: str = "",
        registry: PeerRegistry | None = None,
        token_ttl: float = DEFAULT_TOKEN_TTL,
        clock: Callable[[], float] = time.monotonic,
        lock_after: int = DEFAULT_LOCK_AFTER,
        lock_seconds: float = DEFAULT_LOCK_SECONDS,
        global_lock_after: int = DEFAULT_GLOBAL_LOCK_AFTER,
        on_regenerate: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._code = str(code)
        self._clock = clock
        self._registry = registry if registry is not None else PeerRegistry(clock=clock)
        self._ttl = max(0.0, float(token_ttl))
        self._tokens: dict[str, float] = {}
        self._lock_after = max(1, int(lock_after))
        self._lock_seconds = max(0.0, float(lock_seconds))
        self._global_lock_after = max(1, int(global_lock_after))
        self._on_regenerate = on_regenerate
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._global_failures: list[float] = []
        self._frozen_until = 0.0

    @property
    def code(self) -> str:
        """The current code (it changes when the global threshold regenerates it)."""
        return self._code

    def set_code(self, code: str) -> None:
        """Replace the code (a manual rotation from the daemon, e58s01)."""
        self._code = str(code)

    def is_locked(self, ip: str) -> bool:
        """True while ``ip`` is locked out after too many failed attempts."""
        until = self._locked_until.get(ip)
        if until is None:
            return False
        if until <= self._clock():
            del self._locked_until[ip]
            return False
        return True

    def is_frozen(self) -> bool:
        """True while the global freeze after a regeneration is in effect."""
        return self._frozen_until > self._clock()

    def _code_matches(self, code: object) -> bool:
        return hmac.compare_digest(str(code).encode(), self._code.encode())

    def _note_failure(self, ip: str) -> None:
        """Count a failed attempt; lock the peer and/or regenerate the code."""
        now = self._clock()
        self._failures[ip] = self._failures.get(ip, 0) + 1
        if self._failures[ip] >= self._lock_after:
            self._failures[ip] = 0
            self._locked_until[ip] = now + self._lock_seconds
        self._global_failures.append(now)
        window = now - self._lock_seconds
        self._global_failures = [t for t in self._global_failures if t > window]
        if len(self._global_failures) >= self._global_lock_after:
            self._global_failures.clear()
            self._frozen_until = now + self._lock_seconds
            self._code = generate_code(len(self._code) or DEFAULT_CODE_LENGTH)
            if self._on_regenerate is not None:
                self._on_regenerate(self._code)

    def authenticate(self, code: object, ip: str) -> str | None:
        """HTTP auth: a bearer token on the right code, else None (binds ``ip``)."""
        if not self.enabled:
            self._registry.bind(ip)
            return ""
        if self.is_locked(ip) or self.is_frozen():
            return None
        if not self._code_matches(code):
            self._note_failure(ip)
            return None
        self._failures.pop(ip, None)
        token = generate_token()
        self._tokens[token] = self._clock() + self._ttl
        self._registry.bind(ip)
        return token

    def authenticate_peer(self, code: object, ip: str) -> bool:
        """OSC auth: bind ``ip`` when the code is right, no token returned."""
        if not self.enabled:
            self._registry.bind(ip)
            return True
        if self.is_locked(ip) or self.is_frozen():
            return False
        if not self._code_matches(code):
            self._note_failure(ip)
            return False
        self._failures.pop(ip, None)
        self._registry.bind(ip)
        return True

    def verify(self, token: object) -> bool:
        """True when ``token`` holds a live lease (or pairing is off)."""
        if not self.enabled:
            return True
        if not token:
            return False
        key = str(token)
        expiry = self._tokens.get(key)
        if expiry is None:
            return False
        if expiry <= self._clock():
            del self._tokens[key]
            return False
        return True

    def is_bound(self, ip: str) -> bool:
        """True when ``ip`` may use the OSC planes (or pairing is off)."""
        if not self.enabled:
            return True
        return self._registry.is_bound(ip)
