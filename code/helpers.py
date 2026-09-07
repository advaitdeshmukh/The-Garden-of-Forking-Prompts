from __future__ import annotations

import gzip
import json
import re
import warnings
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Iterator


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_FILES = {
    "wildstories": "wildstories.jsonl.gz",
    "trees": "wildedits_trees.jsonl.gz",
    "nodes": "wildedits_nodes.jsonl.gz",
    "edges": "wildedits_edges.jsonl.gz",
    "actions": "wildedits_actions.jsonl.gz",
}
DATASET_NAME = "yuntian-deng/WildChat-4.8M-Full"
# The revision the release was verified against. See README.
DATASET_REVISION = "eedff4afb0239e69217ffd1c276e2ba45bbfdd45"
TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Control characters dropped from source text, keeping tab and newline.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Token:
    text: str
    comparison: str
    start: int
    end: int


def normalize_text(value: object) -> str | None:
    """Normalize source text the way the corpus was built.

    Line endings fold to `\n`, control characters other than tab and newline are
    dropped, and the result is stripped. Prompts and responses were normalized this
    way before annotation, so rehydrating without it produces text that differs from
    what the released span ids were assigned against: the diff then draws different
    span boundaries and an action resolves to a longer or shorter region.

    `None` stays `None`, keeping an unanswered prompt distinct from an empty one.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL_RE.sub("", text).strip()


def normalize_prompt_id(value: object) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    return value[:-2] if value.endswith(".0") else value


def iter_jsonl_gz(path: str | Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def iter_table(name: str, data_dir: str | Path = DATA_DIR) -> Iterator[dict]:
    return iter_jsonl_gz(Path(data_dir) / DATA_FILES[name])


def read_table(
    name: str,
    data_dir: str | Path = DATA_DIR,
    limit: int | None = None,
) -> list[dict]:
    rows = []
    for row in iter_table(name, data_dir):
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _reply_to(turns: list[dict], index: int) -> str | None:
    """The assistant turn answering the user turn at `index`, or None.

    The scan stops at the next user turn. A prompt whose turn identifier is not
    carried by any assistant turn and that is followed directly by another user
    turn went unanswered, and must not borrow the answer to a later prompt. A
    `None` response therefore means "no response in this record", which is not the
    same as an empty one; callers that score response text have to drop it rather
    than treat it as the empty string.
    """
    for later in turns[index + 1 :]:
        role = str(later.get("role", "")).lower()
        if role == "user":
            return None
        if role == "assistant":
            return later.get("content")
    return None


def rehydrate_from_records(
    records: Iterable[dict],
    prompt_ids: Iterable[object],
    strict: bool = False,
) -> dict[str, dict]:
    wanted = {normalize_prompt_id(value) for value in prompt_ids}
    wanted.discard("")
    found = {}
    for record_index, record in enumerate(records):
        turns = record.get("conversation") or []
        timestamp = record.get("timestamp")
        hashed_ip = record.get("hashed_ip")
        assistants = {
            normalize_prompt_id(turn.get("turn_identifier")): turn.get("content")
            for turn in turns
            if str(turn.get("role", "")).lower() == "assistant"
        }
        for index, turn in enumerate(turns):
            if str(turn.get("role", "")).lower() != "user":
                continue
            key = normalize_prompt_id(turn.get("turn_identifier"))
            if key not in wanted or key in found:
                continue
            response = assistants.get(key)
            if response is None:
                response = _reply_to(turns, index)
            found[key] = {
                "prompt_id": key,
                "conversation_id": str(record.get("conversation_hash", "")),
                "prompt": normalize_text(turn.get("content")),
                "response": normalize_text(response),
                "timestamp": timestamp,
                "record_index": record_index,
                "turn_index": index,
                "hashed_ip": hashed_ip,
            }
        if len(found) == len(wanted):
            break
    missing = wanted - found.keys()
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        message = (
            f"Rehydrated {len(found)} of {len(wanted)} prompt_id values; "
            f"{len(missing)} are absent from this WildChat source. Results are incomplete. "
            "Released trees and edges were built from the full snapshot. Recompute "
            "clustering, tree construction, and pruning before treating a subset as a "
            f"standalone topology. Missing examples: {sample}"
        )
        if strict:
            raise KeyError(message)
        warnings.warn(message, UserWarning, stacklevel=2)
    return found


def rehydrate_from_wildchat(
    prompt_ids: Iterable[object],
    dataset_name: str = DATASET_NAME,
    split: str = "train",
    token: str | bool | None = None,
    strict: bool = False,
    revision: str | None = DATASET_REVISION,
) -> dict[str, dict]:
    """Restore prompt and response text from WildChat.

    `revision` pins the source snapshot and defaults to the revision this release
    was verified against. Pass `None` to take whatever the dataset currently serves,
    which may have had conversations removed since.
    """
    from datasets import load_dataset

    if dataset_name != DATASET_NAME:
        warnings.warn(
            "Using an alternate WildChat source. Rehydrated results may be incomplete, "
            "and the released trees and edges still describe the full release snapshot. "
            "Recompute clustering, tree construction, and pruning before treating the "
            "subset as a standalone topology.",
            UserWarning,
            stacklevel=2,
        )
    options = {"split": split, "streaming": True}
    if token is not None:
        options["token"] = token
    if revision is not None:
        options["revision"] = revision
    records = load_dataset(dataset_name, **options)
    return rehydrate_from_records(records, prompt_ids, strict=strict)


def pipeline_inputs(hydrated: dict[str, dict]) -> dict[str, dict]:
    """Turn rehydrated records into the three inputs `pipeline.py` asks for.

    Returns `prompts`, `timestamps`, `source_order` and `hashed_ip`, keyed by
    `prompt_id`:

        hydrated = rehydrate_from_wildchat(stories.prompt_id)
        parts = pipeline_inputs(hydrated)
        built = pipeline.build_trees(parts["prompts"], parts["timestamps"],
                                     parts["source_order"])
        users = pipeline.merge_users_by_cluster(built["clusters"], parts["hashed_ip"])

    `source_order` breaks timestamp ties, and it is the order the records arrive in:
    `(record position in the stream, turn position in the record)`.

    This assumes the records arrive in dataset order.

    `hashed_ip` is what `merge_users_by_cluster` needs to derive inferred users;
    the released tables carry `inferred_user_id` in its place.
    """
    def stream_position(record):
        index = record.get("record_index")
        turn = record.get("turn_index")
        return (index is None, index or 0, turn or 0, str(record["prompt_id"]))

    ordered = sorted(hydrated.values(), key=stream_position)
    return {
        "prompts": {r["prompt_id"]: r["prompt"] for r in ordered},
        "timestamps": {r["prompt_id"]: parse_timestamp(r.get("timestamp")) for r in ordered},
        "source_order": {r["prompt_id"]: i for i, r in enumerate(ordered)},
        "hashed_ip": {r["prompt_id"]: r["hashed_ip"] for r in ordered
                      if r.get("hashed_ip")},
    }


def parse_timestamp(value: object) -> float | None:
    """WildChat timestamp to epoch milliseconds, or None when absent.

    Naive values are read as UTC. See `pipeline.parse_timestamp_ms` for why: local
    resolution makes the ordering depend on the machine's timezone.
    """
    from datetime import datetime, timezone

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000


def _tokenize(text: str) -> list[Token]:
    return [
        Token(match.group(0), match.group(0).lower(), match.start(), match.end())
        for match in TOKEN_RE.finditer(text or "")
    ]


def _make_opcodes(
    parent_tokens: list[Token],
    child_tokens: list[Token],
) -> list[tuple[str, int, int, int, int]]:
    parent = [token.comparison for token in parent_tokens]
    child = [token.comparison for token in child_tokens]
    prefix = 0
    while prefix < min(len(parent), len(child)) and parent[prefix] == child[prefix]:
        prefix += 1
    parent_suffix = len(parent)
    child_suffix = len(child)
    while (
        parent_suffix > prefix
        and child_suffix > prefix
        and parent[parent_suffix - 1] == child[child_suffix - 1]
    ):
        parent_suffix -= 1
        child_suffix -= 1
    opcodes = []
    if prefix:
        opcodes.append(("equal", 0, prefix, 0, prefix))
    parent_middle = parent[prefix:parent_suffix]
    child_middle = child[prefix:child_suffix]
    if parent_middle or child_middle:
        matcher = SequenceMatcher(None, parent_middle, child_middle, autojunk=False)
        for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
            opcodes.append(
                (tag, prefix + a_start, prefix + a_end, prefix + b_start, prefix + b_end)
            )
    if parent_suffix < len(parent) or child_suffix < len(child):
        opcodes.append(("equal", parent_suffix, len(parent), child_suffix, len(child)))
    return opcodes or [("equal", 0, 0, 0, 0)]


def _merge_adjacent_changes(
    opcodes: list[tuple[str, int, int, int, int]],
    child_text: str,
    child_tokens: list[Token],
    max_gap_tokens: int = 3,
) -> list[tuple[str, int, int, int, int]]:
    merged = []
    for opcode in opcodes:
        tag, a_start, a_end, b_start, b_end = opcode
        previous_change = merged[-2] if len(merged) >= 2 else None
        both_inserts = previous_change is not None and previous_change[0] == tag == "insert"
        both_deletes = previous_change is not None and previous_change[0] == tag == "delete"
        can_merge = (
            tag != "equal"
            and len(merged) >= 2
            and merged[-1][0] == "equal"
            and merged[-2][0] != "equal"
            and not both_inserts
            and not both_deletes
            and merged[-1][2] - merged[-1][1] <= max_gap_tokens
            and merged[-1][4] - merged[-1][3] <= max_gap_tokens
        )
        if not can_merge:
            merged.append(opcode)
            continue
        previous = merged[-2]
        gap_start = child_tokens[previous[4] - 1].end if previous[4] > 0 else 0
        gap_end = child_tokens[b_start].start if b_start < len(child_tokens) else len(child_text)
        if "\n" in child_text[gap_start:gap_end]:
            merged.append(opcode)
            continue
        merged.pop()
        merged.pop()
        new_tag = (
            "replace"
            if a_end > previous[1] and b_end > previous[3]
            else "delete"
            if a_end > previous[1]
            else "insert"
        )
        merged.append((new_tag, previous[1], a_end, previous[3], b_end))
    return merged


PREVIEW_MAX_CHARS = 180


def _compact_preview(text: str, max_chars: int = PREVIEW_MAX_CHARS) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value if len(value) <= max_chars else value[: max_chars - 3].rstrip() + "..."


def _span(
    span_id: str,
    side: str,
    text: str,
    tokens: list[Token],
    start: int,
    end: int,
) -> dict:
    char_start = tokens[start].start
    char_end = tokens[end - 1].end
    value = text[char_start:char_end]
    return {
        "span_id": span_id,
        "side": side,
        "token_start": start,
        "token_end": end,
        "char_start": char_start,
        "char_end": char_end,
        "text": value,
        "preview": _compact_preview(value),
    }


def build_span_index(
    parent_prompt: str,
    child_prompt: str,
) -> dict[str, dict[str, dict]]:
    parent_tokens = _tokenize(parent_prompt)
    child_tokens = _tokenize(child_prompt)
    opcodes = _merge_adjacent_changes(
        _make_opcodes(parent_tokens, child_tokens),
        child_prompt,
        child_tokens,
    )
    removed = {}
    added = {}
    for tag, a_start, a_end, b_start, b_end in opcodes:
        if tag != "equal" and a_end > a_start:
            span_id = f"R{len(removed) + 1}"
            removed[span_id] = _span(
                span_id, "removed", parent_prompt, parent_tokens, a_start, a_end
            )
        if tag != "equal" and b_end > b_start:
            span_id = f"A{len(added) + 1}"
            added[span_id] = _span(
                span_id, "added", child_prompt, child_tokens, b_start, b_end
            )
    return {"removed": removed, "added": added}


# Edit-volume diff.
EDIT_STATS_WORD_RE = re.compile(r"[A-Za-z0-9]+")
MAX_MATCHER_TOKENS = 4096
MAX_MATCHER_PRODUCT = 1_500_000


def compact_len(text: str) -> int:
    """Character length after collapsing runs of whitespace."""
    return len(re.sub(r"\s+", " ", text or "").strip())


def pair_edit_chars(parent_prompt: str, child_prompt: str) -> tuple[int, int]:
    """Characters added and removed between two prompts.

    Produces the `added_chars` / `removed_chars` behind the edit-volume figure.
    """
    def spans(text):
        return [(m.group(0).lower(), m.start(), m.end())
                for m in EDIT_STATS_WORD_RE.finditer(text or "")]

    parent_spans, child_spans = spans(parent_prompt), spans(child_prompt)
    parent = [w for w, _, _ in parent_spans]
    child = [w for w, _, _ in child_spans]
    prefix, limit = 0, min(len(parent), len(child))
    while prefix < limit and parent[prefix] == child[prefix]:
        prefix += 1
    parent_end, child_end = len(parent), len(child)
    while (parent_end > prefix and child_end > prefix
           and parent[parent_end - 1] == child[child_end - 1]):
        parent_end -= 1
        child_end -= 1
    parent_middle, child_middle = parent[prefix:parent_end], child[prefix:child_end]
    if not parent_middle and not child_middle:
        return 0, 0

    def measure(span_list, text, start, end):
        if end <= start:
            return 0
        return compact_len(text[span_list[start][1]:span_list[end - 1][2]])

    if (len(parent_middle) + len(child_middle) > MAX_MATCHER_TOKENS
            or len(parent_middle) * len(child_middle) > MAX_MATCHER_PRODUCT):
        return (measure(child_spans, child_prompt, prefix, child_end),
                measure(parent_spans, parent_prompt, prefix, parent_end))

    added = removed = 0
    matcher = SequenceMatcher(None, parent_middle, child_middle, autojunk=True)
    for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if tag == "equal" or (a_end <= a_start and b_end <= b_start):
            continue
        removed += measure(parent_spans, parent_prompt, prefix + a_start, prefix + a_end)
        added += measure(child_spans, child_prompt, prefix + b_start, prefix + b_end)
    return added, removed


def resolve_action_spans(
    action: dict,
    span_index: dict[str, dict[str, dict]],
    on_missing: str = "skip",
) -> dict:
    """Look up an action's marked spans and join them into text.

    Two joined forms are returned for each side:

    * `*_preview_text` joins the whitespace-collapsed previews, truncated at
      `PREVIEW_MAX_CHARS` characters per span.
    * `*_full_text` joins the complete span text.

    Ids resolve on their own side only. Three kinds of id do not resolve: a `"?"`
    placeholder, a cross-side reference, and anything absent because the rehydrated
    endpoints differ from the release snapshot. Each is skipped by default, keeping
    the action's remaining spans. Pass `on_missing="error"` to raise `KeyError`
    instead.

    `missing_span_ids` is empty exactly when every id resolved, so a caller wanting
    the strict population can filter on it without catching an exception.
    """
    if on_missing not in ("skip", "error"):
        raise ValueError(f"on_missing must be 'skip' or 'error', not {on_missing!r}")

    missing: list[str] = []

    def resolve(ids: list[str], side: str) -> list[dict]:
        values = []
        for raw_id in ids:
            span_id = str(raw_id).lstrip("+-")
            span = span_index[side].get(span_id)
            if span is None:
                if on_missing == "error":
                    raise KeyError(
                        f"Span {span_id} is absent from the reconstructed {side} side"
                    )
                missing.append(str(raw_id))
                continue
            values.append(span)
        return values

    def join(spans: list[dict], field: str) -> str:
        return " ".join(
            span[field].strip() for span in spans if span[field].strip()
        )

    removed = resolve(action.get("removed_span_ids") or [], "removed")
    added = resolve(action.get("added_span_ids") or [], "added")
    return {
        "removed_spans": removed,
        "added_spans": added,
        "removed_preview_text": join(removed, "preview"),
        "added_preview_text": join(added, "preview"),
        "removed_full_text": join(removed, "text"),
        "added_full_text": join(added, "text"),
        "missing_span_ids": missing,
    }
