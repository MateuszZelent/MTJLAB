"""Small dependency-free client for the eLabFTW REST API v2."""

from __future__ import annotations

from dataclasses import dataclass
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
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ElabApiError(f"Cannot read result attachment {target.name}: {exc}") from exc
        boundary = f"----PyLabElab{uuid.uuid4().hex}"
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        safe_name = target.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        parts = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8"),
            content,
        ]
        clean_comment = str(comment).strip()
        if clean_comment:
            parts.append(
                (
                    f"\r\n--{boundary}\r\n"
                    'Content-Disposition: form-data; name="comment"\r\n\r\n'
                    f"{clean_comment}\r\n"
                ).encode("utf-8")
            )
        parts.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
        response = self._request(
            "POST",
            f"/experiments/{self._checked_id(experiment_id)}/uploads",
            data=b"".join(parts),
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
        data: bytes | None = None,
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
            "Authorization": self._credentials.api_key,
            "User-Agent": "PyLab-eLabFTW/0.1",
        }
        if payload is not None:
            request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(url, data=request_data, headers=headers, method=method.upper())
        try:
            with self._opener(request, timeout=self._timeout_s) as response:
                raw = response.read()
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
