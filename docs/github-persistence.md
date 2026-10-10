# GitHub source and report-artifact persistence

## Separate branches, separate responsibilities

- `main`: application code, workflow contracts, configuration templates, tests and documentation
- `daily-artifacts`: reviewed, public-safe generated HTML editions, their required assets and small integrity manifests
- Private recovery storage: complete operational state, exact deployed-source bundle and independently verified restore receipts

A branch inherits the repository's visibility. This repository is public; a new branch is not a private backup. Never commit credentials, tokens, personalized runtime configuration, queue jobs or answers, account/worker/conversation identifiers, private Site URLs, Library identities or receipts, full operational snapshots, or source material without redistribution permission. Ignore rules alone are not a privacy review.

GitHub stores code and approved report files durably. It does not host the parent assistant, install a schedule, restore a paused worker, confirm a message, or guarantee that an unattended report will finish. A verified Git commit is evidence of repository persistence only. Publication and delivery retain their separate acceptance and readback contracts.

## Publishing source

1. Read the current `main` commit and compare all intended files against that exact tree
2. Preserve unrelated remote changes; inspect and reconcile any conflict
3. Run the applicable offline tests against the final source and record actual results
4. Create a commit with the observed head as its parent, then fast-forward `main` with an expected-head check; never force-push
5. Read the branch and commit back from GitHub and inspect checks for that exact commit

Use an existing authorized GitHub connection. Do not add credentials to the repository or mint access solely for this workflow. Public test fixtures must be synthetic, licensed for redistribution, or sufficiently minimized; replacing a private fixture must not pretend that a synthetic replay is an observed real-paper run.

## Artifact layout and acceptance

The artifact branch has its own root tree and no application checkout:

    README.md
    catalog.json
    reports/YYYY-MM-DD/EDITION/index.html
    reports/YYYY-MM-DD/EDITION/assets/...
    reports/YYYY-MM-DD/EDITION/manifest.json

Publish only a finished, accepted edition. Keep prior edition paths immutable. If an authorized correction is needed, use a new edition identifier and preserve the old one. A catalog entry can reference the edition path and safe provenance; it must not claim external delivery without actual delivery evidence. Do not publish draft, diagnostic or failed-run HTML as a finished daily report.

Each manifest should include a schema version, report date, edition identifier, generating code commit or source fingerprint, acceptance status, and the relative path, byte count and SHA256 for every report/asset file. Keep private operational proof in private recovery storage; public manifests carry no message, account, Library or private Site identifiers. Verify all local links and assets and scan the final HTML and metadata before committing. A hash proves byte identity, not scientific correctness or permission to redistribute source figures.

Use the same expected-head, fast-forward and remote-readback checks as source publication. If the branch has moved, read the new head and merge its artifact catalog without overwriting existing editions. Never merge generated artifacts into `main`.

## Restoring code and HTML

For source-only recovery, clone the repository and check out the exact verified commit needed by the deployment. Follow the README to prepare dependencies and run the tests. The configuration under `config/` is a template, not recovered production state.

For a report-only recovery, check out `daily-artifacts` separately or download the exact edition files at a verified artifact commit. Verify their manifest hashes and open `index.html` with its relative assets. The GitHub file viewer is not the private HTML reading service.

## Restoring an operational run

Source and reports cannot reconstruct mutable queue claims, counters, deadlines, cached evidence, delivery reservations or publication history. Keep the complete private state and an exact deployed-source bundle under the restart-safe checkpoint process in [durable recovery](durable-recovery.md).

A recovery registry must bind the exact source/state versions and hashes to independently materialized bytes. Preserve all existing budgets, attempts, sealed identities and ambiguous delivery outcomes. Bootstrap into a new empty target and retain the mutation fence until prior ownership, paths and delivery state are reconciled. Do not start a new issue, reset counters, or resend a report merely because a new executor has an empty filesystem.

The public source snapshot may differ from the private deployed bundle in documentation and synthetic test fixtures. That does not authorize substituting it into an old source-bound run; restore the recorded deployed bundle or use an explicit validated migration for future work.
