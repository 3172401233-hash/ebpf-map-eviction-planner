"""Data model for a single eBPF map entry.

All timestamps are expressed in arbitrary monotonic units (for example
nanoseconds since boot).  Only the relative differences matter.
"""

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class MapEntry:
    """One flow/connection record stored in the map.

    Attributes:
        key: Unique lookup key (typically a tuple of the flow 5-tuple).
        last_hit: Timestamp of the most recent lookup/update.
        expiry: Absolute timestamp at which the entry is considered dead.
        ref_count: Number of in-flight program references.  An entry with
            ``ref_count > 0`` must never be evicted.
        pinned: Explicitly pinned entries (e.g. long-lived control flows)
            are protected from eviction but still expire.
        cpu_distribution: Hit count per CPU, mirroring a per-CPU map.
        hits: Total hits, kept so hot/cold ranking survives a rebalanced
            per-CPU layout.
    """

    key: object
    last_hit: int
    expiry: int
    ref_count: int = 0
    pinned: bool = False
    cpu_distribution: Dict[int, int] = field(default_factory=dict)
    hits: int = 0

    def is_expired(self, now: int) -> bool:
        return now >= self.expiry

    def is_referenced(self) -> bool:
        return self.ref_count > 0

    @property
    def cpu_count(self) -> int:
        return len(self.cpu_distribution)

    def touch(self, now: int, cpu: int) -> None:
        """Record a lookup on ``cpu`` at timestamp ``now``."""
        self.last_hit = now
        self.hits += 1
        self.cpu_distribution[cpu] = self.cpu_distribution.get(cpu, 0) + 1

    def snapshot(self) -> "MapEntry":
        """Deep-ish copy used for rollback of a failed map update."""
        return MapEntry(
            key=self.key,
            last_hit=self.last_hit,
            expiry=self.expiry,
            ref_count=self.ref_count,
            pinned=self.pinned,
            cpu_distribution=dict(self.cpu_distribution),
            hits=self.hits,
        )
