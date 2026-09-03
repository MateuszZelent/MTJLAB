"""Configuration and local credential handling for eLabFTW."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


class ElabConfigurationError(ValueError):
    """Raised when an eLab endpoint or upload profile is not usable."""


def resolve_env_path(path: str | Path = ".env") -> Path:
    """Resolve the local credential file consistently for the app and tests.

    A relative path is first resolved against the current working directory,
    which keeps packaged/local launches intuitive.  When that file is absent,
    fall back to the repository root so launching the station from an IDE,
    shortcut, or another working directory still finds the project ``.env``.
    """

    target = Path(path).expanduser()
    if target.is_absolute():
        return target
    working_directory_target = (Path.cwd() / target).resolve()
    if working_directory_target.exists():
        return working_directory_target
    repository_target = (Path(__file__).resolve().parents[3] / target).resolve()
    return repository_target


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without making dotenv a runtime dependency."""

    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ElabConfigurationError(f"Cannot read credential file {path}: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        if not separator or key.strip() not in {"ELAB_API", "ELAB_HOST"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = str(json.loads(value))
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def save_credentials(path: str | Path, *, host: str, api_key: str) -> None:
    """Atomically update only the two eLab keys in a local ``.env`` file."""

    target = resolve_env_path(path)
    normalized_host = normalize_api_base_url(host)
    # Persist the human-facing host rather than the derived /api/v2 URL while
    # retaining an optional deployment subpath (for example /elab).
    parsed = urlsplit(normalized_host)
    human_path = parsed.path
    if human_path.endswith("/api/v2"):
        human_path = human_path[: -len("/api/v2")]
    human_host = urlunsplit((parsed.scheme, parsed.netloc, human_path.rstrip("/"), "", ""))
    clean_key = api_key.strip()
    if not clean_key:
        raise ElabConfigurationError("The eLab API key cannot be empty.")

    try:
        existing = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    except OSError as exc:
        raise ElabConfigurationError(f"Cannot read credential file {target}: {exc}") from exc

    replacements = {
        "ELAB_HOST": _dotenv_value(human_host),
        "ELAB_API": _dotenv_value(clean_key),
    }
    output: list[str] = []
    written: set[str] = set()
    pattern = re.compile(r"^\s*(?:export\s+)?(ELAB_API|ELAB_HOST)\s*=")
    for line in existing.splitlines():
        match = pattern.match(line)
        if match:
            key = match.group(1)
            output.append(f"{key}={replacements[key]}")
            written.add(key)
        else:
            output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key in ("ELAB_HOST", "ELAB_API"):
        if key not in written:
            output.append(f"{key}={replacements[key]}")
    text = "\n".join(output).rstrip("\n") + "\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        # On POSIX this prevents accidental group/world reads. Windows keeps
        # the normal user ACL; chmod is harmless there and documents intent.
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ElabConfigurationError(f"Cannot save credential file {target}: {exc}") from exc


def normalize_api_base_url(host: str) -> str:
    """Return the canonical eLabFTW v2 API base URL."""

    raw = str(host or "").strip()
    if not raw:
        raise ElabConfigurationError("ELAB_HOST is not configured.")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.netloc:
        raise ElabConfigurationError(
            "ELAB_HOST must be an HTTP(S) URL, for example https://elab.example.org."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ElabConfigurationError(
            "ELAB_HOST must not contain credentials, query parameters or fragments."
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v2"):
        api_path = path
    elif path.endswith("/api"):
        api_path = f"{path}/v2"
    else:
        api_path = f"{path}/api/v2"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, api_path, "", "")).rstrip("/")


@dataclass(frozen=True, slots=True)
class ElabCredentials:
    """Runtime-only credentials; the API key is deliberately not in station YAML."""

    host: str
    api_key: str

    @property
    def api_base_url(self) -> str:
        return normalize_api_base_url(self.host)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip()) and bool(self.host.strip())

    @classmethod
    def from_values(cls, host: str, api_key: str) -> "ElabCredentials":
        normalized_host = str(host or "").strip()
        normalized_key = str(api_key or "").strip()
        if not normalized_host:
            raise ElabConfigurationError("ELAB_HOST is not configured.")
        if not normalized_key:
            raise ElabConfigurationError("ELAB_API is not configured.")
        normalize_api_base_url(normalized_host)
        return cls(normalized_host, normalized_key)


@dataclass(frozen=True, slots=True)
class ElabTemplateReference:
    """Non-secret template identity retained for quick selection."""

    id: int
    title: str


