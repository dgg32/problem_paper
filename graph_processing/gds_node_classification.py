#!/usr/bin/env python3
"""
gds_node_classification.py — Tier-B learned triage signal (plan.md §2.2).

A Neo4j GDS node-classification pipeline that learns what distinguishes a
MISCONDUCT retraction from an honest-error retraction, then applies that model
to the not-yet-retracted expansion candidates to produce a per-paper
`gds_misconduct_prob` — one more triage signal, NOT a verdict (plan.md §0).

Design decisions (read before trusting the output):
  * LABELS are drawn only from RETRACTED papers:
        class 1 = retracted for a misconduct reason (Paper Mill, Fabrication,
                  Image/Results Manipulation, ORI/official finding, ...)
        class 0 = retracted for any other reason (author error, publisher
                  error, duplication, plagiarism dispute, ...)
    Because BOTH classes are retracted, the "candidates were sampled near
    retracted papers" artifact does NOT separate the two training classes —
    so it is not label leakage for this target. The model must learn what
    makes a retraction a MISCONDUCT one.
  * DOMAIN SHIFT is the honest caveat: we train on retracted papers and
    predict on not-yet-retracted ones. Feature meaning can shift. Treat the
    output as a ranked hypothesis for human review, never a conclusion.
  * The projected graph is co-authorship (shared probable-person / cluster_id)
    + CITES, undirected. Features combine FastRP embeddings + degree + PageRank
    (graph-learned) with interpretable domain properties (leakage-aware
    co-author-misconduct count, journal retraction rate, author count, and the
    Phase-4 sensor flag counts).

Pipeline is fully idempotent: drops any prior GDS graph/pipeline/model and the
temporary GDS_LINK edges on every run.

Usage:
  python graph_processing/gds_node_classification.py            # full run
  python graph_processing/gds_node_classification.py --prep-only
  python graph_processing/gds_node_classification.py --keep-links   # leave GDS_LINK edges
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "graph_processing"))
from normalize_authors import resolve_connection  # noqa: E402

GRAPH = "papers_gds"
PIPE = "mc_pipeline"
MODEL = "mc_model"

MISCONDUCT_REASONS = [
    "Misconduct - Official Investigation(s) and/or Finding(s)",
    "Investigation by ORI",
    "Paper Mill",
    "Falsification/Fabrication of Data",
    "Falsification/Fabrication of Image",
    "Falsification/Fabrication of Results",
    "Manipulation of Images",
    "Manipulation of Results",
    "Euphemisms for Misconduct",
    "Misconduct by Author",
]

FEATURE_PROPS = [
    "emb", "deg", "pr",                       # graph-learned
    "n_authors", "coauthor_other_misconduct", # domain
    "journal_retr_rate",
    "retracted_citation_flag_count",
    "reference_integrity_flag_count",
    "journal_integrity_flag_count",
]

NODE_PROPS_FOR_PROJECTION = [
    "misconduct_label", "n_authors", "coauthor_other_misconduct",
    "journal_retr_rate", "retracted_citation_flag_count",
    "reference_integrity_flag_count", "journal_integrity_flag_count",
]


# --------------------------------------------------------------------------- #
# Stage A — prep: labels, node labels, domain features                        #
# --------------------------------------------------------------------------- #
def prep(session) -> None:
    print("[prep] setting misconduct_label + :LabeledPaper on retracted papers...")
    session.run(
        """
        MATCH (p:Paper {is_retracted:true})
        OPTIONAL MATCH (p)-[:RETRACTED_FOR]->(r:Reason)
        WITH p, collect(r.code) AS reasons
        SET p.misconduct_label =
              CASE WHEN any(x IN reasons WHERE x IN $reasons) THEN 1 ELSE 0 END
        SET p:LabeledPaper
        """,
        reasons=MISCONDUCT_REASONS,
    ).consume()

    print("[prep] marking not-retracted candidates :CandidatePaper (label -1)...")
    session.run(
        """
        MATCH (p:Paper {is_retracted:false})
        SET p.misconduct_label = -1
        SET p:CandidatePaper
        """
    ).consume()

    print("[prep] n_authors...")
    session.run(
        """
        MATCH (p:Paper)
        OPTIONAL MATCH (p)<-[:WROTE]-(a:AuthorInstance)
        WITH p, count(a) AS n
        SET p.n_authors = n
        """
    ).consume()

    print("[prep] coauthor_other_misconduct (leakage-aware)...")
    session.run(
        """
        MATCH (p:Paper) SET p.coauthor_other_misconduct = 0
        """
    ).consume()
    session.run(
        """
        MATCH (p:Paper)<-[:WROTE]-(a:AuthorInstance)
        WITH p, collect(DISTINCT a.cluster_id) AS clusters
        UNWIND clusters AS cid
        OPTIONAL MATCH (m:AuthorInstance {cluster_id: cid})-[:WROTE]->(mp:Paper)-[:RETRACTED_FOR]->(r:Reason)
        WHERE mp <> p AND r.code IN $reasons
        WITH p, cid, count(mp) AS mpaper_count
        WITH p, sum(CASE WHEN mpaper_count > 0 THEN 1 ELSE 0 END) AS c
        SET p.coauthor_other_misconduct = c
        """,
        reasons=MISCONDUCT_REASONS,
    ).consume()

    print("[prep] journal_retr_rate...")
    session.run(
        """
        MATCH (p:Paper) SET p.journal_retr_rate = 0.0
        """
    ).consume()
    session.run(
        """
        MATCH (j:Journal)<-[:PUBLISHED_IN]-(p:Paper)
        WITH j, toFloat(sum(CASE WHEN p.is_retracted THEN 1 ELSE 0 END)) / count(p) AS rate
        MATCH (j)<-[:PUBLISHED_IN]-(p2:Paper)
        SET p2.journal_retr_rate = rate
        """
    ).consume()

    print("[prep] defaulting sensor flag counts to 0 where absent...")
    session.run(
        """
        MATCH (p:Paper)
        SET p.retracted_citation_flag_count = coalesce(p.retracted_citation_flag_count, 0),
            p.reference_integrity_flag_count = coalesce(p.reference_integrity_flag_count, 0),
            p.journal_integrity_flag_count   = coalesce(p.journal_integrity_flag_count, 0),
            p.misconduct_label               = coalesce(p.misconduct_label, -1)
        """
    ).consume()

    counts = session.run(
        """
        MATCH (p:LabeledPaper)
        RETURN p.misconduct_label AS label, count(*) AS n ORDER BY label
        """
    ).data()
    cand = session.run("MATCH (p:CandidatePaper) RETURN count(*) AS n").single()["n"]
    print(f"[prep] labeled distribution: {counts}; candidates: {cand}")


# --------------------------------------------------------------------------- #
# Stage B — temporary GDS_LINK edges + projection                             #
# --------------------------------------------------------------------------- #
def build_links(session) -> None:
    print("[links] dropping any prior GDS_LINK edges...")
    session.run("MATCH ()-[l:GDS_LINK]->() DELETE l").consume()

    print("[links] shared-probable-person (co-authorship) links...")
    n = session.run(
        """
        MATCH (a1:AuthorInstance)-[:WROTE]->(p1:Paper)
        MATCH (a2:AuthorInstance)-[:WROTE]->(p2:Paper)
        WHERE a1.cluster_id = a2.cluster_id AND id(p1) < id(p2)
        MERGE (p1)-[:GDS_LINK]->(p2)
        RETURN count(*) AS n
        """
    ).single()["n"]
    print(f"[links]   {n} shared-author links")

    print("[links] CITES links...")
    n = session.run(
        """
        MATCH (p1:Paper)-[:CITES]->(p2:Paper)
        MERGE (p1)-[:GDS_LINK]->(p2)
        RETURN count(*) AS n
        """
    ).single()["n"]
    print(f"[links]   +{n} cites links merged")


def project(session) -> None:
    session.run(f"CALL gds.graph.drop('{GRAPH}', false) YIELD graphName RETURN graphName").consume()
    print(f"[project] projecting '{GRAPH}' (Paper nodes + GDS_LINK undirected)...")
    res = session.run(
        f"""
        CALL gds.graph.project(
          '{GRAPH}',
          {{
            Paper:          {{ properties: $props }},
            LabeledPaper:   {{ properties: $props }},
            CandidatePaper: {{ properties: $props }}
          }},
          {{ GDS_LINK: {{ orientation: 'UNDIRECTED' }} }}
        )
        YIELD graphName, nodeCount, relationshipCount
        RETURN graphName, nodeCount, relationshipCount
        """,
        props=NODE_PROPS_FOR_PROJECTION,
    ).single()
    print(f"[project]   nodes={res['nodeCount']} rels={res['relationshipCount']}")


# --------------------------------------------------------------------------- #
# Stage C — pipeline + train                                                  #
# --------------------------------------------------------------------------- #
def train(session) -> dict:
    session.run(f"CALL gds.pipeline.drop('{PIPE}', false) YIELD pipelineName RETURN pipelineName").consume()
    session.run(f"CALL gds.model.drop('{MODEL}', false) YIELD modelName RETURN modelName").consume()

    print("[train] creating pipeline + feature steps...")
    session.run(f"CALL gds.beta.pipeline.nodeClassification.create('{PIPE}')").consume()

    # graph-learned node properties (mutate on the projected graph)
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addNodeProperty(
          '{PIPE}', 'fastRP',
          {{ mutateProperty: 'emb', embeddingDimension: 64, randomSeed: 42,
             propertyRatio: 0.0, iterationWeights: [0.0, 1.0, 1.0] }}
        )
        """
    ).consume()
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addNodeProperty(
          '{PIPE}', 'degree', {{ mutateProperty: 'deg' }}
        )
        """
    ).consume()
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addNodeProperty(
          '{PIPE}', 'pageRank', {{ mutateProperty: 'pr' }}
        )
        """
    ).consume()

    print(f"[train] selecting features: {FEATURE_PROPS}")
    session.run(
        f"CALL gds.beta.pipeline.nodeClassification.selectFeatures('{PIPE}', $feats)",
        feats=FEATURE_PROPS,
    ).consume()

    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.configureSplit(
          '{PIPE}', {{ testFraction: 0.25, validationFolds: 4 }}
        )
        """
    ).consume()

    print("[train] adding candidate models (LogisticRegression + RandomForest)...")
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addLogisticRegression(
          '{PIPE}', {{ penalty: 0.062, maxEpochs: 200 }}
        )
        """
    ).consume()
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addLogisticRegression(
          '{PIPE}', {{ penalty: 1.0, maxEpochs: 200 }}
        )
        """
    ).consume()
    session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.addRandomForest(
          '{PIPE}', {{ numberOfDecisionTrees: 200, maxDepth: 10 }}
        )
        """
    ).consume()

    print("[train] training (targetNodeLabels=LabeledPaper)...")
    row = session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.train(
          '{GRAPH}', {{
            pipeline: '{PIPE}',
            modelName: '{MODEL}',
            targetNodeLabels: ['LabeledPaper'],
            targetProperty: 'misconduct_label',
            metrics: ['F1_WEIGHTED','ACCURACY','F1(class=1)','PRECISION(class=1)','RECALL(class=1)'],
            randomSeed: 42
          }}
        )
        YIELD modelInfo
        RETURN modelInfo.bestParameters AS best, modelInfo.metrics AS metrics
        """
    ).single()
    best = row["best"]
    metrics = row["metrics"]

    def mval(metric_key: str, split: str = "test"):
        m = metrics.get(metric_key)
        if isinstance(m, dict):
            return m.get(split)
        return m  # some metrics come back as a scalar/list

    def fmt(v):
        return f"{v:.3f}" if isinstance(v, (int, float)) else str(v)

    print("\n=== TRAIN METRICS (held-out test) ===")
    print(f"  best model        : {best.get('methodName')}  {best}")
    print(f"  F1_weighted  test : {fmt(mval('F1_WEIGHTED'))}  (outerTrain {fmt(mval('F1_WEIGHTED', 'outerTrain'))})")
    print(f"  Accuracy     test : {fmt(mval('ACCURACY'))}")
    print(f"  F1(class=1)  test : {fmt(mval('F1_class_1'))}   <-- misconduct class")
    print(f"  Precision(1) test : {fmt(mval('PRECISION_class_1'))}")
    print(f"  Recall(1)    test : {fmt(mval('RECALL_class_1'))}")
    print(f"\n  [metric keys available: {list(metrics.keys())}]")
    return {"best": best, "metrics": metrics}


# --------------------------------------------------------------------------- #
# Stage D — predict on candidates + write back                                #
# --------------------------------------------------------------------------- #
def predict(session) -> None:
    print("\n[predict] streaming predictions on CandidatePaper...")
    rows = session.run(
        f"""
        CALL gds.beta.pipeline.nodeClassification.predict.stream(
          '{GRAPH}', {{
            modelName: '{MODEL}',
            targetNodeLabels: ['CandidatePaper'],
            includePredictedProbabilities: true
          }}
        )
        YIELD nodeId, predictedClass, predictedProbabilities
        WITH gds.util.asNode(nodeId) AS p, predictedClass, predictedProbabilities
        SET p.gds_misconduct_prob = predictedProbabilities[1],
            p.gds_predicted_class = predictedClass
        RETURN p.doi AS doi, p.title AS title, p.gds_misconduct_prob AS prob
        ORDER BY prob DESC
        LIMIT 15
        """
    ).data()

    print("\n=== TOP 15 CANDIDATES BY LEARNED MISCONDUCT PROBABILITY ===")
    for i, r in enumerate(rows, 1):
        title = (r["title"] or "")[:66]
        print(f"  {i:2}. [{r['prob']:.3f}] {title}  ({r['doi']})")


def teardown(session, keep_links: bool) -> None:
    session.run(f"CALL gds.graph.drop('{GRAPH}', false) YIELD graphName RETURN graphName").consume()
    if not keep_links:
        print("[teardown] removing temporary GDS_LINK edges...")
        session.run("MATCH ()-[l:GDS_LINK]->() DELETE l").consume()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prep-only", action="store_true", help="only run the prep stage")
    ap.add_argument("--keep-links", action="store_true", help="leave GDS_LINK edges in the graph")
    args = ap.parse_args()

    conn = resolve_connection()
    driver = GraphDatabase.driver(conn["uri"], auth=(conn["user"], conn["password"]))
    try:
        with driver.session(database=conn["database"]) as s:
            prep(s)
            if args.prep_only:
                return
            build_links(s)
            project(s)
            try:
                train(s)
                predict(s)
            finally:
                teardown(s, args.keep_links)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
