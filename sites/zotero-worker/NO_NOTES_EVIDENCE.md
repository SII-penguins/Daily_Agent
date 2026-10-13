# Minimal-note-access transport evidence

Reviewed 2026-10-10 using public official documentation and Zotero's public source. No account, credential, library, API, or storage request was made. The tests use injected responses only; this is not live-service acceptance evidence.

## Implemented boundary

- `verifyCurrentKey` defaults to `requireNotes: false`; personal-library, write and file grants remain independently checked
- All item inventory pages, item version proofs and individual-key lookups use `includeTrashed=1` with one `itemType=-note || annotation` filter. The individual-key lookup uses the multi-item endpoint with `itemKey`, never an unfiltered single-item JSON request
- Inventory completeness means the entire filtered bibliographic/attachment scope plus collections. Pagination totals, unique keys, object versions and terminal key/version maps must agree at the same library version. The return value includes `complete: true`, `includesTrashed: true`, and `excludesNotes: true`
- A returned note/annotation item is an error. Item data is projected at the transport boundary without `note`, `notes`, or `annotation*` properties. These fields are never returned to the runner, logged, included in membership patches, or accepted by bibliographic creation
- Membership updates still send only complete desired collection/tag arrays with an object-version precondition. Existing note objects and content are not changed

## Official support

1. [API basics](https://www.zotero.org/support/dev/web_api/v3/basics) documents `itemKey`, Boolean `itemType` filters, `includeTrashed`, pagination, and `format=versions`
2. [Syncing](https://www.zotero.org/support/dev/web_api/v3/syncing) explains that version maps cover all matching objects without a default limit. Multi-object response version headers describe the library; individual objects retain their own versions. It also documents `/keys/current` grants and conflict-safe version-zero creation
3. [OAuth permissions](https://www.zotero.org/support/dev/web_api/v3/oauth#requesting_specific_permissions) treats personal-library note reading as a separate permission
4. [Official search parser](https://github.com/zotero/dataserver/blob/master/model/API.inc.php#L1296-L1325) removes a leading negation before splitting the OR terms. [Official item search](https://github.com/zotero/dataserver/blob/master/model/Items.inc.php#L370-L403) applies the resulting set with `NOT IN` and rejects repeated `itemType` parameters. Thus `-note || annotation` excludes both types; repeated `itemType=-note&itemType=-annotation` is not used
5. [Partial-item writes](https://www.zotero.org/support/dev/web_api/v3/write_requests#partial-item_updating_patch) leave omitted properties untouched and replace only the supplied arrays

## Important limit: legacy attachment notes

This implementation does **not** claim that no note bytes can cross the API response wire. Zotero's [item response serializer](https://github.com/zotero/dataserver/blob/master/model/Item.inc.php#L4419-L4420) obtains editable item data through `toJSON`. Its [attachment serialization](https://github.com/zotero/dataserver/blob/master/model/Item.inc.php#L4735-L4738) includes legacy embedded `note` content, without a notes-permission argument. Such a field may therefore occur incidentally in attachment metadata even with a notes-disabled key. It is discarded by this adapter before an item leaves the transport boundary; it is never used to match, classify, overwrite or verify an import.

The reviewed source is the repository's current `master` view, not a guarantee about the exact deployed server revision. A stronger zero-note-bytes guarantee would need a different, separately verified attachment protocol. The current boundary preserves working PDF upload/readback and avoids intentional note/annotation queries or broader grants.
