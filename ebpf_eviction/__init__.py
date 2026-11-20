"""Pure-Python planning layer for eBPF map eviction.

This package never touches a real BPF syscall.  It only simulates the
decisions a userspace control plane would make when an eBPF map is close
to its capacity: insert / update / evict / reject, with hot-key
protection, reference-count pinning and per-CPU atomic semantics.
"""

from .model import MapEntry
from .simulated_map import SimulatedMap, UpdateConflict
from .planner import EvictionPlanner, PlannerConfig, Decision, ProtectionEvidence

__all__ = [
    "MapEntry",
    "SimulatedMap",
    "UpdateConflict",
    "EvictionPlanner",
    "PlannerConfig",
    "Decision",
    "ProtectionEvidence",
]
