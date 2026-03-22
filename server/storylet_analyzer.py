"""
Three Clue Rule Analyzer for the DM Engine.

Gap 5 implementation: Detects chokepoint storylets (single points of failure)
in the storylet dependency graph and generates N-2 additional storylets per
chokepoint to provide redundant discovery vectors.

Inverse Three Clue Redundancy:
  "For any essential conclusion, the architecture must provide at least
   three distinct vectors of discovery."

No LLM calls — pure graph analysis + deterministic heuristics.
"""

import uuid
from typing import Dict, List, Optional, Set, Any
from collections import deque

from storylet import Storylet, StoryletPrerequisites, GraphQuery, TensionLevel


class StoryletGraph:
    """
    In-memory directed bipartite graph of storylet dependencies.

    An AND-edge (hard prerequisite): both prerequisite storylet AND target must fire
    An OR-edge: either prerequisite OR target may satisfy the requirement

    Represented as adjacency lists for efficient traversal.
    """

    def __init__(self, storylets: List[Storylet]) -> None:
        self.storylets: Dict[str, Storylet] = {str(s.id): s for s in storylets}
        # outgoing[i] = list of storylet IDs that i leads to
        self.outgoing: Dict[str, List[str]] = {str(s.id): [] for s in storylets}
        # incoming[i] = list of storylet IDs that lead to i
        self.incoming: Dict[str, List[str]] = {str(s.id): [] for s in storylets}
        self._build_graph()

    def _build_graph(self) -> None:
        """
        Build adjacency from storylet prerequisites.

        Edges represent narrative causation: A → B means A can lead to B.
        Two strategies:
        1. Explicit: B has a GraphQuery prerequisite referencing A (via tag/entity)
        2. Implicit: same tag creates a chain: S1 → S2 → S3 (ordered by index)

        Strategy 2 (linear chaining by shared tag) is the default heuristic:
        for each unique tag, sort storylets by their index in self.storylets,
        then create edges S[i] → S[i+1].
        """
        # Group storylets by tag
        tag_to_storylets: Dict[str, List[str]] = {}
        for sid, storylet in self.storylets.items():
            for tag in storylet.tags:
                if tag not in tag_to_storylets:
                    tag_to_storylets[tag] = []
                tag_to_storylets[tag].append(sid)

        # For each tag group, create a linear chain (storylet order = discovery order)
        for tag, sids in tag_to_storylets.items():
            for i in range(len(sids) - 1):
                from_sid = sids[i]
                to_sid = sids[i + 1]
                self.outgoing[from_sid].append(to_sid)
                self.incoming[to_sid].append(from_sid)

    def _storylets_are_connected(self, from_s: Storylet, to_s: Storylet) -> bool:
        """
        Returns True if firing `from_s` could help satisfy `to_s`'s prerequisites.

        Unidirectional heuristic:
        - to_s has a prerequisite that from_s satisfies (e.g. shared tags)
        - Direction: from_s → to_s (from_s fires first, then to_s)
        """
        if from_s.id == to_s.id:
            return False
        # Shared tags: if from_s has fewer tags, it fires first and leads to to_s
        # Only connect if from_s.tags is a subset of to_s.tags (to_s is more specific)
        # OR if from_s has tags that to_s also has (shared narrative space)
        if from_s.tags and to_s.tags:
            # Unidirectional: only from S1 to S2 if S1.tags ⊂ S2.tags
            # (S1 is the prerequisite, S2 is the more specific storylet)
            if from_s.tags < to_s.tags:
                return True
            if from_s.tags & to_s.tags and len(from_s.tags) < len(to_s.tags):
                return True
        return False

    def find_leaf_nodes(self) -> List[str]:
        """Storylets with no outgoing edges — they conclude narrative threads."""
        return [sid for sid, outs in self.outgoing.items() if not outs]

    def find_root_nodes(self) -> List[str]:
        """Storylets with no incoming edges — entry points to the graph."""
        return [sid for sid, ins in self.incoming.items() if not ins]

    def find_chokepoints(self) -> List[str]:
        """
        Find storylets that are the sole path to N≥1 leaf nodes.
        A chokepoint is any node where |incoming| == 1 and it leads to ≥1 leaf.
        """
        leaves = set(self.find_leaf_nodes())
        chokepoints = []
        for sid in self.storylets:
            # Node is a chokepoint if removing it would orphan at least one leaf
            # Simple proxy: node has incoming=1 and leads to ≥1 leaf
            if len(self.incoming[sid]) == 1 and self._leads_to_any(sid, leaves):
                chokepoints.append(sid)
        return chokepoints

    def _leads_to_any(self, sid: str, targets: Set[str]) -> bool:
        """BFS: does sid's subgraph contain any target?"""
        queue = deque([sid])
        visited: Set[str] = set()
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            if current in targets:
                return True
            for neighbor in self.outgoing.get(current, []):
                if neighbor not in visited:
                    queue.append(neighbor)
        return False

    def find_discovery_vectors(self, target_sid: str) -> List[List[str]]:
        """
        Find all paths (discovery vectors) TO the target storylet.
        Returns list of paths, each path is a list of storylet IDs.
        """
        paths: List[List[str]] = []
        for root in self.find_root_nodes():
            self._dfs_find_paths(root, target_sid, [root], paths)
        return paths

    def _dfs_find_paths(
        self,
        current: str,
        target: str,
        path: List[str],
        results: List[List[str]],
    ) -> None:
        if current == target:
            results.append(list(path))
            return
        for neighbor in self.outgoing.get(current, []):
            if neighbor not in path:  # Avoid cycles
                path.append(neighbor)
                self._dfs_find_paths(neighbor, target, path, results)
                path.pop()

    def redundancy_score(self, target_sid: str) -> int:
        """
        Count how many independent paths reach the target.
        Score < 3 means the Three Clue Rule is violated.
        """
        paths = self.find_discovery_vectors(target_sid)
        # Deduplicate by first branching point
        return len(paths)


