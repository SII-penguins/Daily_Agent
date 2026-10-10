# Daily Agent report artifacts

This branch stores accepted, public-safe HTML daily editions and their required assets. Application code, workflow contracts, configuration templates, and tests live on `main`.

## Current archive

No report edition has been published to this branch yet. An empty catalog does not mean that no report exists elsewhere, and it does not claim generation or delivery success.

Future accepted editions use `reports/YYYY-MM-DD/EDITION/index.html`, relative assets, and a small SHA256 integrity manifest. Existing edition paths remain immutable; an authorized correction receives a new edition identifier.

## Privacy and recovery

This branch inherits the repository's public visibility. It is not a private operational backup. Do not store credentials, private runtime state, queue/worker/conversation IDs, Library receipts, private Site URLs, full PDFs or unlicensed source material here. A commit confirms repository persistence only, not message delivery or scientific acceptance.

See [source and restoration instructions](https://github.com/SII-penguins/Daily_Agent/blob/main/docs/github-persistence.md). Complete operational recovery uses a separately retained, verified private source/state checkpoint pair.
