# Evidence-grounded scientific analysis

The daily pipeline calls `scientific_analysis.analyze_papers` after the existing
integrity-checked draft cache and before publication review. It is additive:
previous scientific fields, semantic approval, complete-document reading and
visual fidelity are never rewritten by this stage. Existing sealed editions
remain immutable.

Each paper receives three connected Chinese paragraphs: the most valuable
insight and precise problem; the bottleneck, minimal explanatory idea and
necessary complexity; the article's argument progression, decisive evidence
and unresolved questions. Claim provenance distinguishes explicit author
claims, evidence-backed interpretation and unanswered questions. The Occam lens
is a question about explanatory sufficiency, not a demand that every paper be
simple. Theoretical proof, observation and controlled experiment are supported;
no fabricated discovery history, novelty or causal mechanism is allowed.

Every sentence has exact read-chunk evidence and conditions. Mechanical checks
validate citations, numbers, schema and full facet coverage. A separate model
review must approve every sentence and substantively evaluate all eight facets.
Parent-queue writer/reviewer provenance uses distinct worker identities.

The stage stores drafts and reviews under `data/scientific-analysis/<identity>`.
Identity binds source documents, full reading notes, visual provenance, old
scientific approval, writer settings and the explicit semantic protocol (prompts, schema, source binding, validators and stage execution). Complete
read chunks and notes are provided to the model; duplicate native extraction and
visual transcription are hash-bound rather than repeated. No silent truncation
is permitted. Changing scientific editorial policy invalidates interpretation and independent review only, never upstream reading caches. Rendering functions live separately in `rendering/scientific.py`; their source, typography, label wording and layout are excluded from semantic identity. Changes to evidence validators remain identity-bound, preventing stale approval reuse.

A failure or rejected review produces a visible gap and preserves the qualified
paper's prior approval. One attempt is made per exact source/protocol/config;
a native-only missing-numeric-token response may receive one local correction
in `scientific_writer` ordinal 1. Its precise claim IDs and missing tokens are
provided to the writer, and all other claims plus every claim ID/kind/facet
must remain unchanged. The original and corrected response receipts are both
retained, and the corrected analysis still needs complete independent pixel
review. This correction shares the original issue budget and does not reread
the source. Response schema/semantic failures are local to the paper, while
actual JSON/transport errors retain existing global circuit behavior;
resumable queue requests wait for an independent response, while an expired
request becomes a failed analysis. Under the new native daily-selection
contract, failed/incomplete analysis keeps CORE reusable but blocks complete
selection; the historical strict optional-analysis behavior is unchanged.
Unsupported interpretation is never rendered. Optional settings live under
`sources.scientific_analysis`: enabled (default true), timeout_seconds (180,
bounded to 1–600), max_input_chars (380000). Setting enabled false exposes the
honest missing-analysis label rather than claiming completion.

The editorial, ordinary HTML, Markdown and reading-note renderers display only
exact reviewed and still-source-bound paragraphs. Editorial HTML puts the
insight near the lead, before figures, and expands the deeper argument in a
separate block. Compact actual page/chunk locators retain traceability without
printing a long English quotation list.

## Moderate editorial simplification

Future analysis uses a coherent insight → decisive evidence → limits line, usually
450–850 Chinese characters as guidance rather than a truncation rule. It reads
existing approved fields for context, adds scientific explanation, and avoids
restating problem, method or limitations in several places. The precise problem
gets only the necessary bridging sentence; causal logic, comparison conditions,
important numerical evidence and real unknowns remain. Related facts are joined
in readable paragraphs, limitations are consolidated, and provenance remains
explicit in claim kinds. Independent review checks both source support and this
nonredundant flow. The renderer must never implement prose shortening itself;
changed scientific prose returns through the additive draft/review stage.

### Conservative fingerprint boundary

The semantic fingerprint is default-inclusive, not a hand-maintained list of
semantic functions. It hashes the entire analysis module's parsed code and live
local function definitions. The only excluded top-level declarations are the
presentation labels `LABELS`, display-only `GAP`, and the compatibility wrappers
`analysis_paragraphs` / `analysis_sources`. New local helpers, constants,
validation logic and policy automatically invalidate the analysis stage.

Project symbols imported anywhere in the remaining semantic code are discovered
automatically. Their source and referenced project helper functions/classes and
JSON-like policy constants are recursively included, covering transitive
validator changes. Unfingerprintable project symbols, wildcard imports and
module-valued project imports fail closed instead of being silently skipped.
Presentation wrappers import `rendering/scientific.py` only after the semantic
boundary, so typography, grouping and label wording do not trigger model work.
Regression tests cover a newly added semantic helper, a transitive imported
validator, real prose-policy changes, presentation changes and unchanged full
reading/visual reuse. Renderer code never rewrites approved scientific claims.

### Partial completion is explicit

Stored outcomes distinguish `completion_scope=core_only` from
`core_and_scientific_analysis`. New native selection, completion and ready
gates enforce the required independent science from actual proof, not this
mutable label. A CORE-only item is an intermediate, not a complete daily entry. A failed addition carries bounded diagnostics
and the exact local-repair attempt count; it does not gain a scientific PASS
from the prior CORE approval. Replaying an exact failed attempt does not spawn
another repair. Unsupported prose is never attached to published fields.
