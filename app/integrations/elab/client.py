"""Small dependency-free client for the eLabFTW REST API v2."""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import mimetypes
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from app.integrations.elab.config import ElabCredentials


class ElabApiError(RuntimeError):
    """Raised for an eLabFTW transport, authentication or API error."""


@dataclass(frozen=True, slots=True)
class ElabTemplate:
    id: int
    title: str
    body: str = ""


@dataclass(frozen=True, slots=True)
class ElabApiResponse:
    status: int
    headers: Mapping[str, str]
    payload: Any


class MultipartFileStream:
    """Stream multipart form-data for large file uploads without buffering the file in RAM (NET-02)."""

    def __init__(self, prefix: bytes, file_path: Path, suffix: bytes) -> None:
        self._prefix_bytes = prefix
        self._prefix_stream: io.BytesIO | None = io.BytesIO(prefix)
        self._file_path = file_path
        self._file_stream: io.BufferedReader | None = None
        self._file_done = False
        self._suffix_bytes = suffix
        self._suffix_stream: io.BytesIO | None = io.BytesIO(suffix)
        try:
            file_size = file_path.stat().st_size
        except OSError as exc:
            raise ElabApiError(f"Cannot stat result attachment {file_path.name}: {exc}") from exc
        self._total_length = len(prefix) + file_size + len(suffix)

    @property
    def total_length(self) -> int:
        return self._total_length

    def __len__(self) -> int:
        return self._total_length

    def __bytes__(self) -> bytes:
        with self._file_path.open("rb") as stream:
            return self._prefix_bytes + stream.read() + self._suffix_bytes

    def __contains__(self, item: bytes) -> bool:
        if item in self._prefix_bytes or item in self._suffix_bytes:
            return True
        with self._file_path.open("rb") as stream:
            while chunk := stream.read(65536):
                if item in chunk:
                    return True
        return False

    def read(self, size: int = 65536) -> bytes:
        if self._prefix_stream is not None:
            chunk = self._prefix_stream.read(size)
            if chunk:
                return chunk
            self._prefix_stream = None

        if not self._file_done:
            if self._file_stream is None:
                try:
                    self._file_stream = self._file_path.open("rb")
                except OSError as exc:
                    raise ElabApiError(
                        f"Cannot open result attachment {self._file_path.name}: {exc}"
                    ) from exc
            chunk = self._file_stream.read(size)
            if chunk:
                return chunk
            self._file_stream.close()
            self._file_stream = None
            self._file_done = True

        if self._suffix_stream is not None:
            chunk = self._suffix_stream.read(size)
            if chunk:
                return chunk
            self._suffix_stream = None

        return b""

    def close(self) -> None:
        if self._file_stream is not None:
            self._file_stream.close()
            self._file_stream = None
        self._file_done = True


