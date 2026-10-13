"""Hash safe regular TAR members without extracting or following any links."""
import hashlib
import json
from pathlib import PurePosixPath
import sys
import tarfile

MAX_BYTES = 256 * 1024 * 1024
rows = []
names = set()
size = 0
member_count = 0
with tarfile.open(sys.argv[1], mode="r:*") as archive:
    for member in archive:
        member_count += 1
        if member_count > 100000:
            raise ValueError("Too many TAR members")
        name = member.name
        # Official Sites packaging wraps the staged build in dist/.
        while name.startswith("./"):
            name = name[2:]
        if member.isdir() and name in {"", ".", "dist"}:
            continue
        if not name.startswith("dist/"):
            raise ValueError("Expected official dist/ artifact wrapper")
        name = name[len("dist/"):]
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts
                or "\\" in name or any(ord(c) < 32 for c in name)
                or path.as_posix() != name.rstrip("/")):
            raise ValueError("Unsafe TAR member")
        if member.isdir():
            continue
        if not member.isfile() or member.issparse() or name in names:
            raise ValueError("TAR links, sparse, duplicate, or special files forbidden")
        names.add(name)
        size += member.size
        if member.size > MAX_BYTES or size > MAX_BYTES or len(names) > 50000:
            raise ValueError("TAR exceeds verification budget")
        body = archive.extractfile(member)
        digest = hashlib.sha256()
        count = 0
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
            count += len(chunk)
        if count != member.size:
            raise ValueError("Truncated TAR member")
        rows.append({"path": name, "bytes": count, "sha256": digest.hexdigest()})
print(json.dumps(rows))