def load_credentials(
    env_path: str | Path = ".env",
    *,
    environ: Mapping[str, str] | None = None,
) -> ElabCredentials:
    """Load credentials from ``.env`` with process environment as fallback."""

    environment = environ if environ is not None else os.environ
    file_values = _read_dotenv(resolve_env_path(env_path))
    host = str(file_values.get("ELAB_HOST") or environment.get("ELAB_HOST") or "").strip()
    api_key = str(file_values.get("ELAB_API") or environment.get("ELAB_API") or "").strip()
    return ElabCredentials(host, api_key)


_DEFAULT_TITLE_PATTERN = "PyLab measurement {run_name}"


@dataclass(frozen=True, slots=True)
class ElabIntegrationProfile:
    """Persisted, non-secret policy for automatic result uploads."""

    enabled: bool = False
    template_id: int | None = None
    template_name: str = ""
    title_pattern: str = _DEFAULT_TITLE_PATTERN
    tags: tuple[str, ...] = ()
    upload_hdf5: bool = True
    upload_csv: bool = True
    recent_templates: tuple[ElabTemplateReference, ...] = ()
    favorite_templates: tuple[ElabTemplateReference, ...] = ()

    @classmethod
    def from_application(cls, application: Mapping[str, Any] | None) -> "ElabIntegrationProfile":
        raw = application.get("elab", {}) if isinstance(application, Mapping) else {}
        if not isinstance(raw, Mapping):
            raw = {}
        raw_template_id = raw.get("template_id")
        template_id: int | None
        if raw_template_id in (None, "", 0, "0"):
            template_id = None
        else:
            try:
                template_id = int(raw_template_id)
            except (TypeError, ValueError) as exc:
                raise ElabConfigurationError(
                    "The configured eLab template ID must be an integer."
                ) from exc
        raw_tags = raw.get("tags", ())
        if isinstance(raw_tags, str):
            tags = tuple(item.strip() for item in raw_tags.split(",") if item.strip())
        elif isinstance(raw_tags, (list, tuple)):
            tags = tuple(str(item).strip() for item in raw_tags if str(item).strip())
        else:
            tags = ()
        raw_recent = raw.get("recent_templates", ())
        recent: list[ElabTemplateReference] = []
        if isinstance(raw_recent, (list, tuple)):
            for item in raw_recent:
                if not isinstance(item, Mapping):
                    continue
                try:
                    recent_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if recent_id <= 0 or any(reference.id == recent_id for reference in recent):
                    continue
                recent.append(
                    ElabTemplateReference(
                        recent_id,
                        str(
                            item.get("title") or item.get("name") or f"Template #{recent_id}"
                        ).strip(),
                    )
                )
        raw_favorites = raw.get("favorite_templates", ())
        favorites: list[ElabTemplateReference] = []
        if isinstance(raw_favorites, (list, tuple)):
            for item in raw_favorites:
                if not isinstance(item, Mapping):
                    continue
                try:
                    fav_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if fav_id <= 0 or any(reference.id == fav_id for reference in favorites):
                    continue
                favorites.append(
                    ElabTemplateReference(
                        fav_id,
                        str(
                            item.get("title") or item.get("name") or f"Template #{fav_id}"
                        ).strip(),
                    )
                )
        profile = cls(
            enabled=bool(raw.get("enabled", False)),
            template_id=template_id,
            template_name=str(raw.get("template_name", "") or "").strip(),
            title_pattern=str(raw.get("title_pattern", _DEFAULT_TITLE_PATTERN) or "").strip(),
            tags=tuple(dict.fromkeys(tags)),
            upload_hdf5=bool(raw.get("upload_hdf5", True)),
            upload_csv=bool(raw.get("upload_csv", True)),
            recent_templates=tuple(recent[:8]),
            favorite_templates=tuple(favorites[:64]),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.template_id is not None and self.template_id <= 0:
            raise ElabConfigurationError("The eLab template ID must be a positive integer.")
        if not self.title_pattern:
            raise ElabConfigurationError("The eLab experiment title pattern cannot be empty.")
        try:
            self.title_pattern.format_map(
                {"run_name": "example", "status": "completed", "created_at": "2026-01-01T00:00:00Z"}
            )
        except (KeyError, ValueError) as exc:
            raise ElabConfigurationError(
                "The title pattern may use only {run_name}, {status} and {created_at}."
            ) from exc
        if not self.upload_hdf5 and not self.upload_csv:
            raise ElabConfigurationError("Select at least one result format to upload.")
        if any(len(tag) > 80 for tag in self.tags):
            raise ElabConfigurationError("An eLab tag cannot be longer than 80 characters.")
        if len(self.recent_templates) > 8:
            raise ElabConfigurationError("Keep at most eight recent eLab templates.")
        if any(reference.id <= 0 for reference in self.recent_templates):
            raise ElabConfigurationError("A recent eLab template ID must be positive.")
        if any(
            not reference.title or len(reference.title) > 255 for reference in self.recent_templates
        ):
            raise ElabConfigurationError(
                "A recent eLab template title must contain 1-255 characters."
            )
        if len(self.favorite_templates) > 64:
            raise ElabConfigurationError("Keep at most 64 favorite eLab templates.")
        if any(reference.id <= 0 for reference in self.favorite_templates):
            raise ElabConfigurationError("A favorite eLab template ID must be positive.")
        if any(
            not reference.title or len(reference.title) > 255 for reference in self.favorite_templates
        ):
            raise ElabConfigurationError(
                "A favorite eLab template title must contain 1-255 characters."
            )

    def render_title(self, *, run_name: str, status: str, created_at: str) -> str:
        self.validate()
        title = self.title_pattern.format(
            run_name=str(run_name), status=str(status), created_at=str(created_at)
        ).strip()
        if not title:
            raise ElabConfigurationError("The rendered eLab experiment title cannot be empty.")
        return title[:255]

    def to_raw(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "title_pattern": self.title_pattern,
            "tags": list(self.tags),
            "upload_hdf5": self.upload_hdf5,
            "upload_csv": self.upload_csv,
            "recent_templates": [
                {"id": reference.id, "title": reference.title}
                for reference in self.recent_templates
            ],
            "favorite_templates": [
                {"id": reference.id, "title": reference.title}
                for reference in self.favorite_templates
            ],
        }

    def remember_template(self, template_id: int, title: str) -> "ElabIntegrationProfile":
        """Return a profile with the selected template at the front of history."""

        reference = ElabTemplateReference(
            int(template_id), str(title).strip() or f"Template #{int(template_id)}"
        )
        recent = (reference,) + tuple(
            item for item in self.recent_templates if item.id != reference.id
        )
        return replace(self, recent_templates=recent[:8])

    def is_favorite(self, template_id: int | None) -> bool:
        """Return True if the template ID is currently in the favorites list."""
        if template_id is None:
            return False
        try:
            tid = int(template_id)
        except (ValueError, TypeError):
            return False
        return any(reference.id == tid for reference in self.favorite_templates)

    def with_favorite_template(self, template_id: int, title: str) -> "ElabIntegrationProfile":
        """Return a profile with the specified template added to favorites."""
        try:
            tid = int(template_id)
        except (ValueError, TypeError) as exc:
            raise ElabConfigurationError("A favorite template ID must be an integer.") from exc
        if self.is_favorite(tid):
            return self
        reference = ElabTemplateReference(
            tid, str(title).strip() or f"Template #{tid}"
        )
        updated = self.favorite_templates + (reference,)
        new_profile = replace(self, favorite_templates=updated[:64])
        new_profile.validate()
        return new_profile

    def without_favorite_template(self, template_id: int) -> "ElabIntegrationProfile":
        """Return a profile with the specified template removed from favorites."""
        try:
            tid = int(template_id)
        except (ValueError, TypeError) as exc:
            raise ElabConfigurationError("A favorite template ID must be an integer.") from exc
        updated = tuple(ref for ref in self.favorite_templates if ref.id != tid)
        return replace(self, favorite_templates=updated)

    def toggle_favorite(self, template_id: int, title: str) -> "ElabIntegrationProfile":
        """Add or remove a template from favorites."""
        if self.is_favorite(template_id):
            return self.without_favorite_template(template_id)
        return self.with_favorite_template(template_id, title)

    def with_overrides(
        self,
        *,
        enabled: bool | None = None,
        template_id: int | None = None,
        template_name: str | None = None,
        title_pattern: str | None = None,
        tags: Any | None = None,
        upload_hdf5: bool | None = None,
        upload_csv: bool | None = None,
    ) -> "ElabIntegrationProfile":
        """Return a copy of this profile with the specified per-run or per-node overrides."""

        effective_template_id = self.template_id if template_id is None else template_id
        effective_template_name = (
            self.template_name if template_name is None else str(template_name)
        )
        effective_title_pattern = (
            self.title_pattern if title_pattern is None else str(title_pattern)
        )
        effective_tags = (
            self.tags
            if tags is None
            else tuple(dict.fromkeys(str(t).strip() for t in tags if str(t).strip()))
        )
        effective_hdf5 = self.upload_hdf5 if upload_hdf5 is None else bool(upload_hdf5)
        effective_csv = self.upload_csv if upload_csv is None else bool(upload_csv)
        effective_enabled = self.enabled if enabled is None else bool(enabled)
        new_profile = replace(
            self,
            enabled=effective_enabled,
            template_id=effective_template_id,
            template_name=effective_template_name,
            title_pattern=effective_title_pattern,
            tags=effective_tags,
            upload_hdf5=effective_hdf5,
            upload_csv=effective_csv,
        )
        new_profile.validate()
        return new_profile

