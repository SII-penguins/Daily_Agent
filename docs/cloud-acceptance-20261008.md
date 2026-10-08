# Deployment acceptance checklist

This is a generic checklist, not a report of a personal production run.

Use a separate calibration root. Verify source access, evidence quality, local
rendering, private destination access and exact delivery readback independently.
Keep deployment identities, private addresses, source/state snapshots and receipts
in private storage. Test an empty-directory restore without generating new jobs
or resending an accepted message. A passing offline suite does not establish
unattended production reliability. See [cloud workflow](cloud-migration.md) and
[repository separation](github-persistence.md).
