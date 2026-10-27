"""Thin-client source selection and upload for Relay memory analysis.

This module performs ordinary local extraction, deterministic chunking, and
resumable/idempotent upload.  It deliberately contains no memory construction,
ranking, hierarchy, retrieval, or routing logic.
"""

import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from xml.etree import ElementTree

from .client_endpoint import normalize_relay_base_url
from .client_errors import ProviderResponseError, ValidationError
from .client_memory_mode import MemoryPreparationProgress


MEMORY_IMPORT_VERSION = "0.58.0"
MAXIMUM_SOURCE_FILES = 10_000
MAXIMUM_SOURCE_BYTES = 10 * 1024 * 1024 * 1024
MAXIMUM_SINGLE_FILE_BYTES = 512 * 1024 * 1024
MAXIMUM_EVENT_TEXT_BYTES = 48 * 1024
MAXIMUM_REQUEST_BYTES = 900 * 1024
MAXIMUM_EVENTS_PER_REQUEST = 64
_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log"}
)
_DOCX_EXTENSIONS = frozenset({".docx"})
_NATIVE_EXTENSIONS = frozenset({".pdf", ".rtf", ".html", ".htm"})
_SUPPORTED_EXTENSIONS = _TEXT_EXTENSIONS | _DOCX_EXTENSIONS | _NATIVE_EXTENSIONS
_WHITESPACE = re.compile(r"[ \t]+")
ProgressCallback = Callable[[MemoryPreparationProgress], None]
ImportTransport = Callable[
    [str, Mapping[str, str], bytes, float], Tuple[int, bytes]
]


