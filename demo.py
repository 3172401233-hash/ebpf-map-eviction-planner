"""End-to-end demonstration of the eviction planner.

Run with::

    python demo.py

No eBPF subsystem, BPF syscall or elevated privilege is involved:
the map and its per-CPU slots are plain Python objects.
"""

from ebpf_eviction import EvictionPlanner, PlannerConfig, SimulatedMap


def show(title, decisions):
    print("--- %s ---" % title)
    for decision in decisions:
        line = "[%s] key=%r accepted=%s reason=%s" % (
            decision.action.upper(), decision.key,
            decision.accepted, decision.reason)
        if decision.score is not None:
            line += " score=%.3f" % decision.score
        print(line)
        for evidence in decision.protections:
            print("    protection: %s -> %s (%s)"
                  % (evidence.key, evidence.reason, evidence.detail))
    print()


def main():
    cpus = [0, 1, 2, 3]
    smap = SimulatedMap(capacity=3, cpu_ids=cpus)
    planner = EvictionPlanner(smap, PlannerConfig(hot_hit_threshold=100))

    # Round 1: seed one cold flow, one hot flow, one referenced flow.
    for key, last_hit, hits, ref_count in (
            ("cold-flow", 1, 3, 0),
            ("hot-flow", 80, 500, 0),
            ("pinned-conn", 90, 20, 1)):
        entry = planner.build_entry(
            key=key, last_hit=last_hit, expiry=1000,
            cpu_distribution={0: hits}, hits=hits, ref_count=ref_count)
        planner.map.update(key, entry, expect_existing=False)
        planner.add_entry(entry)
    planner.entries["pinned-conn"].pinned = True
    show("seed: map full (3/3)", planner.last_round())

    # Round 2: a new flow arrives; the cold, low-hit flow is recycled
    # while the hot flow and the referenced connection are protected.
    new_flow = planner.build_entry("new-flow", 100, 1100, hits=0)
    show("insert new flow", [planner.insert(new_flow, now=100)])

    # Round 3: the new flow had a short TTL; the sweep reclaims it.
    planner.entries["new-flow"].expiry = 105
    show("expiry sweep at t=110", planner.sweep_expired(now=110))

    # Refill the freed slot with another hot flow so the map is full
    # again and every survivor is protected.
    extra = planner.build_entry(
        "hot-flow-2", last_hit=115, expiry=2000,
        cpu_distribution={0: 300}, hits=300)
    planner.map.update(extra.key, extra, expect_existing=False)
    planner.add_entry(extra)

    # Round 4: inject a per-CPU write fault and prove rollback: every
    # replica keeps the previous value.
    smap.fault_on_cpu["hot-flow"] = 2
    updated = planner.build_entry(
        "hot-flow", last_hit=120, expiry=2000,
        cpu_distribution={0: 501}, hits=501)
    show("update with injected cpu-2 fault",
         [planner.update(updated, now=120)])

    # Round 5: every survivor is protected (hot or pinned), so the new
    # flow cannot be admitted even though eviction was attempted.
    blocked = planner.insert(
        planner.build_entry("blocked-flow", 120, 1200), now=120)
    show("insert while all entries protected", [blocked])

    print("map consistent across all per-CPU replicas:", smap.consistent())


if __name__ == "__main__":
    main()
