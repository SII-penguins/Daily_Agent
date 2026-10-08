# Research scope, publication evidence and durable overflow

Policy updated 8 October 2026. Production is `data/cloud`; legacy source credentials and cloud no-new-key restrictions remain separate.

## Scope and selection

- Main stream: both AI-for-quantum and quantum-for-AI, at equal domain priority. Control/calibration, QEC, experiments, materials/state learning, compilation, QML/QNN, kernels, generative models, learning theory and practical benchmarks are included.
- QAS/QNAS is the user's organizational umbrella, including compilation/synthesis and circuit generation/design. Precise subtags are retained; the grouping is not a claim that these research terms are academically equivalent. Prefer approximately 2–3 of the 8 papers when supported candidates exist, rather than letting this topic dominate or disappear.
- Existing targets remain 8 papers + 2 repositories, 6 quantum + 4 exploratory. Exploratory AI/embodied/VLA/agents remain enabled. Counts and topic diversity are soft targets; they never waive full-reading or claim-support requirements.
- Newly discovered papers use the last three **calendar months**, clamping month ends (8 October → 8 July; 31 May → 28/29 February). Future, undated or year-only records cannot prove they fall inside this window. Conference publication dates are separate from event dates. Existing fully reviewed quota-deferred records have durable retention beyond that window and an explicit historical label.

## Ranking and truthful publication labels

- arXiv remains eligible. Its preprint-only publication contribution is −10. Verified formal publication earns +6; preferred ICML/ICLR/NeurIPS/Nature/npj venues earn an additional +14. Other PMLR proceedings earn +8 venue preference. Relevance and evidence quality still matter.
- A DOI, Crossref/OpenAlex bibliographic claim, an arXiv journal reference or an accepted-looking venue name is not independently verified publication.
- PMLR/NeurIPS official paper records and accepted OpenReview records retain provenance from their primary collector. Nature requires matching canonical publisher article metadata, not just a successful RSS or HTTP response. The UI can distinguish published, metadata-only, preprint-only and unconfirmed status.
- The previous cloud formal-papers-only eligibility gate is superseded. Full-document reading, visual fidelity when required, supported claims, delivery reservations and publication-ledger checks remain enforced.

## Public discovery endpoints verified 8 October 2026

- PMLR index: https://proceedings.mlr.press/
- ICML 2026: https://proceedings.mlr.press/v306/ . Conference: 6–11 July; proceedings publication: 29 September. Article example: https://proceedings.mlr.press/v306/liu26bi.html . Parse actual PDF links; current PDFs can live in the official mlresearch GitHub repository.
- NeurIPS proceedings: https://proceedings.neurips.cc/ . Latest listed volume is 2025, volume 38. Main track: https://proceedings.neurips.cc/paper_files/paper/2025/vol38-main-conference . The bare 2025 route may show Creative AI and require following the official “See also” volume link.
- ICLR: https://openreview.net/group?id=ICLR.cc/2026/Conference . Exact accepted `content.venueid` is required. The no-auth notes API returned 403 during verification; current docs say API use needs an account: https://docs.openreview.net/getting-started/using-the-api . Public group metadata access does not imply public notes access. Report degraded coverage; do not request new keys or bypass denial.
- Nature RSS: https://www.nature.com/nature.rss , https://www.nature.com/natmachintell.rss , https://www.nature.com/nphys.rss , https://www.nature.com/ncomms.rss , https://www.nature.com/natcomputsci.rss , https://www.nature.com/natelectron.rss , https://www.nature.com/natrevphys.rss , https://www.nature.com/npjqi.rss . RSS is rolling, not an exhaustive three-month archive; journal-scoped Crossref discovery supplements it.
- Nature live landing verification hit an off-host `idp.nature.com` cookie/identity redirect. The collector rejects that path and retains metadata-only status with a visible source failure. It does not infer publication verification or fulltext access from the RSS feed. It ignores Springer API links that require keys.

## Durable pool semantics

`pool_deferral` is only assigned to a fully read, claim-supported, editorial-PASS paper omitted when the daily item/paper capacity is full. It records reason `daily_count_limit`, first/last deferral date, paper version identity and a revisit policy. Unreviewed high-scoring overflow uses a separate `screening_deferred` state with `daily_count_limit_before_review` and `review_required=true`; it is retained but has no full-reading or approval endorsement. Rejected, unsupported, delivered and reserved records are not quota-deferred.

Scores and publication evidence are recalculated for each issue. Deferred papers gain a small waiting bonus (0.5/day, capped at 6); freshness is recalculated, never frozen. Repeated discovery preserves deferral and reading data only for the same version. Delivery and cross-source identity suppression remain independent and authoritative.
