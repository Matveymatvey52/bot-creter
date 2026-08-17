"""Template-candidate clustering — parser degrade-on-malformed-JSON behavior
(services/claude_service.py._parse_template_candidate_cluster_assignments)
and the incremental sweep logic
(runtime/template_candidate_clustering.run_template_candidate_clustering_pass),
with the Haiku classification call mocked out
(docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §3)."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import services.claude_service as claude_service
import runtime.template_candidate_clustering as clustering
from db.database import (
    add_template_candidate,
    init_db,
    list_template_candidates,
    list_unclustered_template_candidates,
)


class ClusterAssignmentParsingTests(unittest.TestCase):
    def test_parses_existing_cluster_assignment(self):
        raw = '{"assignments": [{"index": 0, "existing_cluster_id": 3, "new_label": null, "new_description": null}]}'
        result = claude_service._parse_template_candidate_cluster_assignments(raw, batch_size=1)
        self.assertEqual(
            result, [{"index": 0, "existing_cluster_id": 3, "new_label": None, "new_description": None}]
        )

    def test_parses_new_cluster_assignment(self):
        raw = (
            '{"assignments": [{"index": 0, "existing_cluster_id": null, '
            '"new_label": "voice-cashflow", "new_description": "desc"}]}'
        )
        result = claude_service._parse_template_candidate_cluster_assignments(raw, batch_size=1)
        self.assertEqual(result[0]["new_label"], "voice-cashflow")
        self.assertIsNone(result[0]["existing_cluster_id"])

    def test_malformed_json_degrades_to_empty_list(self):
        result = claude_service._parse_template_candidate_cluster_assignments("not json", batch_size=5)
        self.assertEqual(result, [])

    def test_out_of_range_index_is_dropped(self):
        raw = '{"assignments": [{"index": 99, "existing_cluster_id": 1, "new_label": null, "new_description": null}]}'
        result = claude_service._parse_template_candidate_cluster_assignments(raw, batch_size=1)
        self.assertEqual(result, [])

    def test_assignment_with_neither_shape_is_dropped(self):
        raw = '{"assignments": [{"index": 0, "existing_cluster_id": null, "new_label": null, "new_description": null}]}'
        result = claude_service._parse_template_candidate_cluster_assignments(raw, batch_size=1)
        self.assertEqual(result, [])

    def test_strips_markdown_code_fences(self):
        raw = (
            '```json\n{"assignments": [{"index": 0, "existing_cluster_id": 7, '
            '"new_label": null, "new_description": null}]}\n```'
        )
        result = claude_service._parse_template_candidate_cluster_assignments(raw, batch_size=1)
        self.assertEqual(result[0]["existing_cluster_id"], 7)


class ClusteringPassTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

    async def test_pass_assigns_new_candidate_to_new_cluster(self):
        # The shared real DB (see backlog memory on test DB isolation) may
        # already carry unclustered rows from other tests/sessions, so this
        # candidate isn't guaranteed to land at batch index 0 — the fake
        # classify_template_candidate_clusters below must answer for
        # WHATEVER batch it is actually given, not assume a fixed shape.
        await add_template_candidate(
            creator_user_id=555,
            summary="голосовой учёт расходов для клининга — pass-test-marker-1",
            fallback_reason="no_template_match",
            selected_templates=[],
            bot_type="general",
        )
        rows = await list_template_candidates()
        candidate_id = next(r for r in rows if r["summary"].endswith("pass-test-marker-1"))["id"]

        async def fake_classify(known_clusters, batch):
            return [
                {
                    "index": i,
                    "existing_cluster_id": None,
                    "new_label": f"pass-test-cluster-{i}-{id(item)}",
                    "new_description": "desc",
                }
                for i, item in enumerate(batch)
            ]

        with patch.object(
            clustering, "classify_template_candidate_clusters", AsyncMock(side_effect=fake_classify)
        ) as mock_classify:
            await clustering.run_template_candidate_clustering_pass()

        mock_classify.assert_awaited()
        unclustered = await list_unclustered_template_candidates()
        self.assertFalse(any(r["id"] == candidate_id for r in unclustered))

    async def test_pass_is_noop_when_nothing_unclustered(self):
        """Guards the early-return in run_template_candidate_clustering_pass —
        must not call the classifier at all when there's nothing to classify,
        since an empty-batch call would be pure wasted cost."""
        with patch.object(clustering, "list_unclustered_template_candidates", AsyncMock(return_value=[])):
            with patch.object(
                clustering, "classify_template_candidate_clusters", AsyncMock()
            ) as mock_classify:
                await clustering.run_template_candidate_clustering_pass()
        mock_classify.assert_not_awaited()

    async def test_classification_failure_leaves_batch_unclustered_for_retry(self):
        await add_template_candidate(
            creator_user_id=555,
            summary="сломанная классификация — pass-test-marker-2",
            fallback_reason="no_template_match",
            selected_templates=[],
        )
        rows = await list_template_candidates()
        candidate_id = next(r for r in rows if r["summary"].endswith("pass-test-marker-2"))["id"]

        with patch.object(
            clustering, "classify_template_candidate_clusters", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await clustering.run_template_candidate_clustering_pass()  # must not raise

        unclustered = await list_unclustered_template_candidates()
        self.assertTrue(any(r["id"] == candidate_id for r in unclustered))


if __name__ == "__main__":
    unittest.main()
