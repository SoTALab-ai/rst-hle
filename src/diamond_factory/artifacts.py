import hashlib
import json
from pathlib import Path


class LocalObjects:
    """Immutable, content-addressed filesystem objects; future cloud adapters use the same receipt."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value: dict):
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(content).hexdigest()
        key = f"sha256/{sha[:2]}/{sha}.json"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        if target.read_bytes() != content:
            raise ValueError("Object checksum mismatch after write")
        return key, sha, len(content)

    def get(self, key, sha, size):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact path escapes object root")
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Artifact missing or corrupt; refuse recovery")
        return json.loads(data)
