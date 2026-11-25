"""Which IANA zone names this deployment can compute with."""

from zoneinfo import available_timezones

# Two entries the tz tree carries that are not zones. ``localtime`` is a
# symlink to whatever this machine happens to be set to — host-relative,
# and so the exact opposite of the snapshot a stored zone exists to be.
# ``Factory`` is a placeholder whose content is the string "no timezone
# has been set". Python already filters ``posixrules``.
_NOT_A_ZONE = frozenset({"localtime", "Factory"})

# Built once at import. ``available_timezones()`` rebuilds its set on every
# call and may walk the filesystem to do it — several milliseconds, on a
# path that runs for every message, every schedule create and every
# recurrence edit, and PATCH carries no rate limit of its own.
_KNOWN_ZONES = frozenset(available_timezones()) - _NOT_A_ZONE


def is_known_timezone(value: str) -> bool:
    """Whether this deployment's tz database carries ``value`` as a zone.

    Asked of a zone a client sends, and again of one read back out of the
    database, where no validator has run — a row can outlive the tz data it
    was written against.
    """
    return value in _KNOWN_ZONES
