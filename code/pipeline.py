"""Construction pipeline: story prompts to edit trees.
1. **Cluster.** Normalize a prompt to lowercase word tokens, keep the first 300,
   take 3-gram shingles, join two prompts whose shingle Jaccard is at least 0.5,
   and take connected components. Produces 91,270 clusters over the 275,635
   prompts, of which 22,999 are non-singleton.

2. **User disambiguation.** Hashed IPs are not reliable identities: the same person
   appears under several when they change network or use a VPN, and clustering
   routinely groups their prompts together. Every IP appearing in the same cluster
   is therefore merged, and the connected components of that merge are the
   *inferred users* used for all user-balanced estimates.

3. **Parent selection.** Order a cluster's prompts by timestamp. For each prompt
   after the first, choose as parent the earlier prompt with the highest Jaccard
   similarity, computed over 3-grams of the *full* normalized text rather than the
   300-token prefix. Ties go to the later candidate. The first prompt is the root.

4. **Prune.** Because parent selection uses full text, some chosen parents fall
   below the clustering threshold. Drop every edge with similarity below 0.5 and
   split what remains into connected components. Produces 94,130 components:
   69,839 singletons and the 24,291 released edit trees.

## Inputs, and where they come from

Stage 1 needs prompt text. Stages 2 and 3 need three more fields, none of which are
in the released tables. All three come from the WildChat record itself, so
`helpers.rehydrate_from_wildchat` collects them and `helpers.pipeline_inputs` shapes
them for the functions here:

* `timestamps`: the record's `timestamp`. WildChat stamps a *conversation*, not a
  turn, so every prompt a user edits inside one chat carries the same value.
* `source_order`: the tie-break that resolves exactly that. `pipeline_inputs` builds
  it from the order the records arrive in, `(record position in the stream, turn
  position in the record)`, with the prompt id settling any remaining tie. It
  therefore assumes the records arrive in dataset order.
* `hashed_ip`: the record's `hashed_ip`, for `merge_users_by_cluster`.

    hydrated = helpers.rehydrate_from_wildchat(stories.prompt_id)
    parts = helpers.pipeline_inputs(hydrated)
    built = build_trees(parts["prompts"], parts["timestamps"], parts["source_order"])
    users = merge_users_by_cluster(built["clusters"], parts["hashed_ip"])

Record which WildChat snapshot you built from: upstream removals change cluster
membership, and therefore the trees.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

PREFIX_TOKENS = 300
SHINGLE_N = 3
JACCARD_THRESHOLD = 0.5
WORD_RE = re.compile(r"\w+")


def normalize_words(text: str) -> list[str]:
    return WORD_RE.findall((text or "").lower())


def build_shingles(text: str, prefix_tokens: int = PREFIX_TOKENS,
                   n: int = SHINGLE_N) -> list[int]:
    """Clustering features: n-grams over the first `prefix_tokens` tokens.

    Shingles are hashed to 64-bit integers. A prompt shorter than `n` tokens
    contributes a single shingle covering all of its tokens rather than none.
    """
    tokens = normalize_words(text)[:prefix_tokens]
    if not tokens:
        return []
    if len(tokens) < n:
        shingle_texts = [" ".join(tokens)]
    else:
        shingle_texts = [
            " ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)
        ]
    return sorted({
        int.from_bytes(hashlib.blake2b(t.encode("utf-8"), digest_size=8).digest(),
                       "little", signed=False)
        for t in shingle_texts
    })


def full_text_shingles(text: str, n: int = SHINGLE_N) -> frozenset[tuple[str, ...]]:
    """Parent-selection features: n-grams over the whole prompt."""
    tokens = normalize_words(text)
    if not tokens:
        return frozenset()
    if len(tokens) < n:
        return frozenset({tuple(tokens)})
    return frozenset(tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1))


def jaccard(left, right) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    intersection = sum(1 for item in left if item in right)
    union = len(left) + len(right) - intersection
    return 0.0 if union <= 0 else intersection / union


def prefix_length(shingle_count: int, threshold: float = JACCARD_THRESHOLD) -> int:
    """Number of rarest shingles that must be indexed for an exact prefix join."""
    if shingle_count <= 0:
        return 0
    return max(shingle_count - math.ceil(threshold * shingle_count) + 1, 0)


def required_overlap(left_count: int, right_count: int,
                     threshold: float = JACCARD_THRESHOLD) -> int:
    return math.ceil((threshold * (left_count + right_count)) / (1.0 + threshold))


def can_meet_jaccard_by_size(left_count: int, right_count: int,
                             threshold: float = JACCARD_THRESHOLD) -> bool:
    if left_count == 0 and right_count == 0:
        return True
    if left_count == 0 or right_count == 0:
        return False
    return (min(left_count, right_count) / max(left_count, right_count)) >= threshold


def _components(node_ids, adjacency) -> list[list]:
    seen, out = set(), []
    for start in node_ids:
        if start in seen:
            continue
        component, stack = [start], [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            for neighbour in adjacency.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    component.append(neighbour)
                    stack.append(neighbour)
        out.append(component)
    return out


def cluster_prompts(prompts: dict[str, str],
                    threshold: float = JACCARD_THRESHOLD) -> list[list[str]]:
    """Connected components of the shingle-Jaccard similarity graph.

    `prompts` maps prompt_id to prompt text. Pairs are found with an exact prefix
    join: shingles are ordered by global frequency, only the rarest
    `prefix_length` of each prompt are indexed, and candidates are filtered by
    set size and required overlap before an exact Jaccard is computed. The
    result is identical to scoring every pair.
    """
    features = {pid: build_shingles(text) for pid, text in prompts.items()}

    frequencies: Counter = Counter()
    for shingles in features.values():
        frequencies.update(shingles)

    adjacency: dict[str, set[str]] = defaultdict(set)
    index: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for pid in sorted(prompts):
        shingles = features[pid]
        count = len(shingles)
        if not count:
            continue
        ordered = sorted(shingles, key=lambda value: (frequencies[value], value))
        prefix = ordered[: prefix_length(count, threshold)]

        candidates: set[str] = set()
        for token in prefix:
            for other, other_count in index.get(token, ()):
                if can_meet_jaccard_by_size(other_count, count, threshold):
                    candidates.add(other)
        for other in candidates:
            other_shingles = features[other]
            if min(len(other_shingles), count) < required_overlap(
                len(other_shingles), count, threshold
            ):
                continue
            if jaccard(set(shingles), set(other_shingles)) >= threshold:
                adjacency[pid].add(other)
                adjacency[other].add(pid)

        for token in prefix:
            index[token].append((pid, count))
    return _components(sorted(prompts), adjacency)


def _sort_key(
    pid: str,
    timestamps: dict[str, float | None],
    source_order: dict[str, int] | None,
):
    stamp = timestamps.get(pid)
    tie_breaker = source_order[pid] if source_order is not None else 0
    return (
        0 if stamp is not None else 1,
        stamp if stamp is not None else float("inf"),
        tie_breaker,
    )


def select_parents(cluster_ids: list[str], prompts: dict[str, str],
                   timestamps: dict[str, float | None],
                   source_order: dict[str, int] | None = None) -> list[dict]:
    """Pick each prompt's most similar temporal predecessor within one cluster."""
    timestamp_counts = Counter(timestamps.get(pid) for pid in cluster_ids)
    if source_order is None and any(count > 1 for count in timestamp_counts.values()):
        raise ValueError("source_order is required when timestamps tie or are missing")
    if source_order is not None:
        missing = set(cluster_ids) - source_order.keys()
        if missing:
            raise ValueError(f"source_order is missing {len(missing)} prompt ids")
    ordered = sorted(
        cluster_ids,
        key=lambda pid: _sort_key(pid, timestamps, source_order),
    )
    features = {pid: full_text_shingles(prompts[pid]) for pid in ordered}
    edges = []
    for position, child in enumerate(ordered):
        best_position, best_similarity = None, 0.0
        for candidate_position in range(position):
            similarity = jaccard(features[child], features[ordered[candidate_position]])
            # Ties resolve to the later candidate.
            if similarity > best_similarity or (
                similarity == best_similarity and best_position is not None
                and candidate_position > best_position
            ):
                best_similarity, best_position = similarity, candidate_position
        if best_position is not None:
            edges.append({
                "parent_prompt_id": ordered[best_position],
                "child_prompt_id": child,
                "parent_similarity": best_similarity,
            })
    return edges


