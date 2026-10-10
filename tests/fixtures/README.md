# Public regression fixtures

These five JSON fixtures are deterministic **synthetic substitutions** for
private development inputs. Historical dates in filenames are compatibility
labels, not a claim that these are original trial documents or model answers.
All source prose, captions, author identities, record identities, source hashes,
image hashes, and model-response prose are synthetic. No full paper, original
page image, private filesystem path, production receipt, or user profile is
distributed here. The synthetic content is covered by the repository license.

## What each fixture tests

- `reading_actual_documents_20261009.json.gz` retains only numerical positional
  shape: 16/21/26 pages, 20/39/62 repaired chunks, their lengths and offsets, and
  the corresponding native chunk layout. Newly authored filler occupies each
  exact source span. Synthetic native documents and their derivatives have
  matching identities and recomputed content hashes. They still require
  16/24/30 bounded reading groups, totaling 121 notes and 70 groups
- `reading_pack_audit_20261009.json` is the matching synthetic span-only manifest
  with hashes of the synthetic chunks. It contains no original journal hash
- `native_caption_trial_20261009.json.gz` contains two invented twenty-page
  caption documents. Caption roles, page locations, and numbered-math hints
  exercise late qualitative figures, role diversity, the eight-page limit,
  exact caption spans, and explicit omissions. Source hashes are synthetic;
  they are not receipts proving an original PDF or pixel inspection
- `native_post_trial_failures_20261009.json` uses invented evidence and response
  text with the original failure topology: one missing `3` token for a `3s`
  claim, and nine CORE fields whose intact evidence spans nine distinct pages.
  Evidence-reference counts and page relationships are preserved. No original
  job IDs or model outputs are retained
- `author_trial_rejections.json` contains fictional authors and institutions,
  an oversized source, a noncontiguous excerpt, and unbound affirmative prose.
  These exercise rejection and rendering boundaries without disclosing real
  authors' proposed or rejected research context

## Validation boundary

The public tests exercise the actual local validators, immutable queue handling,
finite operation accounting, source-span checks, independent-review boundaries,
and exact replay. They do not certify the scientific correctness of a real
paper, actual pixel inspection, historical model output, or production speed.

Earlier real-paper trial validation remains a separate private historical
record. Public synthetic tests cannot independently reproduce that scientific
record and must not be presented as having done so. Published development notes
that describe the earlier trial should be read with this distinction.

All source and image references are inert examples. Tests operate offline and
do not fetch those URLs or treat synthetic hashes as real review approval.
