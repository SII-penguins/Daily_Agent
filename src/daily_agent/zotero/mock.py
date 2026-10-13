"""Pure in-memory Zotero double. Never sends HTTP or uses credentials."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .domain import ImportRejected, sha256
from .importer import BlobUnavailable, TransportError, VersionConflict


class MemoryBlobs:
    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.reads = []

    def read_blob(self, blob_id):
        self.reads.append(blob_id)
        if blob_id not in self.blobs:
            raise BlobUnavailable(blob_id)
        return self.blobs[blob_id]


class MockZotero:
    mode = "offline_mock"

    def __init__(self, library_scope="personal:offline-fixture"):
        self.library_scope = library_scope
        self.items = {}
        self.collections = {}
        self.attachments = {}
        self.files = {}
        self.calls = []
        self.complete_inventory = True
        self.failures = {}
        self.after_patch_conflict = None

    def fail_next(self, operation, *, after=False, code="uncertain_write", retryable=True):
        self.failures[(operation, after)] = TransportError(code, retryable=retryable)

    def _fail(self, operation, after=False):
        error = self.failures.pop((operation, after), None)
        if error:
            raise error

    def inventory(self):
        self.calls.append("inventory")
        self._fail("inventory")
        return deepcopy(list(self.items.values())), self.complete_inventory

    def get_item(self, key):
        self._fail("get_item")
        return deepcopy(self.items.get(key))

    def create_item(self, key, fields, identity, *, version):
        self.calls.append("create_item")
        self._fail("create_item")
        if version != 0 or key in self.items:
            raise VersionConflict()
        item = {**deepcopy(fields), "key": key, "version": 1, "identity": deepcopy(identity),
                "collections": [], "tags": [], "notes": []}
        self.items[key] = item
        self._fail("create_item", True)
        return deepcopy(item)

    def ensure_collection(self, path):
        self.calls.append("ensure_collection")
        self._fail("ensure_collection")
        for i in range(1, len(path) + 1):
            prefix = path[:i]
            if prefix not in self.collections:
                # Mock-only stable keys, opaque to domain logic.
                self.collections[prefix] = f"C{len(self.collections):07d}"
        self._fail("ensure_collection", True)
        return self.collections[path]

    def patch_classification(self, key, collections, tags, *, version):
        self.calls.append("patch_classification")
        self._fail("patch_classification")
        if self.after_patch_conflict:
            mutate = self.after_patch_conflict
            self.after_patch_conflict = None
            mutate(self.items[key])
            self.items[key]["version"] += 1
        if self.items[key]["version"] != version:
            raise VersionConflict()
        self.items[key]["collections"] = deepcopy(collections)
        self.items[key]["tags"] = deepcopy(tags)
        self.items[key]["version"] += 1
        self._fail("patch_classification", True)
        return deepcopy(self.items[key])

    def find_attachment(self, parent, sha, size):
        for key, attachment in self.attachments.items():
            content = self.files.get(key)
            if (attachment["parentItem"] == parent and attachment.get("registered")
                    and content is not None and sha256(content) == sha and len(content) == size):
                return deepcopy(attachment)
        return None

    def get_attachment(self, key):
        return deepcopy(self.attachments.get(key))

    def create_attachment(self, key, parent, filename, *, version):
        self.calls.append("create_attachment")
        self._fail("create_attachment")
        if version != 0 or key in self.attachments or key in self.items:
            raise VersionConflict()
        attachment = {"key": key, "parentItem": parent, "filename": filename,
                      "linkMode": "imported_file", "contentType": "application/pdf",
                      "md5": None, "mtime": None, "registered": False, "version": 1}
        self.attachments[key] = attachment
        self._fail("create_attachment", True)
        return deepcopy(attachment)

    def upload_and_register(self, key, content, md5, *, if_none_match):
        self.calls.append("upload_and_register")
        self._fail("upload_and_register")
        if not if_none_match or self.attachments[key]["registered"]:
            raise VersionConflict()
        if hashlib.md5(content, usedforsecurity=False).hexdigest() != md5:
            raise ImportRejected("Mock upload hash mismatch")
        self.files[key] = content
        self.attachments[key].update(registered=True, md5=md5, mtime=0, version=2)
        self._fail("upload_and_register", True)

    def download_registered(self, key):
        self.calls.append("download_registered")
        self._fail("download_registered")
        if not self.attachments[key]["registered"]:
            raise TransportError("file_not_registered", retryable=True)
        return self.files[key]
