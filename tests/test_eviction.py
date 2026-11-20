import unittest

from ebpf_eviction import (
    EvictionPlanner, PlannerConfig, SimulatedMap, UpdateConflict,
)


CPUS = [0, 1, 2, 3]


def make_planner(capacity=3, config=None):
    smap = SimulatedMap(capacity=capacity, cpu_ids=CPUS)
    planner = EvictionPlanner(smap, config)
    return planner, smap


def seed(planner, key, last_hit, expiry, hits=0, ref_count=0, pinned=False):
    entry = planner.build_entry(
        key=key, last_hit=last_hit, expiry=expiry,
        cpu_distribution={0: hits}, hits=hits,
        ref_count=ref_count, pinned=pinned)
    planner.map.update(key, entry, expect_existing=False)
    planner.add_entry(entry)
    return entry


class ExpiryTests(unittest.TestCase):
    def test_expired_entries_are_swept(self):
        planner, smap = make_planner()
        seed(planner, "flow-a", last_hit=0, expiry=10)
        seed(planner, "flow-b", last_hit=0, expiry=20)

        decisions = planner.sweep_expired(now=15)

        self.assertEqual([d.action for d in decisions], ["evict"])
        self.assertEqual(decisions[0].key, "flow-a")
        self.assertNotIn("flow-a", smap)
        self.assertIn("flow-b", smap)
        self.assertTrue(smap.consistent())

    def test_expired_but_referenced_entry_survives(self):
        planner, smap = make_planner()
        seed(planner, "flow-a", last_hit=0, expiry=10, ref_count=2)

        decisions = planner.sweep_expired(now=99)

        self.assertEqual(decisions[0].action, "reject")
        self.assertFalse(decisions[0].accepted)
        self.assertIn("flow-a", smap)
        evidence = planner.hot_protection_evidence()
        self.assertEqual(evidence[0].reason, "referenced")


class HotColdCompetitionTests(unittest.TestCase):
    def test_cold_old_entry_evicted_before_hot_one(self):
        config = PlannerConfig(hot_hit_threshold=100)
        planner, smap = make_planner(capacity=2, config=config)
        # Old, cold entry: idle for a long time, few hits.
        seed(planner, "cold", last_hit=1, expiry=1000, hits=2)
        # Recent, hot entry.
        seed(planner, "hot", last_hit=90, expiry=1000, hits=500)

        new_flow = planner.build_entry(
            key="new", last_hit=100, expiry=1100, hits=0)
        decision = planner.insert(new_flow, now=100)

        self.assertEqual(decision.action, "insert")
        self.assertTrue(decision.accepted)
        self.assertNotIn("cold", smap)
        self.assertIn("hot", smap)
        evictions = [d for d in planner.decisions if d.action == "evict"]
        self.assertEqual(evictions[0].key, "cold")
        # Hot key produced explicit protection evidence.
        reasons = {e.key: e.reason
                   for e in planner.hot_protection_evidence()}
        self.assertEqual(reasons.get("hot"), "hot")

    def test_new_flow_does_not_starve_when_cold_tail_exists(self):
        # A fresh low-hit key must still be insertable: protection only
        # covers genuinely hot / pinned / referenced entries.
        config = PlannerConfig(hot_hit_threshold=100)
        planner, _ = make_planner(capacity=2, config=config)
        seed(planner, "old-cold-1", last_hit=1, expiry=1000, hits=5)
        seed(planner, "old-cold-2", last_hit=2, expiry=1000, hits=6)

        decision = planner.insert(
            planner.build_entry("new", 100, 1100, hits=0), now=100)
        self.assertTrue(decision.accepted)

    def test_all_protected_means_reject(self):
        config = PlannerConfig(hot_hit_threshold=10)
        planner, smap = make_planner(capacity=2, config=config)
        seed(planner, "hot-1", last_hit=90, expiry=1000, hits=100)
        seed(planner, "pinned", last_hit=90, expiry=1000, pinned=True)

        decision = planner.insert(
            planner.build_entry("new", 100, 1100), now=100)

        self.assertEqual(decision.action, "reject")
        self.assertFalse(decision.accepted)
        self.assertNotIn("new", smap)
        self.assertEqual(len(smap), 2)
        protected = {e.key for e in decision.protections}
        self.assertEqual(protected, {"hot-1", "pinned"})


