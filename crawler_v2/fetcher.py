from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import settings


class FetchError(RuntimeError):
    pass


class DisallowedHostError(FetchError):
    pass


class DocumentTooLargeError(FetchError):
    """
    Файл слишком велик для полной обработки.

    Отличается от `DownloadTooLargeError` тем, что документ признаётся
    годным, но обрабатывается только по реквизитам: он остаётся
    находимым по номеру, дате и названию, не занимая конвейер.
    """

    def __init__(self, message: str, *, size_bytes: int = 0) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes


class DownloadTooLargeError(FetchError):
    pass


class InsufficientDownloadSpaceError(FetchError):
    pass


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    requested_url: str
    final_url: str
    content_type: str
    body: bytes
    body_path: Path | None
    size_bytes: int
    etag: str | None
    last_modified: str | None

    @property
    def payload(self) -> bytes | Path:
        return (
            self.body_path
            if self.body_path is not None
            else self.body
        )

    def cleanup(self) -> None:
        if self.body_path is None:
            return

        try:
            self.body_path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

    def __del__(self) -> None:
        self.cleanup()


def normalize_content_type(
    value: str | None,
) -> str:
    if not value:
        return ""

    return value.split(
        ";",
        maxsplit=1,
    )[0].strip().lower()


def host_is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if parsed.scheme not in {
        "http",
        "https",
    }:
        return False

    for allowed_host in settings.allowed_hosts:
        if (
            hostname == allowed_host
            or hostname.endswith(
                "." + allowed_host
            )
        ):
            return True

    return False


def response_is_pdf(
    *,
    content_type: str,
    final_url: str,
    content_disposition: str | None,
) -> bool:
    if content_type == "application/pdf":
        return True

    path = unquote(
        urlparse(final_url).path
    ).casefold()

    if path.endswith(".pdf"):
        return True

    disposition = str(
        content_disposition
        or ""
    ).casefold()

    return ".pdf" in disposition


def ensure_download_capacity(
    directory: Path,
    *,
    incoming_bytes: int,
) -> None:
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    free_bytes = shutil.disk_usage(
        directory
    ).free
    required_bytes = (
        max(0, incoming_bytes)
        + settings.download_min_free_bytes
    )

    if free_bytes >= required_bytes:
        return

    raise InsufficientDownloadSpaceError(
        "Недостаточно свободного места для PDF: "
        f"свободно {free_bytes} байт, требуется "
        f"не менее {required_bytes} байт"
    )


def remove_stale_downloads(
    directory: Path,
    *,
    older_than_seconds: int = 24 * 60 * 60,
) -> None:
    try:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError:
        return

    threshold = time.time() - older_than_seconds

    for pattern in (
        "sochi-search-pdf-*.tmp",
        "sochi-search-pdf-*.tmp.writing",
    ):
        for path in directory.glob(
            pattern
        ):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink()
            except OSError:
                continue