class ElabApiClient:
    """Authenticated eLabFTW v2 client with a testable HTTP opener."""

    def __init__(
        self,
        credentials: ElabCredentials,
        *,
        timeout_s: float = 30.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("eLab API timeout must be positive.")
        if not credentials.configured:
            raise ElabApiError("eLab credentials are not configured.")
        self._credentials = credentials
        self._timeout_s = float(timeout_s)
        self._opener = opener or urlopen

    @property
    def base_url(self) -> str:
        return self._credentials.api_base_url

    def list_experiment_templates(self) -> tuple[ElabTemplate, ...]:
        """Read accessible experiment templates, including all API pages."""

        templates: list[ElabTemplate] = []
        offset = 0
        limit = 100
        for _page in range(20):
            response = self._request(
                "GET",
                "/experiments_templates",
                params={
                    "limit": limit,
                    "offset": offset,
                    "order": "title",
                    "sort": "asc",
                    "state": "1",
                },
            )
            rows = self._list_payload(response.payload)
            page = tuple(self._template_from_payload(row) for row in rows)
            templates.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
        else:
            raise ElabApiError("eLab returned more than 2,000 experiment templates.")
        return tuple(sorted(templates, key=lambda item: (item.title.casefold(), item.id)))

    def test_connection(self) -> int:
        """Authenticate and return the number of accessible templates."""

        return len(self.list_experiment_templates())

    def create_experiment(
        self,
        *,
        template_id: int,
        title: str,
        body: str,
    ) -> tuple[int, str]:
        if template_id <= 0:
            raise ElabApiError("The eLab template ID must be positive.")
        if not title.strip():
            raise ElabApiError("The eLab experiment title cannot be empty.")
        response = self._request(
            "POST",
            "/experiments",
            payload={
                "title": title.strip(),
                "template": int(template_id),
                "body": body,
            },
        )
        location = self._header(response.headers, "location")
        experiment_id = self._id_from_location(location)
        if experiment_id is None and isinstance(response.payload, Mapping):
            experiment_id = self._positive_int(response.payload.get("id"))
        if experiment_id is None:
            raise ElabApiError("eLab created an experiment without returning its ID.")
        experiment_url = location or f"{self.base_url}/experiments/{experiment_id}"
        return experiment_id, experiment_url

    def add_tag(self, *, experiment_id: int, tag: str) -> None:
        clean_tag = str(tag).strip()
        if not clean_tag:
            return
        self._request(
            "POST",
            f"/experiments/{self._checked_id(experiment_id)}/tags",
            payload={"tag": clean_tag},
        )

    def upload_file(self, *, experiment_id: int, path: str | Path, comment: str = "") -> str:
        target = Path(path).expanduser()
        if not target.is_file():
            raise ElabApiError(f"Result attachment does not exist: {target}")
        boundary = f"----PyLabElab{uuid.uuid4().hex}"
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        safe_name = target.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        clean_comment = str(comment).strip()
        comment_part = (
            (
                f"\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="comment"\r\n\r\n'
                f"{clean_comment}\r\n"
            ).encode("utf-8")
            if clean_comment
            else b""
        )
        suffix = comment_part + f"\r\n--{boundary}--\r\n".encode("ascii")

        # Stream attachment in chunks without loading entire file into memory (NET-02)
        stream = MultipartFileStream(prefix, target, suffix)
        response = self._request(
            "POST",
            f"/experiments/{self._checked_id(experiment_id)}/uploads",
            data=stream,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return self._header(response.headers, "location") or ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
        data: bytes | Any | None = None,
        content_type: str | None = None,
    ) -> ElabApiResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        if payload is not None and data is not None:
            raise ValueError("Use either JSON payload or raw request data, not both.")
        request_data = data
        headers = {
            "Accept": "application/json",
            "User-Agent": "PyLab-eLabFTW/0.1",
        }
        if payload is not None:
            request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(url, data=request_data, headers=headers, method=method.upper())
        # Prevent credential leakage across cross-host redirects (NET-01)
        request.add_unredirected_header("Authorization", self._credentials.api_key)
        if hasattr(request_data, "total_length"):
            request.add_unredirected_header("Content-Length", str(request_data.total_length))
        try:
            with self._opener(request, timeout=self._timeout_s) as response:
                # NET-02: Bound response read to 10 MB to prevent unbounded memory allocation
                max_response_bytes = 10 * 1024 * 1024
                try:
                    raw = response.read(max_response_bytes + 1)
                except TypeError:
                    raw = response.read()
                if len(raw) > max_response_bytes:
                    raise ElabApiError(
                        f"eLab API response exceeded maximum allowable size of {max_response_bytes} bytes."
                    )
                status = int(response.getcode() or 0)
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
        except HTTPError as exc:
            raise ElabApiError(self._http_error_message(exc)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ElabApiError(f"Cannot reach eLab at {self.base_url}: {exc}") from exc
        return ElabApiResponse(status, response_headers, self._decode_payload(raw))

    @staticmethod
    def _decode_payload(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _list_payload(payload: Any) -> list[Mapping[str, Any]]:
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, Mapping):
            candidate = payload.get("data", payload.get("templates", payload.get("results")))
            rows = candidate if isinstance(candidate, list) else []
        else:
            rows = []
        return [row for row in rows if isinstance(row, Mapping)]

    @classmethod
    def _template_from_payload(cls, payload: Mapping[str, Any]) -> ElabTemplate:
        template_id = cls._positive_int(payload.get("id"))
        if template_id is None:
            raise ElabApiError("eLab returned a template without a valid ID.")
        title = str(
            payload.get("title") or payload.get("name") or f"Template #{template_id}"
        ).strip()
        return ElabTemplate(template_id, title, str(payload.get("body") or ""))

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @classmethod
    def _checked_id(cls, value: int) -> int:
        result = cls._positive_int(value)
        if result is None:
            raise ElabApiError("The eLab entity ID must be a positive integer.")
        return result

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        wanted = name.casefold()
        return next(
            (str(value) for key, value in headers.items() if str(key).casefold() == wanted), ""
        )

    @staticmethod
    def _id_from_location(location: str) -> int | None:
        if not location:
            return None
        path = urlsplit(location).path.rstrip("/")
        match = re.search(r"/(\d+)$", path)
        return int(match.group(1)) if match else None

    @classmethod
    def _http_error_message(cls, error: HTTPError) -> str:
        try:
            decoded = cls._decode_payload(error.read())
        except OSError:
            decoded = None
        if isinstance(decoded, Mapping):
            detail = decoded.get("description") or decoded.get("message") or decoded.get("error")
            if detail:
                return f"eLab API returned HTTP {error.code}: {detail}"
        return f"eLab API returned HTTP {error.code}: {error.reason}"
