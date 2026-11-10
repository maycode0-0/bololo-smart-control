"""Summarize saved REA evidence without printing its large graph or secrets."""

import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    output = []
    for filename in ("rea-artifact.json", "rea-focused-complete.json"):
        evidence = json.loads((root / "analysis" / filename).read_text(encoding="utf-8-sig"))
        if "evidence_id" not in evidence:
            raise ValueError(f"Incomplete REA evidence: {filename}")
        result = evidence["normalized_result"]
        graph = result.get("graph", {})
        output.append(
            {
                "file": filename,
                "evidence_id": evidence["evidence_id"],
                "operation": evidence["operation"],
                "subject": evidence["subject"],
                "confidence": evidence["confidence"],
                "authority": evidence["authority"],
                "statistics": result.get("statistics"),
                "graph_node_count": len(graph.get("nodes", [])),
                "graph_edge_count": len(graph.get("edges", [])),
                "limitations": evidence["limitations"],
            }
        )
    (root / "analysis" / "rea-summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for record in output:
        print(record["operation"], record["evidence_id"])
        print(json.dumps(record["statistics"]))


if __name__ == "__main__":
    main()
