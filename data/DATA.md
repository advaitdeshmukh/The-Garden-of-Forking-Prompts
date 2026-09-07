# Data

The release contains identifiers and derived labels, but no WildChat prompt, response, or extracted-span text. Span text is recovered locally by rehydrating the prompts from WildChat and rebuilding each pair's diff, so a reader without WildChat access holds no prompt content.

## Canonical identifiers

### `prompt_id`

Primary key for a user prompt. It is the normalized string form of `turn_identifier` on the source WildChat user turn.

Rehydration searches user turns by `turn_identifier == prompt_id`.

Do not parse `prompt_id` as an integer. Keep the normalized source value as the string it ships as.

When rehydrating from a WildChat subset, unmatched IDs are expected. Labels for matched prompts remain usable, but the released tree topology describes the full snapshot.

**A subset does not give you a smaller version of the same clustering.** Clusters are
connected components of a similarity graph, and connectivity is not local: if `A`-`B`
and `B`-`C` both clear the 0.5 threshold but `A`-`C` does not, the three are one
cluster only because `B` holds them together. Remove `B` and you get two clusters
where the release has one. That propagates. Different clusters mean different prompt
pools for parent selection, different parents, and different trees after pruning.
Inferred users change too, since they are defined by hashed IPs co-occurring within a
cluster.

Dehydration plus upstream removals cause this, and the release cannot repair it.
Treat the published `tree_id` / `source_cluster_id` / `inferred_user_id` as fixed.
Do not compare a locally reconstructed topology against them unless every prompt
rehydrated.

### `conversation_id`

The source WildChat `conversation_hash`. It is included where cross-conversation behavior must be reconstructed. It is supporting metadata and a consistency check; it is not part of the primary key.

### Derived release identifiers

- `tree_id`: the construction pipeline's tree ID, kept as assigned. Integer, with gaps: pruning also numbered the singleton components, and those are not released.
- `source_cluster_id`: the construction pipeline's cluster ID, kept as assigned.
- `inferred_user_id`: the construction pipeline's inferred-user ID, kept as assigned. Hashed IPs that co-occur in a cluster are merged into one inferred user; the IDs are dense integers with no identity content.
- `pair_id`: deterministic ID for one directed `(parent_prompt_id, child_prompt_id)` edge.

## `wildstories.jsonl.gz`

One row per WildStories prompt.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `prompt_id` | string | yes | Canonical WildChat user-turn ID. |
| `conversation_id` | string | yes | Source conversation hash. |
| `inferred_user_id` | string | yes | Inferred-user group, covering singleton components as well as trees. Shares its namespace with `wildedits_trees.jsonl.gz`. |
| `source_toxic` | boolean | yes | Source WildChat toxicity flag. |
| `mode` | enum | yes | `prose`, `roleplay`, `script`, or `narration`. |
| `instructions` | boolean | yes | Structure label. |
| `jailbreak` | boolean | yes | Structure label. |
| `story_stub` | boolean | yes | Structure label. |
| `premise` | boolean | yes | Structure label. |
| `story_summary` | boolean | yes | Structure label. |
| `example` | boolean | yes | Structure label. |
| `sexual_content` | boolean | yes | Final explicit-content classifier label. |
| `body_humor` | boolean | yes | Final explicit-content classifier label. |
| `fetish_content` | boolean | yes | Final explicit-content classifier label. |
| `any_sensitive` | boolean | yes | **Derived**: exactly `sexual_content OR body_humor OR fetish_content`, kept as a shorthand for those three labels. |
| `response_refusal` | boolean | yes | Final WildGuard label for the associated assistant response. |
| `prompt_english` | boolean | yes | Language detector called the prompt English, or it carried enough English function words to be rescued. Used to build the specificity corpus. |
| `response_english` | boolean | yes | The same verdict for the response. |

## `wildedits_trees.jsonl.gz`

One row per final non-singleton pruned edit tree. Singleton components are part of WildStories but not WildEdits.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tree_id` | integer | yes | Tree ID assigned by the construction pipeline. |
| `source_cluster_id` | string | yes | Opaque source near-duplicate component ID. |
| `inferred_user_id` | string | yes | Inferred-user grouping ID. |
| `root_prompt_id` | string | yes | Root node's `prompt_id`. |
| `size` | integer | yes | Number of prompt nodes. Minimum 2. |
| `max_depth` | integer | yes | Maximum root-relative edge depth. |

There are 24,291 rows in the canonical snapshot.

## `wildedits_nodes.jsonl.gz`

One row per prompt node belonging to a non-singleton WildEdits tree.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tree_id` | integer | yes | Foreign key to `wildedits_trees`. |
| `prompt_id` | string | yes | Foreign key to `wildstories`. |
| `conversation_id` | string | yes | Source conversation hash. |
| `order_index` | integer | yes | Chronological order within the final pruned tree. |
| `depth` | integer | yes | Distance from the root. |

### Added for the refusal/resend analysis

| Field | Type | Description |
| --- | --- | --- |
| `minutes_from_root` | float | Minutes elapsed since the earliest prompt in the tree. Differences between two nodes of the same tree give the gap between them. Absolute timestamps are not published. |
| `normalized_text_id` | integer | Equal for two prompts exactly when their normalized token sequences are equal, after lowercasing and keeping word characters only. |

