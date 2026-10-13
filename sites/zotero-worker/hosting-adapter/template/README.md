# Daily Agent private Sites adapter

This sanitized Vinext template contains application-independent hosting code and schema-only migrations. It contains no real Site identity, runtime secrets, report archive, owner ID, or Zotero credentials.

Use ../materialize.mjs from the parent Zotero source directory to create a separate private checkout. The materializer copies the application modules and explicitly supplied retained archive, preserving file hashes. Do not build this incomplete template directly.

The adapter preserves the standard Sites Vite integration and Vinext server handler. Its Worker routes Zotero requests first and serves existing report paths through the generated ASSETS binding. The private source checkout must retain the existing Site identity and audience before publication.

See ../../HOSTING.md for local commands, checks, deployment prerequisites, and verified limits.
