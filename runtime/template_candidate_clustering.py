from __future__ import annotations

import asyncio
import logging

from db.database import (
    assign_template_candidate_cluster,
    create_template_candidate_cluster,
    list_template_candidate_cluster_labels,
    list_unclustered_template_candidates,
)
from services.claude_service import classify_template_candidate_clusters

logger = logging.getLogger(__name__)

# docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §3 — once a day, not realtime:
# the signal only appears once requests accumulate, and clustering on every
# single insert would be pure LLM-call waste. Same background-loop shape as
# runtime/payment_status_poller.py / runtime/combined_app.py's
# _reminders_sweep_loop — plain while True + sleep, no APScheduler dependency
# (confirmed this codebase's only existing periodic-task mechanism before
# adding this).
SWEEP_INTERVAL_SECONDS = 86400

# Batch size per classification call — keeps a single Haiku request's input
# bounded even if a burst of from-scratch bots landed since the last pass.
# Multiple batches within one sweep all see the SAME known_clusters snapshot
# taken at the start of the pass (not re-read per batch) so clusters minted
# by an earlier batch in this same sweep aren't visible to a later batch —
# acceptable per the design doc's accepted near-duplicate-cluster tradeoff
# (§3), and simpler than re-fetching mid-sweep.
BATCH_SIZE = 20


async def run_template_candidate_clustering_pass() -> None:
    """One incremental pass: classify every currently-unclustered candidate
    against the known cluster labels, assigning each to an existing cluster
    or a newly-minted one. Never re-touches already-clustered rows (see
    design doc §3 — keeps cost linear in new candidates, not the whole
    dataset, and keeps a cluster's assigned members stable between runs)."""
    candidates = await list_unclustered_template_candidates()
    if not candidates:
        return
    known_clusters = await list_template_candidate_cluster_labels()

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        try:
            assignments = await classify_template_candidate_clusters(known_clusters, batch)
        except Exception:
            logger.exception(
                f"run_template_candidate_clustering_pass: classification call failed for batch "
                f"starting at offset {start} — candidates in this batch stay unclustered, retried next pass"
            )
            continue

        for assignment in assignments:
            candidate = batch[assignment["index"]]
            cluster_id = assignment["existing_cluster_id"]
            if cluster_id is None:
                cluster_id = await create_template_candidate_cluster(
                    assignment["new_label"], assignment["new_description"]
                )
                known_clusters.append(
                    {"id": cluster_id, "label": assignment["new_label"], "description": assignment["new_description"]}
                )
            await assign_template_candidate_cluster(candidate["id"], cluster_id)

    logger.info(f"run_template_candidate_clustering_pass: processed {len(candidates)} candidate(s)")


async def template_candidate_clustering_loop() -> None:
    while True:
        try:
            await run_template_candidate_clustering_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("template_candidate_clustering_loop: pass failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