`normalized_text_id` is an equivalence id. It shows which prompts match each other, and it cannot be tested against a guessed prompt. Two prompts sharing an id count as a normalized-text resend in the refusal analysis.

## `wildedits_edges.jsonl.gz`

One row for every retained edge in non-singleton trees, including edges with no substantive marked diff.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tree_id` | integer | yes | Foreign key to `wildedits_trees`. |
| `parent_prompt_id` | string | yes | Earlier prompt. |
| `child_prompt_id` | string | yes | Later prompt. |
| `parent_similarity` | number | yes | Full-prompt Jaccard used for parent selection/pruning; at least 0.5. |
| `delta_minutes` | number | yes | Child time minus parent time. |
| `same_conversation` | boolean | yes | Whether both prompts have the same `conversation_id`. |
| `edge_status` | enum | yes | `in_scope`, `out_of_scope`, or `no_marked_diff`. |
| `pair_id` | string/null | yes | Present for classified edges; null for `no_marked_diff`. |

The complete retained topology has 181,505 edges: 100,200 in-scope classified edges, 437 out-of-scope classified edges, and 80,868 no-marked-diff edges.

## `wildedits_actions.jsonl.gz`

One row per extracted action on an in-scope classified pair. Direction-invalid actions remain present and carry `direction_valid=false`, and are dropped only where a direction is the unit being counted:

| Analysis | Direction-invalid actions |
| --- | --- |
| Direction × target counts (Table 1) | dropped |
| Prompt structure × direction counts (Table 4) | dropped |
| Target PMI, target transitions | retained; the target label is valid even where the direction is not |
| Prompt-attribute × target PMI, word-level PMI | retained |

### Span reconstruction coverage

Span ids are resolved against a diff rebuilt from the rehydrated endpoints.
`resolve_action_spans` skips an id the rebuilt diff does not produce,
keeps the action's remaining spans, and lists what it skipped in `missing_span_ids`.
Pass `on_missing="error"` to raise instead.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `pair_id` | string | yes | Foreign key to `wildedits_edges`. |
| `action_index` | integer | yes | Zero-based index within a pair. |
| `target` | enum | yes | One of the fourteen edit targets. |
| `direction` | enum/null | yes | `ADD`, `REMOVE`, `CHANGE`, `EXTEND`, or null when invalid. |
| `direction_valid` | boolean | yes | Whether span sides and extension flag define a valid direction. |
| `is_extension` | boolean | yes | Extension label from edit classification. |
| `removed_span_ids` | array | yes | References into the rebuilt diff's removed side. `"?"` marks a reference the annotator's output did not parse into an id. |
| `added_span_ids` | array | yes | References into the rebuilt diff's added side. Same `"?"` convention as `removed_span_ids`. |

### Direction derivation

- removed and added spans: `CHANGE`;
- removed spans only: `REMOVE`;
- added spans only and `is_extension=false`: `ADD`;
- added spans only and `is_extension=true`: `EXTEND`;
- any other combination, including `is_extension=true` with removed spans: null and invalid.

Canonical totals are 165,932 actions across 100,107 pairs. The remaining 93 in-scope pairs have no extracted actions. Of the actions, 165,846 have valid directions and 86 do not.

## Population rules

- **WildStories:** all 275,635 Qwen-positive prompts, including singleton clusters.
- **Final component:** any connected component remaining after pruning parent edges below 0.5; 94,130 total, including 69,839 singletons.
- **WildEdits tree:** a final component with at least two nodes; 24,291 total.
- **Singleton component:** a story prompt in no edit tree; 69,839 total. Present in `wildstories.jsonl.gz` but not in `wildedits_nodes.jsonl.gz`. `analysis_lib.response_opportunities` keeps them in the denominator.
- **No-marked-diff edge:** retained topology edge whose prompts differ only in ignored/cosmetic ways under the marked-diff procedure; it was not sent for semantic labeling.
- **Classified pair:** retained edge that was sent for semantic labelling and parsed successfully; 100,637 total.
- **In-scope pair:** classified pair labeled as a story-oriented revision; 100,200 total.
- **Out-of-scope pair:** classified pair rejected by the scope decision; 437 total.
- **Extracted action:** an action attached to an in-scope pair; 165,932 total.
- **Direction-valid action:** extracted action whose span sides and extension flag imply one of the four directions; 165,846 total.

## Reported annotation checks

| Task | Sample | Agreement | Model comparison |
| --- | ---: | --- | --- |
| Story-prompt identification | 100 | Krippendorff's α = 0.78 | recall 1.00; precision 0.78 |
| Prompt structure | 100 sampled; 96 complete for evaluation | Krippendorff's α = 0.721 for mode, 0.809 for components | mode recall/precision 0.85/0.75; component macro recall/precision 0.87/0.72 |
| Explicit-content classification | 100 | Krippendorff's α = 0.77 | recall 0.97; precision 0.90 |
| Exact clustering | 50 positive and 50 negative pairs | perfect agreement | perfect agreement with cluster assignments |
| Edit labels | 100 | 91% agreement; Krippendorff's α = 0.75 | accept/reject validation |
