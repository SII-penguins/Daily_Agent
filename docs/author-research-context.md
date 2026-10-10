# Author and group context

`enrich_author_contexts(items, config)` adds `raw.research_context` without altering scientific summaries or publication eligibility. The normal pipeline invokes it before scoring. Context survives the existing MaterialRecord round trip. `research_context_lines(context)` supplies short, provenance-labeled renderer lines.

## Evidence contract

- Ordered author strings remain source metadata until checked against exact-title/DOI publisher citation metadata or reviewed paper/official evidence
- First-listed author is an ordering fact, not a principal investigator or contribution ranking. Corresponding/co-first/lead roles need explicit evidence
- OpenAlex and Crossref affiliations and ORCID are retained as metadata, labeled unverified. They never imply a lab
- Reviewed sources in `config/research-context-evidence.json` carry exact paper title or DOI, source URL, kind, review status, evidence excerpt, and date. Conflicting DOI or ORCID is not silently reconciled
- Lab relationships are either explicit affiliation in primary evidence or an exact-title entry on a reviewed official publication/project list. Listing a paper does not assert that every coauthor is a group member
- No identity, PI, membership, or lab inference from shared institution, same name, author order, or search snippets
- Historical research-line context is explicitly labeled and never expands the paper-selection date window

The shipped VLANeXt example uses the verified official proceedings PDF, page 1, for nine authors, affiliations, and explicit correspondence to Chen Change Loy. The exact paper appears on the MMLab@NTU homepage. S-Lab affiliation and the MMLab listing remain distinct assertions, not a guessed equivalence. There are no claims of sole lab ownership or PI status.

## Bounded general enrichment

Optional `sources.author_context` settings:

- `enabled: true`
- `max_pages_per_run: 3` (total publisher and lab requests)
- `max_lab_pages_per_run: 1`
- `max_page_bytes: 512000`
- `run_budget_seconds: 12`
- `timeout_seconds: 4`
- `cache_ttl_hours: 168`
- `evidence_registry: config/research-context-evidence.json`
- `watchlist_enabled: true`
- `max_watchlist_entries: 1000`

Only known primary-source publisher hosts are fetched automatically. URLs discovered in paper text are not followed. No redirects, paid API, key, social action, external subscription, or outreach is used. Failed lookups retain explicit unknown labels. Positive context caches are keyed on paper identity, authorship input, and evidence revisions. Network and cached source pages have byte/time limits.

For ongoing group tracking, add `lab_pages` entries to the evidence registry with `name`, verified public official publication-list `url`, and `review_status: verified`. The generic scanner reuses a bounded cached page across all candidates, matches exact normalized titles, and records only `paper_listed_by_group`. Reviewing an official URL establishes that it is a group's publication list; it does not pre-verify future paper claims. Adding more labs does not increase the request budget automatically.

The bounded local `data/author_context/watchlist.json` retains public author/group candidates and supporting paper URLs for future public paper discovery. A verified ORCID can unify an author; without it, same-name authors on different papers remain separate candidates. The watchlist performs no external account action.

## Selected-paper research and resume

After PDF enrichment has been checkpointed, `enrich_selected_author_contexts(records, config)` runs only for selected papers, and only with the explicit `parent_queue` writer transport. It extracts name-bearing correspondence statements conservatively from verified PDF/HTML front matter. More complex numbered affiliations, equal-contribution symbols and group identity go through one bounded batch research job, followed by a separately claimed independent source-review job. There is no regex guessing of superscript relationships or PI status.

The researcher receives up to four relevant PDF pages (10,000 text characters per paper), a first-page image when available, known metadata, and bounded candidate hints read from the local watchlist. It may check at most three public official sources per paper. The response must give exact paper identity, source URLs, dated verbatim evidence, and a supporting quote for each author/institution/role/lab assertion. Local PDF quotes are checked against their actual frozen page. Official-page assertions are independently checked by the reviewer; only individually hash-approved source records are adopted. Unknowns and rejected evidence remain visible. A focused author lookup is not treated as a complete ordered author list.

The two jobs use the existing immutable parent queue and its separate research/reviewer identity enforcement. Inputs, including watchlist hints, are frozen across resumes. Results live in a separate `data/author_context/research-v1` cache; registry or author-context changes do not change scientific reading or review cache keys. Defaults: `research_enabled: true`, `max_research_papers_per_batch: 12` (hard maximum 12), and `research_cache_ttl_hours: 168`. These controls remain under `sources.author_context`. No new paid API or account action is involved.

`watchlist_discovery_queries(config, limit=2)` provides quoted verified author/group candidates to scholarly discovery. They share an existing connector query budget, and every result must still pass the ordinary three-month publication window and quantum/AI relevance checks. Same-name search hits are candidates, never verified identity links. Source metadata refreshes cannot erase previously verified watchlist evidence.

### Explicit-batch schema failure isolation

With the explicit execution ledger, malformed parsed author-research or author-review responses reject only that paper's optional author enrichment. The record keeps `status: not_reviewed`, an `author_context`-scoped schema failure, the original response for audit, and explicit uncertainty. No proposed author claims are adopted and no successful research cache is written. Other papers and scientific drafting/review can continue if their original budgets and gates allow it. Shared backend/JSON failures still open the existing writer circuit; no existing circuit is cleared. Queue identities, claims, provenance, frozen eligibility, deadlines and budget settlement are unchanged. An unknown lab URL must be omitted, never supplied as null or guessed; required source evidence and independent review still apply.
