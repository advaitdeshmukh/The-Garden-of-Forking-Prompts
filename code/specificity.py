"""Lexical specificity of prompts and responses.

Two analyses live here, both needing rehydrated text:

* the root versus deepest-leaf comparison within edit trees, and
* the correlation between a tree's prompt specificity and its story specificity.

Specificity follows Zhang et al. (2017): a word's score is the log ratio of its
rate inside one tree to its rate in a background distribution. The background is
the pooled corpus of the sampled texts themselves, so a text is distinctive
relative to what other users wrote rather than to general English.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import statistics
import warnings
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from scipy.stats import spearmanr, wilcoxon


SEED = 20240811
MIN_NODES = 5
MIN_CHANGED_DEPTH = 2
WORD_CAP = 100
NODES_PER_TREE = 5
BOOTSTRAPS = 2000

# Tokens for the edit signature: ASCII alphanumerics, optionally carrying one
# internal apostrophe. Applied to the raw text, with no smart-quote folding.
SIGNATURE_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")

# High-precision refusal preambles in addition to the WildGuard classifier.
# Only the opening of a response is considered.
SOFT_REFUSAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"^(?:\[[^\]]+\]\s*)?(?:i(?:'m| am) sorry|my apologies|i apologize)[,!. ]{0,8}(?:but|however).{0,180}\b(?:cannot|can't|won't|unable|not able|not appropriate|against)\b",
        r"^(?:\[[^\]]+\]\s*)?(?:as an ai(?: language model)?[, ]+)?(?:i|we)\s+(?:cannot|can't|won't|am unable to|are unable to|will not)\s+(?:fulfill|comply|provide|assist|help|generate|continue|write|create|engage|participate)",
        r"^.{0,180}\bas an ai(?: language model)?.{0,160}\b(?:cannot|can't|won't|unable|not programmed|not able)\b",
        r"^.{0,180}\b(?:request|content|story|scene|direction)\b.{0,160}\b(?:against|violates?)\b.{0,80}\b(?:guidelines|policy|policies|ethical|programming)\b",
        r"^(?:i understand|given)\b.{0,200}\b(?:however|constraints?|content requirements?|inappropriate|offensive)\b.{0,240}\b(?:inappropriate|offensive|cannot|can't|unable|not suitable|not appropriate)\b",
    )
)


def _fold_apostrophes(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").replace("‛", "'")


def edit_signature(prompt: str) -> str:
    """Signature deciding whether two prompts count as the same text.

    A child whose signature equals its parent's is a resend, not an edit, and does
    not advance a tree's changed depth.
    """
    tokens = (match.group(0).lower() for match in SIGNATURE_RE.finditer(prompt))
    return hashlib.sha256("\x00".join(tokens).encode("utf-8")).hexdigest()


def specificity_tokens(text: str) -> list[str]:
    """Lowercase ASCII alphabetic words, keeping internal apostrophes."""
    words: list[str] = []
    current: list[str] = []
    for character in _fold_apostrophes(text):
        if character.isalpha() and character.isascii():
            current.append(character.lower())
        elif character == "'" and current:
            current.append(character)
        else:
            if current and current[-1] == "'":
                current.pop()
            if current:
                words.append("".join(current))
            current = []
    if current and current[-1] == "'":
        current.pop()
    if current:
        words.append("".join(current))
    return words


ROLE_PREFIXES = ("system:", "user:", "assistant:")


def current_user_turn(text: str) -> str:
    """Reduce a serialized conversation to its final user turn.

    Some stored prompts are whole transcripts rather than a single message. Scoring
    the transcript would measure the conversation's vocabulary instead of the
    prompt's, so only the last user turn is kept. Text that is not serialized this
    way is returned unchanged.
    """
    stripped = text.lstrip()
    if not stripped.lower().startswith(ROLE_PREFIXES):
        return text
    markers, offset = [], len(text) - len(stripped)
    for line in stripped.splitlines(keepends=True):
        lowered = line.lower()
        for role in ROLE_PREFIXES:
            if lowered.startswith(role):
                markers.append((offset, offset + len(role), role[:-1]))
                break
        offset += len(line)
    user_markers = [marker for marker in markers if marker[2] == "user"]
    if not user_markers:
        return text
    last = user_markers[-1]
    end = next((start for start, _, _ in markers if start > last[0]), len(text))
    return text[last[1]:end].strip() or text


def analysis_text(hydrated_row: dict, side: str) -> str:
    """The text a side contributes to scoring."""
    value = hydrated_row[side] or ""
    return current_user_turn(value) if side == "prompt" else value


def is_soft_refusal(response: str) -> bool:
    opening = _fold_apostrophes(response).lstrip()[:1200]
    return any(pattern.search(opening) for pattern in SOFT_REFUSAL_PATTERNS)


def stable_seed(seed: int, *parts: object) -> int:
    payload = "|".join(map(str, (seed, *parts))).encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def accepted_prompts(stories: pd.DataFrame, hydrated: dict[str, dict]) -> set[str]:
    """Prompts eligible for language scoring.

    A prompt qualifies when the detector called both sides English, both sides were
    actually rehydrated, and the response is neither a WildGuard refusal nor a soft
    refusal. Refusals are removed because a refusal's vocabulary describes the
    refusal, not the story that was asked for.

    A record whose `prompt` or `response` is `None` was not fully rehydrated and is
    dropped. It is not treated as an empty text: scoring a missing response as ""
    would add a zero-length document to the population instead of removing an
    unobserved one.
    """
    english = stories.loc[stories.prompt_english & stories.response_english, "prompt_id"]
    refused = set(stories.loc[stories.response_refusal, "prompt_id"])
    accepted = set()
    for prompt_id in english:
        record = hydrated.get(prompt_id)
        if record is None or prompt_id in refused:
            continue
        if record.get("prompt") is None or record.get("response") is None:
            continue
        if is_soft_refusal(record["response"]):
            continue
        accepted.add(prompt_id)
    return accepted


def hydration_coverage(nodes: pd.DataFrame, hydrated: dict[str, dict]) -> dict:
    """How much of the released tree population this rehydration actually covers."""
    present = nodes.prompt_id.isin(hydrated)
    per_tree = present.groupby(nodes.tree_id).agg(["sum", "size"])
    complete = per_tree.index[per_tree["sum"] == per_tree["size"]]
    return {
        "released_nodes": int(len(nodes)),
        "hydrated_nodes": int(present.sum()),
        "released_trees": int(len(per_tree)),
        "complete_trees": int(len(complete)),
        "complete_tree_ids": set(complete),
    }


def require_complete_trees(nodes: pd.DataFrame, hydrated: dict[str, dict],
                           on_missing: str = "error") -> tuple[pd.DataFrame, dict]:
    """Restrict the node table to trees whose every released node was rehydrated.

    Root-versus-leaf and per-tree specificity are statements about a tree's shape.
    A tree missing one node is not a smaller tree: its root or its deepest leaf may
    be the missing node, and treating a surviving descendant as a root would invent
    a comparison the data does not support.

    `on_missing="error"` is the strict setting and fails when any released node is
    absent. `on_missing="restrict"` keeps only complete trees,
    warns, and reports coverage so the reduced population is visible next to the
    result.
    """
    if on_missing not in ("error", "restrict"):
        raise ValueError(f"on_missing must be 'error' or 'restrict', not {on_missing!r}")
    coverage = hydration_coverage(nodes, hydrated)
    incomplete = coverage["released_trees"] - coverage["complete_trees"]
    if incomplete:
        message = (
            f"{coverage['hydrated_nodes']:,} of {coverage['released_nodes']:,} released "
            f"nodes were rehydrated; {incomplete:,} of {coverage['released_trees']:,} trees "
            "are incomplete. These statistics need the full snapshot. Pass "
            "on_missing='restrict' to run on the complete trees only, and report the "
            "coverage alongside the result."
        )
        if on_missing == "error":
            raise ValueError(message)
        warnings.warn(message, UserWarning, stacklevel=3)
    return nodes.loc[nodes.tree_id.isin(coverage["complete_tree_ids"])], coverage


def changed_depth(tree_nodes: list[str], parent_of: dict[str, str],
                  signature: dict[str, str]) -> dict[str, int]:
    """Depth counting only edits, so an exact resend does not advance it."""
    depths: dict[str, int] = {}
    for prompt_id in tree_nodes:
        if prompt_id not in signature:
            raise KeyError(
                f"{prompt_id} has no rehydrated text, so its depth is undefined. "
                "Restrict to complete trees with require_complete_trees first."
            )
        parent = parent_of.get(prompt_id)
        if parent is None:
            depths[prompt_id] = 0
        elif parent not in signature:
            raise KeyError(
                f"parent {parent} of {prompt_id} has no rehydrated text. A surviving "
                "descendant is not a root; restrict to complete trees first."
            )
        else:
            depths[prompt_id] = depths[parent] + int(signature[prompt_id] != signature[parent])
    return depths


def score_documents(documents: dict[object, dict[str, list[str]]]) -> dict[str, dict[str, float]]:
    """Score each document against the pooled background of all documents.

    Word scores are computed per tree, so a word is distinctive relative to the tree
    it appears in. `literal` averages over a document's distinct word types;
    `zhang` averages over its tokens.
    """
    pooled: Counter[str] = Counter()
    per_tree: dict[object, Counter[str]] = {}
    for tree_id, texts in documents.items():
        local = Counter(word for words in texts.values() for word in words)
        per_tree[tree_id] = local
        pooled.update(local)
    background_total = sum(pooled.values())

    scores: dict[str, dict[str, float]] = {}
    for tree_id, texts in documents.items():
        local = per_tree[tree_id]
        local_total = sum(local.values())
        word_score = {
            word: math.log((count / local_total) / (pooled[word] / background_total))
            for word, count in local.items()
        }
        for prompt_id, words in texts.items():
            if words:
                scores[prompt_id] = {
                    "literal": statistics.fmean(word_score[w] for w in set(words)),
                    "zhang": statistics.fmean(word_score[w] for w in words),
                }
    return scores


def rank_biserial(differences: np.ndarray) -> float:
    nonzero = differences[differences != 0]
    if not len(nonzero):
        return 0.0
    ranks = pd.Series(np.abs(nonzero)).rank().to_numpy()
    positive = ranks[nonzero > 0].sum()
    negative = ranks[nonzero < 0].sum()
    return float((positive - negative) / (positive + negative))


def _tree_index(nodes: pd.DataFrame, edges: pd.DataFrame, hydrated: dict[str, dict]):
    parent_of = dict(zip(edges.child_prompt_id, edges.parent_prompt_id))
    ordered = nodes.sort_values(["tree_id", "order_index"])
    by_tree: dict[object, list[str]] = defaultdict(list)
    for row in ordered.itertuples(index=False):
        by_tree[row.tree_id].append(row.prompt_id)
    signature = {
        prompt_id: edit_signature(hydrated[prompt_id]["prompt"] or "")
        for prompt_id in ordered.prompt_id if prompt_id in hydrated
    }
    return parent_of, by_tree, signature


def root_versus_leaf(stories, nodes, edges, hydrated, on_missing: str = "error") -> dict:
    """Compare the root prompt of a tree with its deepest edited leaf.

    One tree is drawn per user, among trees holding at least five eligible nodes
    and spanning at least two edits. Scores use each text's first 100 words.

    Only trees whose every released node was rehydrated are considered, since the
    root and the deepest leaf are defined by the released topology. See
    `require_complete_trees` for `on_missing`; the returned `coverage` records how
    much of the release the run actually saw.
    """
    nodes, coverage = require_complete_trees(nodes, hydrated, on_missing)
    edges = edges.loc[edges.tree_id.isin(coverage["complete_tree_ids"])]
    accepted = accepted_prompts(stories, hydrated)
    parent_of, by_tree, signature = _tree_index(nodes, edges, hydrated)
    order = dict(zip(nodes.prompt_id, nodes.order_index))
    user_of = dict(zip(stories.prompt_id, stories.inferred_user_id))

    depths, children, eligible = {}, {}, {}
    for tree_id, prompt_ids in by_tree.items():
        depths[tree_id] = changed_depth(prompt_ids, parent_of, signature)
        kids: dict[str, list[str]] = defaultdict(list)
        for prompt_id in prompt_ids:
            parent = parent_of.get(prompt_id)
            if parent in signature:
                kids[parent].append(prompt_id)
        children[tree_id] = kids
        here = [p for p in prompt_ids if p in accepted]
        if len(here) >= MIN_NODES:
            span = [depths[tree_id][p] for p in here]
            if max(span) - min(span) >= MIN_CHANGED_DEPTH:
                eligible[tree_id] = here

    trees_by_user: dict[str, list] = defaultdict(list)
    for tree_id, here in eligible.items():
        trees_by_user[str(user_of[by_tree[tree_id][0]])].append(tree_id)
    selected = {
        random.Random(stable_seed(SEED, "depth-tree", user)).choice(sorted(tree_ids))
        for user, tree_ids in sorted(trees_by_user.items())
    }

    sides = {}
    for side in ("prompt", "response"):
        documents = {
            tree_id: {p: specificity_tokens(analysis_text(hydrated[p], side))[:WORD_CAP]
                      for p in eligible[tree_id]}
            for tree_id in selected
        }
        sides[side] = score_documents(documents)

    rows = []
    for tree_id in selected:
        here = set(eligible[tree_id])
        roots = [p for p in here if parent_of.get(p) not in signature]
        leaves = [p for p in here if not children[tree_id][p]]
        if len(roots) != 1 or not leaves:
            continue
        leaf = sorted(leaves, key=lambda p: (-depths[tree_id][p], -order[p], p))[0]
        root = roots[0]
        if depths[tree_id][leaf] - depths[tree_id][root] < MIN_CHANGED_DEPTH:
            continue
        if all(p in sides[s] for s in sides for p in (root, leaf)):
            rows.append({
                "tree_id": tree_id,
                **{f"root_{s}": sides[s][root]["zhang"] for s in sides},
                **{f"leaf_{s}": sides[s][leaf]["zhang"] for s in sides},
            })

    paired = pd.DataFrame(rows)
    results = {}
    for side in ("prompt", "response"):
        difference = (paired[f"leaf_{side}"] - paired[f"root_{side}"]).to_numpy()
        results[side] = {
            "pairs": len(paired),
            "median change": float(np.median(difference)),
            "rank-biserial": rank_biserial(difference),
            "p": float(wilcoxon(difference, zero_method="wilcox").pvalue),
        }
    return {
        "paired": paired,
        "selected_trees": len(selected),
        "results": pd.DataFrame(results).T,
        "coverage": {k: v for k, v in coverage.items() if k != "complete_tree_ids"},
    }


def prompt_story_correlation(stories, nodes, hydrated, word_cap=None,
                             on_missing: str = "error") -> dict:
    """Correlate a tree's prompt specificity with its story specificity.

    Five eligible nodes are sampled per tree and one tree per user, so the unit is
    a tree rather than a prompt. The confidence interval resamples whole users.

    A tree with unrehydrated nodes would be sampled from a different population
    than the one the release describes, so only complete trees are used. See
    `require_complete_trees` for `on_missing`.
    """
    nodes, coverage = require_complete_trees(nodes, hydrated, on_missing)
    accepted = accepted_prompts(stories, hydrated)
    user_of = dict(zip(stories.prompt_id, stories.inferred_user_id))
    ordered = nodes.sort_values(["tree_id", "order_index"])
    by_tree: dict[object, list[str]] = defaultdict(list)
    for row in ordered.itertuples(index=False):
        if row.prompt_id in accepted:
            by_tree[row.tree_id].append(row.prompt_id)

    eligible = {t: p for t, p in by_tree.items() if len(p) >= NODES_PER_TREE}
    trees_by_user: dict[str, list] = defaultdict(list)
    for tree_id, prompt_ids in eligible.items():
        trees_by_user[str(user_of[prompt_ids[0]])].append(tree_id)
    selected = {
        random.Random(stable_seed(SEED, "tree", user)).choice(sorted(tree_ids))
        for user, tree_ids in sorted(trees_by_user.items())
    }
    sampled = {
        tree_id: sorted(random.Random(
            stable_seed(SEED, "documents", NODES_PER_TREE, tree_id)
        ).sample(eligible[tree_id], NODES_PER_TREE))
        for tree_id in sorted(selected)
    }

    sides = {}
    for side in ("prompt", "response"):
        documents = {
            tree_id: {p: specificity_tokens(analysis_text(hydrated[p], side))[:word_cap]
                      if word_cap else specificity_tokens(analysis_text(hydrated[p], side))
                      for p in prompt_ids}
            for tree_id, prompt_ids in sampled.items()
        }
        # One score per tree: the mean over that tree's sampled documents.
        scored = score_documents(documents)
        sides[side] = {
            tree_id: statistics.fmean(scored[p]["zhang"] for p in prompt_ids if p in scored)
            for tree_id, prompt_ids in sampled.items()
        }

    rows = [{"tree_id": t, "user_id": str(user_of[sampled[t][0]]),
             "prompt": sides["prompt"][t], "response": sides["response"][t]}
            for t in sampled]
    frame = pd.DataFrame(rows)

    by_user: dict[str, list[int]] = defaultdict(list)
    for position, user in enumerate(frame.user_id):
        by_user[user].append(position)
    users = sorted(by_user)
    generator = np.random.default_rng(
        stable_seed(SEED, f"k{NODES_PER_TREE}_one_tree_per_user", "zhang_document_mean")
    )
    estimates = []
    for _ in range(BOOTSTRAPS):
        positions = [p for user in generator.choice(users, size=len(users), replace=True)
                     for p in by_user[str(user)]]
        drawn = frame.iloc[positions]
        estimates.append(float(spearmanr(drawn.prompt, drawn.response).statistic))
    low, high = np.percentile(estimates, [2.5, 97.5])
    return {
        "trees": len(frame),
        "rho": float(spearmanr(frame.prompt, frame.response).statistic),
        "ci": (float(low), float(high)),
        "coverage": {k: v for k, v in coverage.items() if k != "complete_tree_ids"},
    }
