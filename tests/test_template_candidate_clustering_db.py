"""db/database.py's clustering layer on top of template_candidates —
list_unclustered_template_candidates/list_template_candidate_cluster_labels/
create_template_candidate_cluster/assign_template_candidate_cluster/
list_template_candidate_clusters_with_stats
(docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §3-4). Same real-DB-isolation
posture as test_template_candidates_db.py — not addressed by this change.
"""
from __future__ import annotations

import unittest

from db.database import (
    add_template_candidate,
    assign_template_candidate_cluster,
    create_template_candidate_cluster,
    init_db,
    list_template_candidate_cluster_labels,
    list_template_candidate_clusters_with_stats,
    list_template_candidates,
    list_unclustered_template_candidates,
)


class TemplateCandidateClusteringDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

    async def test_new_candidate_is_unclustered_until_assigned(self):
        await add_template_candidate(
            creator_user_id=555,
            summary="голосовой учёт расходов для шиномонтажа — unique marker A",
            fallback_reason="no_template_match",
            selected_templates=[],
            bot_type="general",
        )
        unclustered = await list_unclustered_template_candidates()
        row = next(r for r in unclustered if r["summary"].endswith("unique marker A"))
        self.assertIsNone(row.get("cluster_id"))

        cluster_id = await create_template_candidate_cluster(
            "голосовой учёт расходов", "Клиент хочет надиктовывать траты голосом."
        )
        await assign_template_candidate_cluster(row["id"], cluster_id)

        unclustered_after = await list_unclustered_template_candidates()
        self.assertFalse(any(r["id"] == row["id"] for r in unclustered_after))

    async def test_cluster_labels_round_trip(self):
        cluster_id = await create_template_candidate_cluster(
            "unique-label-marker-B", "тестовое описание кластера"
        )
        labels = await list_template_candidate_cluster_labels()
        row = next(c for c in labels if c["id"] == cluster_id)
        self.assertEqual(row["label"], "unique-label-marker-B")
        self.assertEqual(row["description"], "тестовое описание кластера")

    async def test_clusters_with_stats_counts_members_and_excludes_unclustered(self):
        cluster_id = await create_template_candidate_cluster("unique-label-marker-C", "desc")
        await add_template_candidate(
            creator_user_id=555,
            summary="первый в кластере C — unique marker C1",
            fallback_reason="no_template_match",
            selected_templates=[],
        )
        rows = await list_template_candidates()
        candidate_id = next(r for r in rows if r["summary"].endswith("unique marker C1"))["id"]
        await assign_template_candidate_cluster(candidate_id, cluster_id)

        # Second candidate stays unassigned — must NOT show up in this cluster's stats.
        await add_template_candidate(
            creator_user_id=555,
            summary="не относится к кластеру C — unique marker C2",
            fallback_reason="no_template_match",
            selected_templates=[],
        )

        stats = await list_template_candidate_clusters_with_stats()
        cluster_row = next(c for c in stats if c["id"] == cluster_id)
        self.assertEqual(cluster_row["count"], 1)
        self.assertIn("первый в кластере C — unique marker C1", cluster_row["examples"])
        self.assertNotIn("не относится к кластеру C — unique marker C2", cluster_row["examples"])

    async def test_clusters_with_stats_sorted_largest_first(self):
        small_id = await create_template_candidate_cluster("small-cluster-marker-D", "desc")
        big_id = await create_template_candidate_cluster("big-cluster-marker-D", "desc")
        for i, cid in enumerate([small_id, big_id, big_id]):
            await add_template_candidate(
                creator_user_id=555,
                summary=f"marker-D candidate {i}",
                fallback_reason="no_template_match",
                selected_templates=[],
            )
            rows = await list_template_candidates()
            candidate_id = next(r for r in rows if r["summary"] == f"marker-D candidate {i}")["id"]
            await assign_template_candidate_cluster(candidate_id, cid)

        stats = await list_template_candidate_clusters_with_stats()
        big_index = next(i for i, c in enumerate(stats) if c["id"] == big_id)
        small_index = next(i for i, c in enumerate(stats) if c["id"] == small_id)
        self.assertLess(big_index, small_index)


if __name__ == "__main__":
    unittest.main()
