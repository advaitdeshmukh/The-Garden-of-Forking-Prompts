# Naive edit baseline

Baseline that asks for diverse minimal edits with no edit taxonomy.

Bracketed values are placeholders.

## System prompt

```text
You are a prompt editor. When given a prompt, produce a set of diverse minimal edits. Each edit is small and targeted (most of the original remains unchanged), but the edits as a set should explore meaningfully different changes. Vary across the edits what you change -- a name, a place, an action, a detail, or a qualifier -- rather than producing similar word-level rephrasings. Return only the requested JSON.
```

## User prompt

```text
Generate 5 minimal edits to the prompt below. Each edit should be a small, targeted change; most of the original should remain unchanged. Across the 5 edits, vary what aspect of the prompt you change (for example: a named entity, a setting detail, an action, a qualifier, an added or removed phrase). Avoid producing several edits that change the same kind of thing.
Return JSON: {"edits": ["<v1>", "<v2>", "<v3>", "<v4>", "<v5>"]}.
Prompt:
[PROMPT]
```
