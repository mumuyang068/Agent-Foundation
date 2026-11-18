from __future__ import annotations

from agent_foundation.modules.graph.contracts import (
    GraphAlgorithmIdentity,
    GraphEdge,
    GraphNode,
    GraphTemporalPath,
)


class LexicalGraphCandidates:
    descriptor = GraphAlgorithmIdentity(
        algorithm_id="graph.lexical-v2", implementation_version="1"
    )

    def rank(
        self, nodes: tuple[GraphNode, ...], *, query: str, limit: int
    ) -> tuple[tuple[str, float], ...]:
        terms = tuple(term for term in query.casefold().split() if term)
        ranked = []
        for node in nodes:
            text = f"{node.label} {node.node_type}".casefold()
            score = float(sum(text.count(term) for term in terms))
            if score > 0:
                ranked.append((node.node_id, score))
        return tuple(sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit])


class RecentGraphCandidates:
    descriptor = GraphAlgorithmIdentity(
        algorithm_id="graph.recent-v2", implementation_version="1"
    )

    def rank(
        self, nodes: tuple[GraphNode, ...], *, query: str, limit: int
    ) -> tuple[tuple[str, float], ...]:
        del query
        ordered = sorted(nodes, key=lambda node: (node.updated_at, node.node_id), reverse=True)
        return tuple((node.node_id, float(len(ordered) - index)) for index, node in enumerate(ordered[:limit]))


class BoundedTemporalTraversal:
    descriptor = GraphAlgorithmIdentity(
        algorithm_id="graph.temporal-bfs-v1", implementation_version="1"
    )

    def traverse(
        self,
        nodes: tuple[GraphNode, ...],
        edges: tuple[GraphEdge, ...],
        *,
        query: str,
        max_hops: int,
        max_items: int,
    ) -> tuple[GraphTemporalPath, ...]:
        terms = tuple(term for term in query.casefold().split() if term)
        if not terms:
            return ()
        by_node = {node.node_id: node for node in nodes}
        seeds = tuple(
            node.node_id
            for node in sorted(nodes, key=lambda item: item.node_id)
            if all(
                term in f"{node.node_id} {node.label} {node.node_type}".casefold()
                for term in terms
            )
        )
        if not seeds:
            return ()
        outgoing: dict[str, list[GraphEdge]] = {}
        for edge in sorted(edges, key=lambda item: item.edge_id):
            if edge.from_node_id in by_node and edge.to_node_id in by_node:
                outgoing.setdefault(edge.from_node_id, []).append(edge)
        paths: list[GraphTemporalPath] = []
        frontier = [(seed, (seed,), ()) for seed in seeds]
        for _hop in range(max_hops):
            next_frontier = []
            for current_id, node_ids, path_edges in frontier:
                for edge in outgoing.get(current_id, ()):
                    if edge.to_node_id in node_ids:
                        continue
                    extended_nodes = (*node_ids, edge.to_node_id)
                    extended_edges = (*path_edges, edge)
                    paths.append(GraphTemporalPath(
                        edge_refs=tuple(item.ref() for item in extended_edges),
                        node_refs=tuple(by_node[item].ref() for item in extended_nodes),
                        score=float(len(terms) + (max_hops - len(extended_edges) + 1)),
                    ))
                    if len(paths) >= max_items:
                        return tuple(paths)
                    next_frontier.append((edge.to_node_id, extended_nodes, extended_edges))
            frontier = next_frontier
            if not frontier:
                break
        return tuple(paths)
