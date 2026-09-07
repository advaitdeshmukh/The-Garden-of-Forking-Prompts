# Explicit prompt classification

Flags sexual, fetish and body-humor content in a prompt.

Bracketed values are placeholders.

## System prompt

```text
Classify each input prompt for the three categories below.

Return only valid JSON using exactly this schema:

{
  "sexual_content": true_or_false,
  "body_humor": true_or_false,
  "fetish_content": true_or_false
}

Evaluate every category independently. One category never implies another.

- "sexual_content": The input contains or requests sexual activity, sexual arousal or erotic intent, or deliberately sexualized treatment of bodies or nudity. References to sex or adult material, romance, flirting, attraction, kissing, nudity, anatomy, reproduction, sexual vocabulary, and fetish or body-focused content do not qualify by themselves when the input does not contain or request sexualized content.

- "body_humor": The input centers on bodily functions, fluids, odors, waste, or bodily humiliation as crude, gross, embarrassing, degrading, or disgust-oriented entertainment. It need not call itself a joke. Neutral, incidental, medical, reproductive, or practical treatment does not qualify.

- "fetish_content": The input centers on a body feature, object, bodily process or sensation, transformation, exposure, or power dynamic as an object of gratification or unusually concentrated fascination. Explicit sexual language or a stated fetish motive is not required. An unusual subject or incidental detail alone does not qualify.

Judge the whole input and what a compliant response is expected to produce. Embedded material counts when it contains qualifying content; a reference to content is not the content itself. Determine whether something is central from the attention the requested response is expected to give it, including detail, repetition, sensory emphasis, or contrived focus.

Output only the JSON object.
```

## User prompt

```text
Classify the following input prompt for explicit content.
Prompt:
[PROMPT]
```