class ThreeClueAnalyzer:
    """
    Detects Three Clue Rule violations and generates remediation storylets.

    The Three Clue Rule (Gygax): "For any essential conclusion, the
    architecture must provide at least three distinct vectors of discovery."
    """

    def __init__(self, storylets: List[Storylet]) -> None:
        self.storylets = storylets
        self.graph = StoryletGraph(storylets)

    def analyze(self) -> Dict[str, Any]:
        """
        Run full Three Clue analysis.

        Returns a report dict:
          - chokepoints: list of storylet IDs violating the rule
          - paths_to_chokepoint: {sid: list of paths}
          - redundancy_scores: {sid: int}
          - suggestions: {sid: str} — LLM prompt fragments for generating backup storylets
        """
        chokepoints = self.graph.find_chokepoints()
        report: Dict[str, Any] = {
            "chokepoints": chokepoints,
            "paths_to_chokepoint": {},
            "redundancy_scores": {},
            "suggestions": {},
        }

        for sid in chokepoints:
            paths = self.graph.find_discovery_vectors(sid)
            score = len(paths)
            report["paths_to_chokepoint"][sid] = paths
            report["redundancy_scores"][sid] = score

            if score < 3:
                # Generate suggestion for creating N-2 backup storylets
                storylet = self.graph.storylets.get(sid)
                missing = 3 - score
                suggestion = (
                    f"Three Clue Rule violation: storylet '{storylet.name if storylet else sid}' "
                    f"has only {score} discovery vector(s). "
                    f"Generate {missing} additional storylet(s) that provide alternative paths "
                    f"to the same narrative conclusion."
                )
                report["suggestions"][sid] = suggestion

        return report

    def generate_backup_storylet(
        self,
        chokepoint_id: str,
        branch_from_path: List[str],
    ) -> Storylet:
        """
        Generate a deterministic backup storylet that provides an additional
        discovery vector for a chokepoint.

        Uses the branch_from_path to infer what tags/entities to reuse.
        """
        chokepoint = self.graph.storylets.get(chokepoint_id)
        if not chokepoint:
            raise ValueError(f"Unknown storylet: {chokepoint_id}")

        # Collect tags from the existing path
        all_tags = set()
        for sid in branch_from_path:
            s = self.graph.storylets.get(sid)
            if s:
                all_tags.update(s.tags)

        # Create a new storylet with a slightly different tension and shared tags
        backup = Storylet(
            name=f"Backup: {chokepoint.name}",
            description=(
                f"Auto-generated Three Clue backup for '{chokepoint.name}'. "
                "Provides an alternative discovery vector to the same narrative."
            ),
            tension_level=chokepoint.tension_level,
            prerequisites=StoryletPrerequisites(
                # Make it reachable via a different path: require one of the
                # existing path storylets but NOT the chokepoint itself.
                any_of=[
                    GraphQuery(
                        query_type="node_exists",
                        node_name=sid,  # Refer to other storylet by ID
                    )
                    for sid in branch_from_path
                    if sid != chokepoint_id
                ]
            ),
            content=(
                f"[AUTO-GENERATED THREE CLUE BACKUP]\n"
                f"This storylet was algorithmically generated to provide a "
                f"third discovery vector for '{chokepoint.name}'."
            ),
            tags=all_tags | {"three_clue_backup", f"backup_of_{chokepoint.id}"},
            max_occurrences=-1,  # Unlimited
        )
        return backup
