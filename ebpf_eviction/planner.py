"""Eviction planning engine.

The planner combines age and heat into one score instead of choosing a
pure LRU or LFU policy:

* pure LRU evicts a cold-but-long-lived connection that is briefly
  idle and can destroy connection state;
* pure LFU starves new flows because well-warmed keys keep winning.

A higher :meth:`EvictionPlanner.eviction_score` means "cheaper to
remove".  Entries above the hot threshold are protected, so genuinely
hot keys survive while cold old entries are recycled first; new flows
still get room once the cold tail is exhausted.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import MapEntry
from .simulated_map import SimulatedMap, UpdateConflict


@dataclass
class ProtectionEvidence:
    """Why an entry was *not* chosen as the eviction victim."""

    key: object
    reason: str          # "referenced" | "pinned" | "hot"
    detail: str


@dataclass
class Decision:
    """One planned action plus its observable result."""

    action: str          # "insert" | "update" | "evict" | "reject"
    key: object
    accepted: bool
    reason: str
    rolled_back: bool = False
    score: Optional[float] = None
    protections: List[ProtectionEvidence] = field(default_factory=list)


@dataclass
class PlannerConfig:
    hot_hit_threshold: int = 100
    age_weight: float = 1.0
    heat_weight: float = 1.0


class EvictionPlanner:
    def __init__(self, simulated_map: SimulatedMap,
                 config: Optional[PlannerConfig] = None):
        self.map = simulated_map
        self.config = config or PlannerConfig()
        # Authoritative entry state keyed by map key.
        self.entries = {}  # type: Dict[object, MapEntry]
        self.decisions = []  # type: List[Decision]
        self.protection_log = []  # type: List[ProtectionEvidence]

    # ------------------------------------------------------------------
    # Bookkeeping helpers
    # ------------------------------------------------------------------
    def add_entry(self, entry: MapEntry) -> None:
        """Register an entry that already lives in the map."""
        self.entries[entry.key] = entry

    def build_entry(self, key: Any, last_hit: int, expiry: int,
                    ref_count: int = 0, pinned: bool = False,
                    cpu_distribution: Optional[Dict[int, int]] = None,
                    hits: Optional[int] = None) -> MapEntry:
        if hits is None:
            hits = sum((cpu_distribution or {}).values())
        return MapEntry(
            key=key,
            last_hit=last_hit,
            expiry=expiry,
            ref_count=ref_count,
            pinned=pinned,
            cpu_distribution=dict(cpu_distribution or {}),
            hits=hits,
        )

    def touch(self, key: object, now: int, cpu: int) -> None:
        """Record a data-path hit on an existing entry."""
        entry = self.entries[key]
        entry.touch(now, cpu)

    def eviction_score(self, entry: MapEntry, now: int) -> float:
        """Higher score == cheaper to evict.

        Idle time (age) pushes the score up; accumulated heat pulls it
        down.  Both terms are normalised against the current candidate
        set so the ranking stays unit-independent.
        """
        candidates = [e for e in self.entries.values() if not e.is_expired(now)]
        if not candidates:
            return 0.0
        max_age = max(1, max(now - e.last_hit for e in candidates))
        max_hits = max(1, max(e.hits for e in candidates))
        age = (now - entry.last_hit) / max_age
        heat = entry.hits / max_hits
        return (self.config.age_weight * age
                - self.config.heat_weight * heat)

    def _protection(self, entry: MapEntry, now: int) -> Optional[ProtectionEvidence]:
        if entry.is_referenced():
            return ProtectionEvidence(
                entry.key, "referenced",
                "ref_count=%d > 0, program holds the entry" % entry.ref_count)
        if entry.pinned:
            return ProtectionEvidence(
                entry.key, "pinned", "entry is explicitly pinned")
        if entry.hits >= self.config.hot_hit_threshold:
            return ProtectionEvidence(
                entry.key, "hot",
                "hits=%d >= hot threshold %d"
                % (entry.hits, self.config.hot_hit_threshold))
        return None

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------
    def sweep_expired(self, now: int) -> List[Decision]:
        """Remove every entry whose expiry time has passed.

        Referenced entries are still skipped: a program currently using
        an entry keeps it alive even if its nominal TTL elapsed; the
        entry gets another sweep once its reference is released.
        """
        decisions = []
        for key in list(self.entries):
            entry = self.entries[key]
            if not entry.is_expired(now):
                continue
            evidence = self._protection(entry, now)
            if evidence is not None and evidence.reason == "referenced":
                decision = Decision(
                    "reject", key, False,
                    "expired but still referenced; retained",
                    protections=[evidence])
                self.protection_log.append(evidence)
            else:
                self.map.delete(key)
                del self.entries[key]
                decision = Decision(
                    "evict", key, True,
                    "expired at t=%d" % entry.expiry)
            self.decisions.append(decision)
            decisions.append(decision)
        return decisions

    # ------------------------------------------------------------------
    # Victim selection and insertion
    # ------------------------------------------------------------------
    def _select_victim(self, now: int):
        """Return (key, score, protections observed) or ``None``."""
        protections = []
        best = None  # (score, key)
        for entry in self.entries.values():
            evidence = self._protection(entry, now)
            if evidence is not None:
                protections.append(evidence)
                continue
            score = self.eviction_score(entry, now)
            if best is None or score > best[0]:
                best = (score, entry.key)
        if best is None:
            return None, protections
        return best, protections

    def insert(self, entry: MapEntry, now: int) -> Decision:
        """Plan and execute an insert, evicting one victim if needed."""
        key = entry.key
        if key in self.entries:
            return self.update(entry, now)

        self.sweep_expired(now)

        if not self.map.is_full():
            return self._do_insert(entry, now)

        # Map full: find a protected-free victim.
        victim, protections = self._select_victim(now)
        self.protection_log.extend(protections)
        if victim is None:
            decision = Decision(
                "reject", key, False,
                "map full and every live entry is protected "
                "(referenced / pinned / hot)",
                protections=list(protections))
            self.decisions.append(decision)
            return decision

        victim_score, victim_key = victim
        victim_entry = self.entries[victim_key]
        self.map.delete(victim_key)
        del self.entries[victim_key]
        evict_decision = Decision(
            "evict", victim_key, True,
            "freed slot for new key %r" % (key,),
            score=victim_score,
            protections=list(protections))
        self.decisions.append(evict_decision)

        insert_decision = self._do_insert(entry, now)
        insert_decision.protections = list(protections)
        return insert_decision

    def _do_insert(self, entry: MapEntry, now: int) -> Decision:
        try:
            self.map.update(entry.key, entry, expect_existing=False)
        except UpdateConflict as exc:
            decision = Decision("insert", entry.key, False, str(exc))
            self.decisions.append(decision)
            return decision
        self.entries[entry.key] = entry
        decision = Decision(
            "insert", entry.key, True,
            "stored at t=%d, map usage %d/%d"
            % (now, len(self.entries), self.map.capacity))
        self.decisions.append(decision)
        return decision

    # ------------------------------------------------------------------
    # Update with rollback
    # ------------------------------------------------------------------
    def update(self, entry: MapEntry, now: int) -> Decision:
        """Overwrite an existing key, restoring the old value on failure."""
        key = entry.key
        if key not in self.entries:
            decision = Decision(
                "update", key, False,
                "cannot update missing key; use insert")
            self.decisions.append(decision)
            return decision

        old_entry = self.entries[key].snapshot()
        try:
            self.map.update(key, entry, expect_existing=True)
        except UpdateConflict as exc:
            # SimulatedMap already restored every per-CPU replica;
            # restore the planner-side authoritative state too.
            self.entries[key] = old_entry
            decision = Decision(
                "update", key, False,
                "conflict: %s; previous value restored on all CPUs" % exc,
                rolled_back=True)
            self.decisions.append(decision)
            return decision

        self.entries[key] = entry
        decision = Decision(
            "update", key, True,
            "all %d per-CPU replicas replaced at t=%d"
            % (len(self.map.cpu_ids), now))
        self.decisions.append(decision)
        return decision

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def last_round(self) -> List[Decision]:
        return list(self.decisions)

    def hot_protection_evidence(self) -> List[ProtectionEvidence]:
        """Every protection decision observed, for audit/test assertions."""
        return list(self.protection_log)
