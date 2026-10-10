# Public source and private runtime repositories

The public repository contains reusable code, tests, documentation and clearly
identified synthetic examples. Personal reports, research preferences, delivery
destinations, schedules, deployed Site source and operational receipts belong in
a separate private repository or private storage. Every branch in a public
repository is public; an artifact branch is not a privacy boundary.

## Public source

Keep reusable source on `main`. Review all changed files and generated metadata
for personal information and credentials before pushing. Tests and example
reports must be synthetic and explicitly labeled; never describe a synthetic
example as an observed production run. Preserve licenses and source attribution.
Use a GitHub noreply commit email. Check the exact remote head before each push
and read back the resulting commit. Never upload personal reports to a public
`daily-artifacts` branch. That branch is retained only as a policy marker.

## Private runtime

Verify that the destination repository is private before the first upload.
Store personal non-secret configuration, accepted dated reports, required assets
and a complete manifest there. Enumerate the file tree as well as the catalog so
an unlisted supplement is not omitted. Do not commit actual credentials, even to
a private repository; retain them in environment variables or secret storage.
Keep personal Site source and access settings private. Do not publish a private
repository or Site address in public documentation.

Keep source versions separate from mutable operational state. Record source
commit, file hashes and restore evidence privately. A backup must be downloaded
and checked before it is considered recoverable. Preserve accepted or uncertain
delivery receipts during restore; restoring a report does not authorize sending
it again. A Git push does not install schedules or confirm report delivery.

## Configuration and recovery

The checked-in configuration is an illustrative research preset, not an installed
personal deployment. Copy it into a separate private runtime root; set your own
topics, timezone, schedule and explicitly enabled delivery integrations there.
Quality-profile enforcement must preserve those delivery choices. Refer to the
[local guide](local-guide.md), [cloud workflow](cloud-migration.md) and
[durable recovery contract](durable-recovery.md) for execution and restore APIs.