class ReferenceTests(unittest.TestCase):
    def test_referenced_entry_blocks_eviction(self):
        planner, smap = make_planner(capacity=1)
        seed(planner, "conn", last_hit=0, expiry=1000, hits=0, ref_count=1)

        decision = planner.insert(
            planner.build_entry("new", 50, 1100), now=50)

        self.assertEqual(decision.action, "reject")
        self.assertIn("conn", smap)
        self.assertTrue(
            any(e.reason == "referenced"
                for e in planner.hot_protection_evidence()))

    def test_entry_evictable_after_reference_released(self):
        planner, smap = make_planner(capacity=1)
        entry = seed(planner, "conn", last_hit=0, expiry=1000, ref_count=1)

        blocked = planner.insert(
            planner.build_entry("new", 50, 1100), now=50)
        self.assertEqual(blocked.action, "reject")

        entry.ref_count = 0
        accepted = planner.insert(
            planner.build_entry("new", 51, 1100), now=51)
        self.assertTrue(accepted.accepted)
        self.assertNotIn("conn", smap)


class UpdateConflictTests(unittest.TestCase):
    def test_failed_per_cpu_update_restores_old_value(self):
        planner, smap = make_planner(capacity=2)
        old = seed(planner, "flow", last_hit=10, expiry=100, hits=4)
        # Make the second CPU reject the next write for this key.
        smap.fault_on_cpu["flow"] = 1

        updated = planner.build_entry(
            key="flow", last_hit=20, expiry=200,
            cpu_distribution={0: 99}, hits=99)
        decision = planner.update(updated, now=20)

        self.assertFalse(decision.accepted)
        self.assertTrue(decision.rolled_back)
        # Planner state is the old value again (restored from snapshot).
        self.assertEqual(planner.entries["flow"], old)
        self.assertEqual(planner.entries["flow"].hits, 4)
        # Every per-CPU replica is consistent and holds the old value.
        self.assertTrue(smap.consistent())
        for cpu in CPUS:
            self.assertEqual(smap.get_replica("flow", cpu).hits, 4)

    def test_update_missing_key_is_rejected(self):
        planner, _ = make_planner()
        decision = planner.update(
            planner.build_entry("ghost", 1, 10), now=1)
        self.assertEqual(decision.action, "update")
        self.assertFalse(decision.accepted)

    def test_successful_update_touches_all_replicas(self):
        planner, smap = make_planner()
        seed(planner, "flow", last_hit=10, expiry=100)
        updated = planner.build_entry("flow", last_hit=30, expiry=300, hits=7)

        decision = planner.update(updated, now=30)

        self.assertTrue(decision.accepted)
        self.assertTrue(smap.consistent())
        for cpu in CPUS:
            replica = smap.get_replica("flow", cpu)
            self.assertEqual(replica.expiry, 300)
            self.assertEqual(replica.hits, 7)

    def test_insert_into_full_map_raises_without_planner_eviction(self):
        smap = SimulatedMap(capacity=1, cpu_ids=CPUS)
        with self.assertRaises(UpdateConflict):
            smap.update("a", _RawEntry("a"), expect_existing=False)
            smap.update("b", _RawEntry("b"), expect_existing=False)


class _RawEntry:
    """Minimal stand-in exercising SimulatedMap without the planner."""

    def __init__(self, key):
        self.key = key

    def snapshot(self):
        return _RawEntry(self.key)


if __name__ == "__main__":
    unittest.main()