def prune_and_split(cluster_ids: list[str], edges: list[dict],
                    threshold: float = JACCARD_THRESHOLD) -> tuple[list[dict], list[list[str]]]:
    """Drop edges below the threshold and split into connected components."""
    kept = [edge for edge in edges if edge["parent_similarity"] >= threshold]
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in kept:
        adjacency[edge["parent_prompt_id"]].add(edge["child_prompt_id"])
        adjacency[edge["child_prompt_id"]].add(edge["parent_prompt_id"])
    return kept, _components(sorted(cluster_ids), adjacency)


def parse_timestamp_ms(value) -> float | None:
    """WildChat timestamp to epoch milliseconds.

    A value with no timezone is read as UTC, which is what WildChat records. Naive
    datetimes would otherwise be resolved in the machine's local zone, so a cluster
    spanning a daylight-saving transition would order differently on different
    machines: on 2024-03-10 in a US zone the missing 02:00-03:00 hour sorts 02:43
    after 03:08, silently changing which prompt becomes the parent.
    """
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000


def build_trees(
    prompts: dict[str, str],
    timestamps: dict[str, float | None],
    source_order: dict[str, int] | None = None,
) -> dict:
    """Run all three stages over a set of prompts."""
    clusters = cluster_prompts(prompts)
    all_edges, all_components = [], []
    for cluster in clusters:
        edges = select_parents(cluster, prompts, timestamps, source_order)
        kept, components = prune_and_split(cluster, edges)
        all_edges.extend(kept)
        all_components.extend(components)
    return {
        "clusters": clusters,
        "edges": all_edges,
        "components": all_components,
        "trees": [c for c in all_components if len(c) > 1],
    }


def merge_users_by_cluster(clusters: list[list[str]],
                           hashed_ip: dict[str, str]) -> dict[str, str]:
    """Merge hashed IPs that co-occur in a cluster into inferred users.

    Returns a map from prompt_id to an inferred-user id, which is the
    lexicographically smallest hashed IP in that user's merged group. Prompts with
    no hashed IP are left out.

    Hashed IPs do not identify people: one person can appear under several, and a
    prompt shared between people can appear under several as well. Merging by
    co-clustering is what the analyses treat as a user; it is an inference, not an
    identity.
    """
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        root = item
        while parent[root] != root:
            parent[root] = parent[parent[root]]
            root = parent[root]
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for prompt_id in hashed_ip:
        parent.setdefault(hashed_ip[prompt_id], hashed_ip[prompt_id])
    for cluster in clusters:
        ips = sorted({hashed_ip[p] for p in cluster if p in hashed_ip})
        for other in ips[1:]:
            union(ips[0], other)
    return {p: find(ip) for p, ip in hashed_ip.items()}
