"""In-memory simulation of an eBPF map with per-CPU value slots.

The class deliberately mirrors the failure modes a real userspace
updater has to handle:

* ``E2BIG`` / map full on insert;
* a partial per-CPU ``bpf_map_update_elem`` batch where some CPUs
  accept the new value and a later CPU rejects it.

On any partial failure the already-written CPU slots are restored to
their previous values so every per-CPU replica stays consistent.
"""

from typing import Dict, List, Optional

from .model import MapEntry


class UpdateConflict(RuntimeError):
    """Raised when an update cannot be applied to the simulated map."""


_NO_FAULT = object()


class SimulatedMap:
    def __init__(self, capacity: int, cpu_ids: List[int]):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if not cpu_ids:
            raise ValueError("at least one CPU is required")
        self.capacity = capacity
        self.cpu_ids = list(cpu_ids)
        # key -> {cpu_id -> MapEntry replica}
        self._store = {}  # type: Dict[object, Dict[int, MapEntry]]
        # Optional fault injection: map key -> CPU id whose update fails.
        self.fault_on_cpu = {}  # type: Dict[object, int]
        self.update_calls = 0

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def keys(self):
        return list(self._store.keys())

    def is_full(self) -> bool:
        return len(self._store) >= self.capacity

    def get_replica(self, key: object, cpu: int) -> Optional[MapEntry]:
        replicas = self._store.get(key)
        if replicas is None:
            return None
        return replicas.get(cpu)

    def _fail_at(self, key: object, cpu: int) -> bool:
        return self.fault_on_cpu.get(key, _NO_FAULT) == cpu

    def update(self, key: object, value: MapEntry,
               expect_existing: Optional[bool] = None) -> None:
        """Atomically write ``value`` to every per-CPU replica.

        ``expect_existing`` mirrors BPF_ANY / BPF_NOEXIST / BPF_EXIST:
        ``True`` requires the key to exist, ``False`` requires it to be
        absent and ``None`` allows either.  A full map rejects a brand
        new key before any replica is touched.
        """
        self.update_calls += 1
        existing = key in self._store
        if expect_existing is True and not existing:
            raise UpdateConflict("key %r does not exist" % (key,))
        if expect_existing is False:
            if existing:
                raise UpdateConflict("key %r already exists" % (key,))
            if self.is_full():
                raise UpdateConflict("map is full (capacity=%d)" % self.capacity)
        if expect_existing is None and not existing and self.is_full():
            raise UpdateConflict("map is full (capacity=%d)" % self.capacity)

        old_replicas = self._store.get(key)
        written = {}  # type: Dict[int, MapEntry]
        for cpu in self.cpu_ids:
            if self._fail_at(key, cpu):
                # Roll back every replica already written in this batch.
                if old_replicas is None:
                    self._store.pop(key, None)
                else:
                    self._store[key] = old_replicas
                raise UpdateConflict(
                    "per-CPU update for %r failed on cpu %d" % (key, cpu))
            written[cpu] = value.snapshot()
        self._store[key] = written

    def delete(self, key: object) -> None:
        if key not in self._store:
            raise UpdateConflict("cannot delete missing key %r" % (key,))
        # All per-CPU replicas are removed together.
        del self._store[key]

    def consistent(self) -> bool:
        """Every present key must have a replica on each configured CPU."""
        for replicas in self._store.values():
            if set(replicas) != set(self.cpu_ids):
                return False
        return True
