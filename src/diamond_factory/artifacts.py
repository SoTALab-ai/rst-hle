import gzip
import hashlib
import json
import uuid
from pathlib import Path


class LocalObjects:
    """Immutable, content-addressed filesystem objects; future cloud adapters use the same receipt."""

    def __init__(self, root: str | Path, *, compress_json=False):
        self.compress_json = compress_json
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, value: dict):
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        suffix = ".json"
        if self.compress_json:
            content = gzip.compress(content, mtime=0)
            suffix += ".gz"
        sha = hashlib.sha256(content).hexdigest()
        key = f"sha256/{sha[:2]}/{sha}{suffix}"
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
        content = self.get_bytes(key, sha, size)
        return json.loads(gzip.decompress(content) if key.endswith(".json.gz") else content)

    def put_bytes(self, content: bytes):
        """PDF等原始二进制与JSON分存；同一哈希只保存一份，损坏不覆盖。"""
        sha = hashlib.sha256(content).hexdigest()
        key = f"sha256/{sha[:2]}/{sha}.bin"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(content)
                # 排他创建避免另一写者或损坏缓存被静默替换。
                try:
                    target.hardlink_to(temporary)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        if target.read_bytes() != content:
            raise ValueError("Object checksum mismatch after write")
        return key, sha, len(content)

    def get_bytes(self, key, sha, size):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact path escapes object root")
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Artifact missing or corrupt; refuse recovery")
        return data
