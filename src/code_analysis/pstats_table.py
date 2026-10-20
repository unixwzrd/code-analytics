"""Native pstats table annotations missing from the stdlib type interface.

CPython creates ``Stats.stats`` in ``init``. This declares its native layout;
imported artifacts must still pass the existing record validators. Caller
payloads remain objects until their timed or count-only representation validates.
"""

from __future__ import annotations

import pstats

type ProfileKey = tuple[str, int, str]
type ProfileRow = tuple[int, int, float, float, dict[ProfileKey, object]]


class ProfileStats(pstats.Stats):
    stats: dict[ProfileKey, ProfileRow]
