# Paper-first v3: bounded semantic pilot, 10 October 2026

## Outcome

An isolated, **cold-semantic** two-paper pilot produced independently qualified HTML for QUFIG (8 pages) and DriveWorld-VLA (21 pages): 20 Chinese scientific paragraphs and 6 original crops. Only immutable PDFs and source metadata were reused; no previous chunk notes, drafts or scientific reviews were consumed.

- Cumulative start: 09:59:08 UTC
- First qualified paper HTML: 10:09:05 UTC, **9 min 57 sec**
- Both papers qualified and HTML sealed: 10:11:42 UTC, **12 min 34 sec**
- Actual private remote upload, materialization and empty-directory restore verified: 10:12:47 UTC, **13 min 39 sec** total
- Native operations: 2 integrated reader/draft jobs + 2 independent reviews + 1 repair = **5 actual jobs**
- Final local acceptance and seal: 0.036 seconds; first qualified preview render: 0.006 seconds
- Remote save/readback/restore phase after the complete HTML: approximately 65 seconds

The timing includes orchestration, setup problems and the actual repair. It is not a cold download/discovery benchmark, a head-to-head controlled legacy comparison, or an unattended 8-paper + 2-repository production guarantee. No existing edition, production source, delivery history or schedule was changed. A later standalone private HTML attachment upload is outside the measured run.

## Preserved failures and limitations

An initial enrollment queued two reader requests, but no native worker had been dispatched when additional synthetic tests exposed malformed-response handling problems. That initialization and source were preserved. The corrected fresh enrollment retained the original cumulative clock and 30-minute cutoff; the abandoned requests are not misreported as actual model executions.

DriveWorld's first actual response supplied nine anchors for one paragraph, exceeding the implementation's eight-anchor bound. Its one repair combined two overlapping exact quotations, with all scientific text, conditions, figures, captions and author context unchanged. This extra job is included above. The final future-issue prompt now states the existing bound explicitly.

The independent reviews examined all source pages and all six supplied page/crop images per paper. QUFIG's report preserves the paper's table/prose MAE contradiction and Figure 4 population inconsistency. DriveWorld's report distinguishes its average collision rate from the 3-second rate and states weaker L2 comparisons and evaluation/input limitations. These were substantive checks, not automatic acceptance of well-formed JSON.

Browser-layout verification was blocked: the local headless browser could not create required sockets, and the cloud browser disallowed file URLs. No restriction was bypassed. Static HTML verification confirmed all 20 reviewed claim texts were preserved exactly, all six embedded PNGs matched accepted crops, links used HTTPS, and no scripts/forms/iframes/event handlers were present. Independent scientific pixel review was completed; visual browser-layout QA remains unproven.

## Actual private recovery

The initial immutable source base was **9,361,466 bytes**. The final latest delta was **3,242,602 bytes**, containing small current state, queue provenance, accepted evidence, images, HTML and the frozen diagnostic implementation. Both were saved as new private Library files, materialized again, and SHA-256 compared before restoration.

An empty-directory restore reproduced **all 46 non-lock files exactly**, including the sealed HTML. A resume admitted **zero new model jobs**. A separate intermediate restore preserved one qualified and one pending paper, likewise with no added jobs and byte-identical partial HTML.

This proves the tested two-file remote recovery path, not an autonomous storage integration. The host performed actual Library calls; the repository does not possess paid-service credentials or claim uploads from local manifests alone. Private recovery IDs, PDFs, raw quotations, model responses and report artifacts are intentionally absent from the public source tree.

## Source and test distinction

The semantic pilot stayed on its frozen implementation throughout actual model work. Independent code review subsequently hardened two future-issue edge cases: rejecting subpixel crops locally and retaining conditions in rendered output even if a writer places unique conditions only in the audit field. The pilot readers/reviewers explicitly verified that its own conditions already appeared in visible paragraph text.

- Full offline suite with the functional guard fixes: **2,122 passed in 367.05 seconds**
- After the final prompt-only clarification: **83 focused tests passed in 4.94 seconds**
- Independent mechanical review: **54 workflow tests**, plus **29 compact-checkpoint tests**

The semantic pilot is not represented as a run of the later guard-hardened source. Direct verification confirmed the final implementation refuses the frozen pilot instead of silently reusing it. Existing legacy protocols remain separate, and adoption is opt-in for a fresh issue.
