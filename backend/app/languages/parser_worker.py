"""Fixed static parser entry point. Never import or execute target code."""

import sys
import struct
from pathlib import Path

# -I removes the cwd and PYTHONPATH; add only the trusted server package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.languages.isolation import MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, ParserRequest
from app.languages.registry import default_language_registry


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(((MAX_SOURCE_BYTES + 2) // 3) * 4 + 8193)
        request = ParserRequest.model_validate_json(raw)
        source = request.source_bytes()
        adapter = default_language_registry.get(request.language_id)
        result = adapter._analyze_native(source, request.relative_path, request.extension)
        payload = result.model_dump_json().encode("utf-8")
        if len(payload) > MAX_OUTPUT_BYTES:
            return 3
        sys.stdout.buffer.write(payload)
        return 0
    except Exception:
        return 2


def persistent_main() -> int:
    limit = ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 8192
    try:
        while header := sys.stdin.buffer.read(4):
            if len(header) != 4:
                return 2
            size = struct.unpack("!I", header)[0]
            if size > limit:
                return 2
            raw = sys.stdin.buffer.read(size)
            if len(raw) != size:
                return 2
            request = ParserRequest.model_validate_json(raw)
            source = request.source_bytes()
            adapter = default_language_registry.get(request.language_id)
            result = adapter._analyze_native(source, request.relative_path, request.extension)
            payload = result.model_dump_json().encode("utf-8")
            if len(payload) > MAX_OUTPUT_BYTES:
                return 3
            sys.stdout.buffer.write(
                struct.pack("!I", len(payload)) + request.request_id.encode("ascii") + payload
            )
            sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(persistent_main() if sys.argv[1:] == ["--persistent"] else main())