class _NoRelayRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward a Relay credential or selected content to another URL."""

    def redirect_request(self, request, file_pointer, code, message, headers, url):
        del request, file_pointer, code, message, headers, url
        return None


def _urllib_transport(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> Tuple[int, bytes]:
    request = urllib.request.Request(
        url=url, data=body, headers=dict(headers), method="POST"
    )
    try:
        opener = urllib.request.build_opener(
            _NoRelayRedirects(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(MAXIMUM_REQUEST_BYTES + 1)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(MAXIMUM_REQUEST_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderResponseError("memory import request failed") from None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_ref(relative_name: str) -> str:
    cleaned = relative_name.replace(os.sep, "/").lstrip("/")
    return "customer-file:///" + urllib.parse.quote(cleaned, safe="/._-()")


def _timestamp(path: Path) -> str:
    occurred = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return occurred.isoformat().replace("+00:00", "Z")


def _decode_text(encoded: bytes) -> str:
    if b"\x00" in encoded[:4096]:
        raise ValidationError("selected source is not supported text")
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return encoded.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("selected source text encoding is unsupported")


def _docx_text(encoded: bytes) -> str:
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(encoded), "r") as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise ValidationError("selected Word document is invalid")
            payload = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        raise ValidationError("selected Word document is invalid") from None
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        raise ValidationError("selected Word document is invalid") from None
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(
            node.text or "" for node in paragraph.iter(namespace + "t")
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _native_extractor_path() -> Path:
    configured = os.environ.get("LONGWEIS_RELAY_SOURCE_EXTRACTOR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    executable_parent = Path(sys.executable).resolve().parent
    candidates.append(executable_parent / "Longweis Relay Source Extractor")
    module = Path(__file__).resolve()
    if len(module.parents) >= 3:
        candidates.append(
            module.parents[2] / "MacOS" / "Longweis Relay Source Extractor"
        )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink() and os.access(str(candidate), os.X_OK):
            return candidate
    raise ValidationError("the native document extractor is unavailable")


def _native_text(path: Path) -> str:
    try:
        completed = subprocess.run(
            [str(_native_extractor_path()), "--extract", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValidationError("selected source could not be extracted") from None
    if completed.returncode != 0 or len(completed.stdout) > MAXIMUM_SINGLE_FILE_BYTES * 2:
        raise ValidationError("selected source could not be extracted")
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("selected source extraction was invalid") from None


def extract_memory_source(path: str) -> str:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValidationError("selected source must be a regular file")
    size = candidate.stat().st_size
    if size <= 0 or size > MAXIMUM_SINGLE_FILE_BYTES:
        raise ValidationError("selected source is empty or exceeds the file limit")
    extension = candidate.suffix.lower()
    if extension not in _SUPPORTED_EXTENSIONS:
        raise ValidationError("selected source type is not supported")
    if extension in _NATIVE_EXTENSIONS:
        text = _native_text(candidate)
    else:
        try:
            encoded = candidate.read_bytes()
        except OSError:
            raise ValidationError("selected source could not be read") from None
        text = _docx_text(encoded) if extension in _DOCX_EXTENSIONS else _decode_text(encoded)
    text = "\n".join(_WHITESPACE.sub(" ", line).rstrip() for line in text.splitlines())
    cleaned = text.strip()
    if not cleaned:
        raise ValidationError("selected source contains no importable text")
    return cleaned


@dataclass(frozen=True)
class MemorySource:
    path: str
    relative_name: str
    size_bytes: int
    modified_at: str

    @property
    def source_ref(self) -> str:
        return _source_ref(self.relative_name)


def scan_memory_sources(paths: Sequence[str]) -> Tuple[MemorySource, ...]:
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValidationError("select at least one source")
    discovered = []
    seen = set()
    for supplied in paths:
        if not isinstance(supplied, str) or not supplied.strip() or "\x00" in supplied:
            raise ValidationError("selected source path is invalid")
        root = Path(supplied).expanduser()
        if root.is_symlink() or not root.exists():
            raise ValidationError("selected source is unavailable")
        if root.is_file():
            candidates = ((root, root.name),)
        elif root.is_dir():
            rows = []
            for directory, names, filenames in os.walk(str(root), followlinks=False):
                names[:] = sorted(
                    name
                    for name in names
                    if not (Path(directory) / name).is_symlink()
                )
                for filename in sorted(filenames):
                    candidate = Path(directory) / filename
                    if candidate.is_symlink() or candidate.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                        continue
                    rows.append((candidate, str(candidate.relative_to(root.parent))))
            candidates = tuple(rows)
        else:
            raise ValidationError("selected source must be a file or folder")
        for candidate, relative_name in candidates:
            if candidate.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                raise ValidationError("selected source type is not supported")
            resolved = str(candidate.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            size = candidate.stat().st_size
            if size <= 0 or size > MAXIMUM_SINGLE_FILE_BYTES:
                raise ValidationError("selected source is empty or exceeds the file limit")
            discovered.append(
                MemorySource(resolved, relative_name, size, _timestamp(candidate))
            )
            if len(discovered) > MAXIMUM_SOURCE_FILES:
                raise ValidationError("selected sources exceed the file-count limit")
    if not discovered:
        raise ValidationError("no supported source files were found")
    if sum(item.size_bytes for item in discovered) > MAXIMUM_SOURCE_BYTES:
        raise ValidationError("selected sources exceed the import-size limit")
    return tuple(sorted(discovered, key=lambda item: item.relative_name.casefold()))


def _utf8_chunks(text: str, maximum_bytes: int = MAXIMUM_EVENT_TEXT_BYTES) -> Tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("memory source text is empty")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks = []
    current = ""
    for paragraph in paragraphs:
        pieces = []
        remaining = paragraph
        while len(remaining.encode("utf-8")) > maximum_bytes:
            low, high = 1, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if len(remaining[:middle].encode("utf-8")) <= maximum_bytes:
                    low = middle
                else:
                    high = middle - 1
            boundary = max(1, low)
            pieces.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining:
            pieces.append(remaining)
        for piece in pieces:
            candidate = piece if not current else current + "\n\n" + piece
            if len(candidate.encode("utf-8")) <= maximum_bytes:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return tuple(chunks)


def memory_events_for_source(source: MemorySource) -> Tuple[Dict[str, object], ...]:
    text = extract_memory_source(source.path)
    document_hash = _sha256_bytes(text.encode("utf-8"))
    events = []
    chunks = _utf8_chunks(text)
    for index, chunk in enumerate(chunks):
        identity = "%s\n%s\n%d\n%s" % (
            source.source_ref,
            document_hash,
            index,
            _sha256_bytes(chunk.encode("utf-8")),
        )
        events.append(
            {
                "event_id": "file.%s" % _sha256_bytes(identity.encode("utf-8")),
                "occurred_at": source.modified_at,
                "text": chunk,
                "source_ref": source.source_ref,
                "metadata": {
                    "kind": "selected_file",
                    "document_hash": document_hash,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                },
            }
        )
    return tuple(events)


def _request_batches(events: Iterable[Mapping[str, object]]) -> Tuple[bytes, ...]:
    batches = []
    current = []
    for event in events:
        candidate = current + [dict(event)]
        encoded = json.dumps(
            {"events": candidate}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(candidate) > MAXIMUM_EVENTS_PER_REQUEST or len(encoded) > MAXIMUM_REQUEST_BYTES:
            if not current:
                raise ValidationError("memory event exceeds the upload limit")
            batches.append(
                json.dumps(
                    {"events": current}, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            current = [dict(event)]
        else:
            current = candidate
    if current:
        batches.append(
            json.dumps(
                {"events": current}, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
    return tuple(batches)


@dataclass(frozen=True)
class MemoryImportResult:
    selected_files: int
    uploaded_events: int
    inserted_events: int
    replayed_events: int

    def public_payload(self) -> Dict[str, object]:
        return {
            "status": "ready",
            "selected_files": self.selected_files,
            "uploaded_events": self.uploaded_events,
            "inserted_events": self.inserted_events,
            "replayed_events": self.replayed_events,
            "resumable_by_replay": True,
            "client_contains_memory_engine": False,
        }


class MemoryImportClient:
    def __init__(
        self,
        relay_base_url: str,
        relay_credential_reference: str,
        credential_reader: Callable[[str], str],
        transport: Optional[ImportTransport] = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.base_url = normalize_relay_base_url(relay_base_url)
        if not isinstance(relay_credential_reference, str) or not relay_credential_reference:
            raise ValidationError("Relay credential reference is invalid")
        if not callable(credential_reader):
            raise ValidationError("Relay credential reader is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not (1 <= float(timeout_seconds) <= 600)
        ):
            raise ValidationError("memory import timeout is invalid")
        self.credential_reference = relay_credential_reference
        self.credential_reader = credential_reader
        self.transport = transport or _urllib_transport
        self.timeout_seconds = float(timeout_seconds)

    def _upload(self, body: bytes, relay_key: str) -> Mapping[str, object]:
        status, response_body = self.transport(
            self.base_url + "/memory/events",
            {
                "Authorization": "Bearer " + relay_key,
                "Content-Type": "application/json",
                "User-Agent": "longweis-relay-macos/0.58",
            },
            body,
            self.timeout_seconds,
        )
        if status < 200 or status >= 300:
            raise ProviderResponseError("memory import returned HTTP %d" % status)
        if len(response_body) > MAXIMUM_REQUEST_BYTES:
            raise ProviderResponseError("memory import response exceeded the size limit")
        try:
            payload = json.loads(response_body.decode("utf-8"))
            accepted = int(payload["accepted_events"])
            inserted = int(payload["inserted_events"])
            replayed = int(payload["replayed_events"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderResponseError("memory import returned malformed JSON") from None
        if min(accepted, inserted, replayed) < 0 or inserted + replayed != accepted:
            raise ProviderResponseError("memory import returned invalid counts")
        return payload

    def prepare(
        self,
        paths: Sequence[str],
        progress: Optional[ProgressCallback] = None,
    ) -> MemoryImportResult:
        callback = progress or (lambda unused: None)
        callback(MemoryPreparationProgress("scanning_sources"))
        sources = scan_memory_sources(paths)
        try:
            relay_key = self.credential_reader(self.credential_reference)
        except Exception:
            raise ValidationError("Relay credential is unavailable") from None
        if not isinstance(relay_key, str) or not relay_key.strip():
            raise ValidationError("Relay credential is unavailable")
        total_events = inserted_events = replayed_events = 0
        for source_index, source in enumerate(sources):
            events = memory_events_for_source(source)
            callback(
                MemoryPreparationProgress(
                    "transferring_selected_content", source_index, len(sources)
                )
            )
            for body in _request_batches(events):
                callback(MemoryPreparationProgress("organizing_private_memory"))
                response = self._upload(body, relay_key.strip())
                total_events += int(response["accepted_events"])
                inserted_events += int(response["inserted_events"])
                replayed_events += int(response["replayed_events"])
            callback(
                MemoryPreparationProgress(
                    "transferring_selected_content", source_index + 1, len(sources)
                )
            )
        callback(MemoryPreparationProgress("ready", len(sources), len(sources)))
        return MemoryImportResult(
            len(sources), total_events, inserted_events, replayed_events
        )


def supported_memory_source_extensions() -> Tuple[str, ...]:
    return tuple(sorted(_SUPPORTED_EXTENSIONS))