class HttpFetcher:
    def __init__(self) -> None:
        remove_stale_downloads(
            settings.download_temp_dir
        )

        self.session = requests.Session()

        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=1,
            status_forcelist=(
                429,
                500,
                502,
                503,
                504,
            ),
            allowed_methods=frozenset(
                {
                    "GET",
                }
            ),
            respect_retry_after_header=True,
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )

        self.session.mount(
            "http://",
            adapter,
        )

        self.session.mount(
            "https://",
            adapter,
        )

        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/pdf;q=0.9,*/*;q=0.5"
                ),
                "Accept-Language": (
                    "ru-RU,ru;q=0.9,en;q=0.5"
                ),
            }
        )

    def fetch(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FetchResult:
        if not host_is_allowed(url):
            raise DisallowedHostError(
                f"Запрещённый адрес: {url}"
            )

        headers: dict[str, str] = {}

        if etag:
            headers["If-None-Match"] = etag

        if last_modified:
            headers["If-Modified-Since"] = (
                last_modified
            )

        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=(
                    settings.connect_timeout,
                    settings.read_timeout,
                ),
                allow_redirects=True,
                stream=True,
            )
        except requests.RequestException as exc:
            raise FetchError(
                f"Ошибка HTTP-запроса: {exc}"
            ) from exc

        try:
            if not host_is_allowed(response.url):
                raise DisallowedHostError(
                    "Перенаправление на запрещённый "
                    f"адрес: {response.url}"
                )

            content_type = normalize_content_type(
                response.headers.get(
                    "Content-Type"
                )
            )

            if response.status_code == 304:
                return FetchResult(
                    status_code=304,
                    requested_url=url,
                    final_url=response.url,
                    content_type=content_type,
                    body=b"",
                    body_path=None,
                    size_bytes=0,
                    etag=response.headers.get(
                        "ETag"
                    ),
                    last_modified=(
                        response.headers.get(
                            "Last-Modified"
                        )
                    ),
                )

            content_length = response.headers.get(
                "Content-Length"
            )

            declared_size = 0

            if content_length:
                try:
                    declared_size = int(
                        content_length
                    )
                except ValueError:
                    declared_size = 0

            # Признак PDF считается отдельно от решения о потоковой
            # записи на диск. Прежде оба вычислялись одним выражением с
            # условием `status_code < 400`, и файл с расширением .pdf мог
            # получить лимит, предназначенный для обычных страниц.
            looks_like_pdf = response_is_pdf(
                content_type=content_type,
                final_url=response.url,
                content_disposition=(
                    response.headers.get(
                        "Content-Disposition"
                    )
                ),
            )

            use_temporary_file = (
                response.status_code < 400
                and looks_like_pdf
            )

            max_bytes = (
                settings.pdf_max_download_bytes
                if looks_like_pdf
                else settings.max_download_bytes
            )

            if declared_size > max_bytes:
                if looks_like_pdf:
                    # Не ошибка обработки: документ существует и должен
                    # остаться в поиске по реквизитам.
                    raise DocumentTooLargeError(
                        f"Размер PDF {declared_size} байт превышает "
                        f"предел {max_bytes}; документ индексируется "
                        "только по реквизитам",
                        size_bytes=declared_size,
                    )

                raise DownloadTooLargeError(
                    "Размер ответа превышает "
                    f"{settings.max_download_bytes} байт"
                )

            temporary_path: Path | None = None
            temporary_file = None
            chunks: list[bytes] | None = (
                None
                if use_temporary_file
                else []
            )
            downloaded = 0

            if use_temporary_file:
                ensure_download_capacity(
                    settings.download_temp_dir,
                    incoming_bytes=declared_size,
                )

                temporary_file = tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="sochi-search-pdf-",
                    suffix=".tmp",
                    dir=str(
                        settings.download_temp_dir
                    ),
                    delete=False,
                )
                temporary_path = Path(
                    temporary_file.name
                )

            try:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024,
                ):
                    if not chunk:
                        continue

                    downloaded += len(chunk)

                    if downloaded > max_bytes:
                        label = (
                            "PDF"
                            if use_temporary_file
                            else "ответа"
                        )
                        raise (
                            DocumentTooLargeError
                            if use_temporary_file
                            else DownloadTooLargeError
                        )(
                            "Фактический размер "
                            f"{label} превысил "
                            f"{max_bytes} байт"
                        )

                    if temporary_file is not None:
                        temporary_file.write(chunk)

                        if downloaded % (32 * 1024 * 1024) < len(chunk):
                            ensure_download_capacity(
                                settings.download_temp_dir,
                                incoming_bytes=0,
                            )
                    else:
                        assert chunks is not None
                        chunks.append(chunk)

                if temporary_file is not None:
                    temporary_file.flush()
                    os.fsync(
                        temporary_file.fileno()
                    )

            except Exception:
                if temporary_file is not None:
                    temporary_file.close()

                if temporary_path is not None:
                    temporary_path.unlink(
                        missing_ok=True
                    )

                raise

            finally:
                if (
                    temporary_file is not None
                    and not temporary_file.closed
                ):
                    temporary_file.close()

            body = (
                b""
                if chunks is None
                else b"".join(chunks)
            )

            return FetchResult(
                status_code=response.status_code,
                requested_url=url,
                final_url=response.url,
                content_type=content_type,
                body=body,
                body_path=temporary_path,
                size_bytes=downloaded,
                etag=response.headers.get(
                    "ETag"
                ),
                last_modified=(
                    response.headers.get(
                        "Last-Modified"
                    )
                ),
            )

        finally:
            response.close()
