import os
import json
import base64
import time
import httpx
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple, Set, Union
from cachetools import TTLCache
from app.config import settings
from app.dto_utils import ensure_page_dto, ensure_book_dto, raw_app_book_to_dto, ensure_series_dto, disambiguate_series_dto
from app.db import db
import asyncio
import logging

logger = logging.getLogger("grimmory-komga-bridge")

# In-memory caches:
# Cache JWT tokens for native Grimmory API (1 hour TTL)
token_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
# Cache page info per book (1 hour TTL)
page_cache: TTLCache = TTLCache(maxsize=5000, ttl=3600)
# Cache page count per book (24 hour TTL)
page_count_cache: TTLCache = TTLCache(maxsize=10000, ttl=86400)
class ReadProgressCache(TTLCache):
    """TTL Cache for read progress that supports compound key (user:book_id)
    while maintaining fallback lookup when queried by bare book_id."""
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            if isinstance(key, str) and ":" not in key:
                suffix = f":{key}"
                for k, v in list(self.items()):
                    if k.endswith(suffix):
                        return v
            raise

    def __contains__(self, key):
        if super().__contains__(key):
            return True
        if isinstance(key, str) and ":" not in key:
            suffix = f":{key}"
            return any(k.endswith(suffix) for k in list(self.keys()))
        return False

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, default=None):
        try:
            return super().pop(key)
        except KeyError:
            if isinstance(key, str) and ":" not in key:
                suffix = f":{key}"
                matched = [k for k in list(self.keys()) if k.endswith(suffix)]
                if matched:
                    return super().pop(matched[0])
            return default

# Cache read progress per book (30 days TTL)
read_progress_cache: ReadProgressCache = ReadProgressCache(maxsize=10000, ttl=86400 * 30)
# Cache book DTOs (5 min TTL)
book_cache: TTLCache = TTLCache(maxsize=5000, ttl=300)
def get_thumbnail_path(target_type: str, item_id: str) -> str:
    safe_id = "".join(c for c in str(item_id) if c.isalnum() or c in ("-", "_"))
    dir_path = os.path.join(settings.THUMBNAILS_DIR, target_type)
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{safe_id}.img")

def detect_image_type(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    elif content.startswith(b"\x89PNG"):
        return "image/png"
    elif content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        return "image/webp"
    elif content.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"

def get_cached_thumbnail(target_type: str, item_id: str) -> Optional[Tuple[bytes, str]]:
    """Retrieve thumbnail from persistent disk storage (mount point)."""
    path = get_thumbnail_path(target_type, item_id)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                content = f.read()
            if content:
                return content, detect_image_type(content)
        except Exception:
            pass
    return None

def save_cached_thumbnail(target_type: str, item_id: str, content: bytes, content_type: str = "image/jpeg") -> None:
    """Save thumbnail to persistent disk storage (mount point)."""
    if not content:
        return
    path = get_thumbnail_path(target_type, item_id)
    try:
        with open(path, "wb") as f:
            f.write(content)
    except Exception as e:
        logger.debug(f"[Thumbnail] Could not save thumbnail to disk: {e}")

class DiskThumbnailCache:
    """Disk-backed thumbnail cache stored on persistent mount point (zero RAM usage)."""
    def __getitem__(self, key: str) -> Tuple[bytes, str]:
        t_type, item_id = key.split(":", 1) if ":" in key else ("books", key)
        cached = get_cached_thumbnail("books" if t_type == "b" else "series" if t_type == "s" else t_type, item_id)
        if cached is not None:
            return cached
        raise KeyError(key)

    def __setitem__(self, key: str, value: Tuple[bytes, str]):
        t_type, item_id = key.split(":", 1) if ":" in key else ("books", key)
        content, c_type = value
        save_cached_thumbnail("books" if t_type == "b" else "series" if t_type == "s" else t_type, item_id, content, c_type)

    def __contains__(self, key: str) -> bool:
        t_type, item_id = key.split(":", 1) if ":" in key else ("books", key)
        path = get_thumbnail_path("books" if t_type == "b" else "series" if t_type == "s" else t_type, item_id)
        return os.path.exists(path)

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self):
        try:
            import shutil
            if os.path.exists(settings.THUMBNAILS_DIR):
                shutil.rmtree(settings.THUMBNAILS_DIR)
            os.makedirs(settings.THUMBNAILS_DIR, exist_ok=True)
        except Exception:
            pass

# Disk-backed thumbnail cache (uses persistent volume mount point, zero RAM bloating)
thumbnail_cache: DiskThumbnailCache = DiskThumbnailCache()
# Active reading sessions: session_key -> dict
active_sessions: Dict[str, Dict[str, Any]] = {}

def load_progress_cache():
    """Load read progress from persistent SQLite database into in-memory cache."""
    try:
        for k, v in db.get_all_read_progress_map().items():
            if isinstance(v, dict):
                read_progress_cache[k] = v
    except Exception:
        pass

load_progress_cache()


class GrimmoryClient:
    def __init__(self):
        self.base_url = settings.GRIMMORY_URL
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop = None
        self.custom_series: Dict[str, Dict[str, Any]] = {}
        self.custom_series_books_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)
        self.all_series_cache: TTLCache = TTLCache(maxsize=100, ttl=60)
        self.user_libraries_cache: TTLCache = TTLCache(maxsize=100, ttl=60)
        self.last_credentials: Optional[Tuple[str, str]] = None
        self._has_reconciled_progress = False

    def get_client(self) -> httpx.AsyncClient:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._client is None or self._client.is_closed or self._client_loop != loop:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0, connect=20.0),
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=100, max_connections=200)
            )
            self._client_loop = loop
        return self._client

    @property
    def client(self) -> httpx.AsyncClient:
        return self.get_client()

    def extract_credentials(self, auth_header: Optional[str]) -> Tuple[str, str]:
        """Extract username and password from Authorization header (Basic auth) or default settings."""
        if auth_header and auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                if ":" in decoded:
                    user, pwd = decoded.split(":", 1)
                    if user in settings.USER_MAPPINGS:
                        mapped_user, mapped_pwd = settings.USER_MAPPINGS[user]
                        self.last_credentials = (mapped_user, mapped_pwd)
                        return mapped_user, mapped_pwd
                    if user and pwd:
                        self.last_credentials = (user, pwd)
                    return user, pwd
            except Exception:
                pass
        if self.last_credentials:
            return self.last_credentials
        return settings.DEFAULT_USERNAME, settings.DEFAULT_PASSWORD

    def get_basic_auth_header(self, user: str, pwd: str) -> Dict[str, str]:
        if not user:
            return {}
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def get_native_token(self, user: str, pwd: str) -> Optional[str]:
        """Obtain or return cached JWT token for Grimmory's native API."""
        if not user:
            return None
        cache_key = f"{user}:{pwd}"
        if cache_key in token_cache:
            return token_cache[cache_key]

        try:
            resp = await self.client.post(
                "/api/v1/auth/login",
                json={"username": user, "password": pwd},
                headers={"Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("accessToken")
                if token:
                    token_cache[cache_key] = token
                    return token
        except Exception:
            pass
        return None

    async def get_native_headers(self, user: str, pwd: str) -> Dict[str, str]:
        token = await self.get_native_token(user, pwd)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return self.get_basic_auth_header(user, pwd)

    async def get_libraries(self, user: str, pwd: str) -> List[Dict[str, Any]]:
        """Fetch libraries via Grimmory native API."""
        native_headers = await self.get_native_headers(user, pwd)
        for path in ["/api/v1/libraries", "/api/v1/app/libraries"]:
            try:
                resp = await self.client.get(path, headers=native_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict) and "content" in data:
                        return data["content"]
            except Exception:
                pass
        return []

    async def get_user_library_ids(self, user: str, pwd: str) -> Optional[Set[str]]:
        """Fetch the set of library IDs that the given user has access to."""
        cache_key = f"{user}:{pwd}"
        if cache_key in self.user_libraries_cache:
            return self.user_libraries_cache[cache_key]

        try:
            libs = await self.get_libraries(user, pwd)
            if libs and isinstance(libs, list):
                lib_ids = {str(lib.get("id")) for lib in libs if lib.get("id")}
                self.user_libraries_cache[cache_key] = lib_ids
                return lib_ids
        except Exception:
            pass

        return None

    async def user_can_access_library(self, library_id: str, user: str, pwd: str) -> bool:
        """Verify whether user has access to this library."""
        if not library_id:
            return True
        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            return str(library_id) in user_libs
        return True

    async def user_can_access_item(self, dto: Dict[str, Any], user: str, pwd: str) -> bool:
        """Verify whether user has access to the item's library."""
        if not dto or not isinstance(dto, dict):
            return False
        lib_id = str(dto.get("libraryId") or dto.get("library_id") or "")
        if not lib_id:
            return True
        return await self.user_can_access_library(lib_id, user, pwd)

    # Aliases for backward compatibility
    async def user_can_access_book(self, book_dto: Dict[str, Any], user: str, pwd: str) -> bool:
        return await self.user_can_access_item(book_dto, user, pwd)

    async def user_can_access_series(self, series_dto: Dict[str, Any], user: str, pwd: str) -> bool:
        return await self.user_can_access_item(series_dto, user, pwd)

    async def native_request(
        self,
        method: str,
        path: str,
        user: str,
        pwd: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> httpx.Response:
        """Make request to Grimmory native API with JWT Bearer auth."""
        req_headers = await self.get_native_headers(user, pwd)
        if headers:
            req_headers.update(headers)
        return await self.client.request(
            method,
            path,
            params=params,
            json=json_data,
            headers=req_headers
        )

    async def fetch_and_cache_native_books(
        self,
        user: str,
        pwd: str,
        library_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch all books from native Grimmory /api/v1/books and synchronize them into SQLite cache."""
        native_headers = await self.get_native_headers(user, pwd)
        raw_books = []
        paths = ["/api/v1/books", "/api/v1/app/books"]
        for p in paths:
            try:
                params = {"stripForListView": "false"}
                if library_id:
                    params["libraryId"] = str(library_id)
                resp = await self.client.get(p, headers=native_headers, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        raw_books = data
                        break
                    elif isinstance(data, dict):
                        raw_books = data.get("content", [])
                        break
            except Exception:
                pass

        if not raw_books:
            return []

        all_book_dtos = []
        series_map: Dict[str, Dict[str, Any]] = {}
        for b in raw_books:
            try:
                dto = raw_app_book_to_dto(b)
                all_book_dtos.append(dto)
                s_id = dto.get("seriesId")
                lib = str(dto.get("libraryId", "1"))
                s_title = dto.get("seriesTitle") or dto.get("name")
                if s_id and s_id not in series_map:
                    series_map[s_id] = {
                        "id": s_id,
                        "libraryId": lib,
                        "name": s_title,
                        "url": f"/api/v1/series/{s_id}",
                        "created": dto.get("created", ""),
                        "lastModified": dto.get("lastModified", ""),
                        "booksCount": 0,
                        "oneshot": dto.get("oneshot", False),
                        "books": []
                    }
                if s_id in series_map:
                    series_map[s_id]["books"].append(dto)
            except Exception:
                pass

        for s_id, s_info in series_map.items():
            books = s_info.pop("books", [])
            s_info["booksCount"] = len(books)
            s_dto = ensure_series_dto(s_info)
            self.register_custom_series(s_id, s_info["libraryId"], s_info["name"], s_dto)
            db.save_series(s_dto)

        if all_book_dtos:
            db.save_books_batch(all_book_dtos)

        return all_book_dtos

    async def get_book_pages_metadata(self, book_id: str, user: str, pwd: str) -> List[Dict[str, Any]]:
        """
        Fetch page metadata for a book.
        Grimmory's /komga/api/v1/books/{id}/pages returns [], so we construct
        accurate PageDto objects from native cbx page dimensions & info.
        """
        if book_id in page_cache:
            return page_cache[book_id]

        # Check persistent database
        db_pages_info = db.get_book_pages(book_id)
        if db_pages_info:
            cached_pages, cached_count = db_pages_info
            if cached_pages:
                page_cache[book_id] = cached_pages
                page_count_cache[book_id] = cached_count or len(cached_pages)
                return cached_pages

        native_headers = await self.get_native_headers(user, pwd)

        # 1. Try CBX pages list: /api/v1/cbx/{book_id}/pages returns array of page numbers e.g. [1, 2, 3, ...]
        page_numbers: List[int] = []
        try:
            pages_resp = await self.client.get(f"/api/v1/cbx/{book_id}/pages", headers=native_headers)
            if pages_resp.status_code == 200:
                data = pages_resp.json()
                if isinstance(data, list) and len(data) > 0:
                    page_numbers = [int(p) for p in data if str(p).isdigit()]
        except Exception:
            pass

        # 2. Try CBX page dimensions: /api/v1/cbx/{book_id}/page-dimensions
        dimensions_list: List[Dict[str, Any]] = []
        try:
            dim_resp = await self.client.get(f"/api/v1/cbx/{book_id}/page-dimensions", headers=native_headers)
            if dim_resp.status_code == 200:
                d = dim_resp.json()
                if isinstance(d, list):
                    dimensions_list = d
        except Exception:
            pass

        # 3. Try CBX page info: /api/v1/cbx/{book_id}/page-info
        info_dict: Dict[int, str] = {}
        try:
            info_resp = await self.client.get(f"/api/v1/cbx/{book_id}/page-info", headers=native_headers)
            if info_resp.status_code == 200:
                d = info_resp.json()
                if isinstance(d, list):
                    for item in d:
                        if isinstance(item, dict):
                            p_num = item.get("pageNumber") if item.get("pageNumber") is not None else item.get("page")
                            p_name = item.get("displayName") or item.get("fileName")
                            if p_num is not None:
                                try:
                                    info_dict[int(p_num)] = p_name or f"{int(p_num):03d}"
                                except Exception:
                                    pass
        except Exception:
            pass

        pages: List[Dict[str, Any]] = []

        if page_numbers:
            for idx, p_num in enumerate(page_numbers, start=1):
                num = p_num if p_num > 0 else idx
                dim = dimensions_list[idx - 1] if idx - 1 < len(dimensions_list) and isinstance(dimensions_list[idx - 1], dict) else {}
                file_name = info_dict.get(num, f"{num:03d}")
                if not any(file_name.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    file_name = f"{file_name}.jpg"
                pages.append({
                    "number": num,
                    "fileName": file_name,
                    "mediaType": "image/jpeg",
                    "width": dim.get("width", 1080),
                    "height": dim.get("height", 1920),
                    "sizeBytes": 0,
                    "size": "0 B"
                })
        elif dimensions_list:
            for idx, dim in enumerate(dimensions_list, start=1):
                raw_num = dim.get("pageNumber") if isinstance(dim, dict) and dim.get("pageNumber") is not None else dim.get("page") if isinstance(dim, dict) else None
                try:
                    num = int(raw_num) if raw_num is not None and int(raw_num) > 0 else idx
                except Exception:
                    num = idx
                width = dim.get("width", 1080) if isinstance(dim, dict) else 1080
                height = dim.get("height", 1920) if isinstance(dim, dict) else 1920
                file_name = info_dict.get(num, f"{num:03d}")
                if not any(file_name.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    file_name = f"{file_name}.jpg"
                pages.append({
                    "number": num,
                    "fileName": file_name,
                    "mediaType": "image/jpeg",
                    "width": width,
                    "height": height,
                    "sizeBytes": 0,
                    "size": "0 B"
                })
        else:
            # 4. Try PDF pages: /api/v1/pdf/{book_id}/pages
            try:
                pdf_resp = await self.client.get(f"/api/v1/pdf/{book_id}/pages", headers=native_headers)
                if pdf_resp.status_code == 200:
                    pdf_data = pdf_resp.json()
                    count = pdf_data if isinstance(pdf_data, int) else len(pdf_data) if isinstance(pdf_data, list) else 0
                    for p in range(1, count + 1):
                        pages.append({
                            "number": p,
                            "fileName": f"{p:03d}.jpg",
                            "mediaType": "image/jpeg",
                            "width": 1080,
                            "height": 1920,
                            "sizeBytes": 0,
                            "size": "0 B"
                        })
            except Exception:
                pass


        if not pages:
            page_count = await self.get_book_page_count(book_id, user, pwd)
            is_epub = False
            if book_id in book_cache and "epub" in str(book_cache[book_id].get("media", {}).get("mediaType", "")).lower():
                is_epub = True
            db_b = db.get_book(book_id)
            if db_b and "epub" in str(db_b.get("media", {}).get("mediaType", "")).lower():
                is_epub = True

            if page_count > 1:
                ext = "xhtml" if is_epub else "jpg"
                m_type = "application/xhtml+xml" if is_epub else "image/jpeg"
                pages = [{
                    "number": p,
                    "fileName": f"page_{p:03d}.{ext}",
                    "mediaType": m_type,
                    "width": 1080 if not is_epub else None,
                    "height": 1920 if not is_epub else None,
                    "sizeBytes": 0,
                    "size": "0 B"
                } for p in range(1, page_count + 1)]
            else:
                pages = [{
                    "number": 1,
                    "fileName": "001.xhtml" if is_epub else "001.jpg",
                    "mediaType": "application/xhtml+xml" if is_epub else "image/jpeg",
                    "width": 1080 if not is_epub else None,
                    "height": 1920 if not is_epub else None,
                    "sizeBytes": 0,
                    "size": "0 B"
                }]

        page_cache[book_id] = pages
        page_count_cache[book_id] = len(pages)
        try:
            db.save_book_pages(book_id, pages, len(pages))
        except Exception:
            pass
        return pages

    async def get_book_page_count(self, book_id: str, user: str, pwd: str) -> int:
        """Get the page count for a book, using cache or querying Grimmory endpoints."""
        if book_id in page_count_cache:
            return page_count_cache[book_id]
        if book_id in page_cache and len(page_cache[book_id]) > 0:
            count = len(page_cache[book_id])
            page_count_cache[book_id] = count
            return count

        db_count = db.get_book_page_count(book_id)
        if db_count is not None and db_count > 0:
            page_count_cache[book_id] = db_count
            return db_count

        native_headers = await self.get_native_headers(user, pwd)

        # 1. Try CBX pages: /api/v1/cbx/{book_id}/pages returns [1, 2, ...]
        try:
            resp = await self.client.get(f"/api/v1/cbx/{book_id}/pages", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    count = len(data)
                    page_count_cache[book_id] = count
                    return count
        except Exception:
            pass

        # 2. Try CBX page-dimensions
        try:
            resp = await self.client.get(f"/api/v1/cbx/{book_id}/page-dimensions", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    count = len(data)
                    page_count_cache[book_id] = count
                    return count
        except Exception:
            pass

        # 3. Try PDF pages: /api/v1/pdf/{book_id}/pages
        try:
            resp = await self.client.get(f"/api/v1/pdf/{book_id}/pages", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                count = data if isinstance(data, int) else len(data) if isinstance(data, list) else 0
                if count > 0:
                    page_count_cache[book_id] = count
                    return count
        except Exception:
            pass

        # 4. Try EPUB pages/chapters/spine: /api/v1/epub/{book_id}/pages, /chapters, /spine
        for epub_path in [
            f"/api/v1/epub/{book_id}/pages",
            f"/api/v1/epub/{book_id}/chapters",
            f"/api/v1/epub/{book_id}/spine"
        ]:
            try:
                resp = await self.client.get(epub_path, headers=native_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    count = data if isinstance(data, int) else len(data) if isinstance(data, list) else 0
                    if count > 0:
                        page_count_cache[book_id] = count
                        return count
            except Exception:
                pass

        # 5. Try native app book info: /api/v1/app/books/{book_id}
        try:
            resp = await self.client.get(f"/api/v1/app/books/{book_id}", headers=native_headers)
            if resp.status_code == 200:
                raw = resp.json()
                if isinstance(raw, dict):
                    raw_pc = raw.get("pageCount") or raw.get("pagesCount") or raw.get("pages") or raw.get("numberOfPages")
                    if isinstance(raw_pc, int) and raw_pc > 0:
                        page_count_cache[book_id] = raw_pc
                        return raw_pc
                    # Check epubProgress
                    epub_prog = raw.get("epubProgress")
                    if isinstance(epub_prog, dict):
                        p = epub_prog.get("page", 0)
                        pct = epub_prog.get("percentage", 0)
                        if p > 0 and pct > 0:
                            calc = round(p * 100 / pct)
                            if calc > 0:
                                page_count_cache[book_id] = calc
                                return calc
                    # If EPUB, calculate from fileSizeKb
                    if raw.get("primaryFileType") == "EPUB":
                        file_size_kb = raw.get("fileSizeKb", 0)
                        if file_size_kb > 0:
                            usable_kb = max(5, file_size_kb - 60)
                            calc = max(1, int(usable_kb / 2.0))
                            page_count_cache[book_id] = calc
                            return calc
        except Exception:
            pass


        # 7. Check if cached book or DB has sizeBytes and is EPUB
        if book_id in book_cache:
            b = book_cache[book_id]
            m_type = str(b.get("media", {}).get("mediaType", "")).lower()
            if "epub" in m_type:
                size_kb = (b.get("sizeBytes") or 0) // 1024
                if size_kb > 0:
                    usable_kb = max(5, size_kb - 60)
                    calc = max(1, int(usable_kb / 2.0))
                    page_count_cache[book_id] = calc
                    return calc

        db_b = db.get_book(book_id)
        if db_b:
            m_type = str(db_b.get("media", {}).get("mediaType", "")).lower()
            if "epub" in m_type:
                size_kb = (db_b.get("sizeBytes") or 0) // 1024
                if size_kb > 0:
                    usable_kb = max(5, size_kb - 60)
                    calc = max(1, int(usable_kb / 2.0))
                    page_count_cache[book_id] = calc
                    return calc

        return 1

    async def enrich_books_page_count(self, books: List[Dict[str, Any]], user: str, pwd: str) -> None:
        """Concurrently populate accurate pagesCount and readProgress for a list of book DTOs."""
        if not books:
            return

        u = (user or "default").lower().strip()

        async def _enrich_one(book: Dict[str, Any]):
            book_id = str(book.get("id"))
            if not book_id or book_id == "None":
                return
            count = await self.get_book_page_count(book_id, user, pwd)
            if "media" not in book or not isinstance(book["media"], dict):
                book["media"] = {"status": "READY", "mediaType": "application/x-cbz", "mediaProfile": "DIVINA"}
            if count > 0:
                book["media"]["pagesCount"] = count

            # Attach read progress from in-memory cache, SQLite DB, or embedded progress if missing
            if "readProgress" not in book or book.get("readProgress") is None:
                p_key = f"{u}:{book_id}"
                prog = read_progress_cache.get(p_key) or (read_progress_cache.get(book_id) if u == "default" else None) or db.get_read_progress(u, book_id)
                if prog:
                    book["readProgress"] = prog
                else:
                    for p_key_f in ["cbxProgress", "pdfProgress", "epubProgress"]:
                        p = book.get(p_key_f)
                        if isinstance(p, dict) and p.get("page", 0) > 0:
                            book["readProgress"] = {
                                "page": p.get("page", 1),
                                "completed": False,
                                "readDate": book.get("lastRead") or book.get("coverUpdatedOn") or "2026-09-24T00:00:00Z",
                                "created": "2026-09-24T00:00:00Z",
                                "lastModified": "2026-09-24T00:00:00Z",
                                "deviceId": "komic",
                                "deviceName": "Komic"
                            }
                            break

        await asyncio.gather(*[_enrich_one(b) for b in books], return_exceptions=True)
        try:
            db.save_books_batch(books)
        except Exception:
            pass

    async def get_book_dto(self, book_id: str, user: str, pwd: str) -> Optional[Dict[str, Any]]:
        """Fetch book DTO from Grimmory's Komga layer and enrich it."""
        u = (user or "default").lower().strip()
        b_key = f"{u}:{book_id}"
        if b_key in book_cache:
            cached_b = book_cache[b_key]
            if await self.user_can_access_book(cached_b, user, pwd):
                return cached_b
            return None

        # Check persistent database
        db_book = db.get_book(book_id)
        if db_book:
            if not await self.user_can_access_book(db_book, user, pwd):
                return None
            db_book = dict(db_book)
            p_key = f"{u}:{book_id}"
            prog = read_progress_cache.get(p_key) or (read_progress_cache.get(book_id) if u == "default" else None) or db.get_read_progress(u, book_id)
            db_book["readProgress"] = prog
            ensure_book_dto(db_book)
            book_cache[b_key] = db_book
            return db_book

        native_headers = await self.get_native_headers(user, pwd)
        for p in [f"/api/v1/books/{book_id}", f"/api/v1/app/books/{book_id}"]:
            try:
                resp = await self.client.get(p, headers=native_headers)
                if resp.status_code == 200:
                    raw_data = resp.json()
                    book = raw_app_book_to_dto(raw_data)
                    await self.enrich_book(book, user, pwd, fetch_dimensions=True)
                    book_cache[b_key] = book
                    try:
                        db.save_book(book)
                    except Exception:
                        pass
                    return book
            except Exception:
                pass
        return None

    async def enrich_book(self, book: Dict[str, Any], user: str, pwd: str, fetch_dimensions: bool = False) -> None:
        """Ensure all required BookDto fields are present and optionally attach dimensions & progress."""
        book_id = str(book.get("id"))
        if fetch_dimensions or book_id in page_cache:
            pages = await self.get_book_pages_metadata(book_id, user, pwd)
            if pages:
                if "media" not in book or not isinstance(book["media"], dict):
                    book["media"] = {"status": "READY", "mediaType": "application/x-cbz", "mediaProfile": "DIVINA"}
                book["media"]["pagesCount"] = len(pages)
        elif book_id in page_count_cache:
            if "media" not in book or not isinstance(book["media"], dict):
                book["media"] = {"status": "READY", "mediaType": "application/x-cbz", "mediaProfile": "DIVINA"}
            book["media"]["pagesCount"] = page_count_cache[book_id]

        # Check and attach readProgress if missing or None
        if "readProgress" not in book or book.get("readProgress") is None:
            progress = await self.get_read_progress(book_id, user, pwd)
            if progress:
                book["readProgress"] = progress
            else:
                book["readProgress"] = None

        ensure_book_dto(book)

    async def get_read_progress(self, book_id: str, user: str, pwd: str) -> Optional[Dict[str, Any]]:
        """Fetch read progress from Grimmory native API and format as Komga ReadProgressDto."""
        u = (user or "default").lower().strip()
        p_key = f"{u}:{book_id}"
        if p_key in read_progress_cache:
            return read_progress_cache[p_key]
        if u == "default" and book_id in read_progress_cache:
            return read_progress_cache[book_id]

        db_prog = db.get_read_progress(u, book_id)
        if db_prog:
            read_progress_cache[p_key] = db_prog
            if u == "default":
                read_progress_cache[book_id] = db_prog
            return db_prog

        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get(f"/api/v1/app/books/{book_id}/progress", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                if not data or not isinstance(data, dict):
                    read_progress_cache[p_key] = None
                    if u == "default":
                        read_progress_cache[book_id] = None
                    return None
                
                page = 1
                completed = False
                pct = 0

                # 1. Native format progress
                if "cbxProgress" in data and isinstance(data["cbxProgress"], dict):
                    page = data["cbxProgress"].get("page", 1)
                    pct = float(data["cbxProgress"].get("percentage", 0))
                elif "pdfProgress" in data and isinstance(data["pdfProgress"], dict):
                    page = data["pdfProgress"].get("page", 1)
                    pct = float(data["pdfProgress"].get("percentage", 0))
                elif "epubProgress" in data and isinstance(data["epubProgress"], dict):
                    page = data["epubProgress"].get("page", 1)
                    pct = float(data["epubProgress"].get("percentage", 0))

                # 2. Grimmory float percentage (e.g. 0.05 -> 5% or 1.0 -> 100%)
                if "readProgress" in data and isinstance(data["readProgress"], (int, float)):
                    pct_val = float(data["readProgress"])
                    pct = round(pct_val * 100) if pct_val <= 1.0 else round(pct_val)
                elif "koreaderProgress" in data and isinstance(data["koreaderProgress"], dict):
                    k_pct = float(data["koreaderProgress"].get("percentage", 0))
                    pct = round(k_pct * 100) if k_pct <= 1.0 else round(k_pct)

                read_status = str(data.get("readStatus") or data.get("status") or "").upper().strip()
                date_finished = data.get("dateFinished") or (data.get("lastReadTime") if read_status == "READ" else None)
                last_read = data.get("lastReadTime") or date_finished

                if (
                    date_finished or data.get("completed") is True or data.get("isRead") is True or
                    read_status == "READ" or pct >= 100
                ):
                    completed = True

                # Check if total pages is known and page >= total pages
                total_pages = await self.get_book_page_count(book_id, user, pwd)
                if total_pages > 1 and page >= total_pages:
                    completed = True

                if completed:
                    page = max(1, total_pages)
                    pct = 100
                elif not completed and page <= 1 and pct > 0 and total_pages > 1:
                    page = max(1, round(pct * total_pages / 100))

                # If book is unread or never opened
                if not completed and (read_status == "UNREAD" or (pct == 0 and page <= 1 and not last_read and data.get("progressValid") is False)):
                    read_progress_cache[p_key] = None
                    if u == "default":
                        read_progress_cache[book_id] = None
                    db.delete_read_progress(u, book_id)
                    return None

                now_iso = last_read or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                progress_dto = {
                    "page": page,
                    "completed": completed,
                    "readDate": date_finished or now_iso,
                    "created": now_iso,
                    "lastModified": now_iso,
                    "deviceId": "komic",
                    "deviceName": "Komic"
                }
                read_progress_cache[p_key] = progress_dto
                if u == "default":
                    read_progress_cache[book_id] = progress_dto
                try:
                    db.save_read_progress(u, book_id, page, completed, date_finished or now_iso, progress_dto)
                except Exception:
                    pass
                return progress_dto
        except Exception:
            pass

        read_progress_cache[p_key] = None
        if u == "default":
            read_progress_cache[book_id] = None
        return None

    async def record_reading_session(
        self,
        book_id: str,
        user: str,
        pwd: str,
        start_iso: str,
        end_iso: str,
        duration: int,
        start_page: int,
        end_page: int
    ) -> None:
        """Report reading session to Grimmory."""
        native_headers = await self.get_native_headers(user, pwd)
        b_id = int(book_id) if str(book_id).isdigit() else book_id
        session_payload = {
            "bookId": b_id,
            "startTime": start_iso,
            "endTime": end_iso,
            "durationSeconds": duration,
            "startPage": start_page,
            "endPage": end_page,
            "device": "Komic"
        }
        for ep in ["/api/v1/reading-sessions", "/api/v1/app/reading-sessions"]:
            try:
                resp = await self.client.post(ep, json=session_payload, headers=native_headers)
                if resp.status_code in [200, 201, 204]:
                    break
            except Exception:
                pass

    async def update_read_progress(self, book_id: str, page: int, completed: bool, user: str, pwd: str) -> bool:
        """Update read progress in Grimmory and record reading session."""
        native_headers = await self.get_native_headers(user, pwd)
        now_time = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_time))

        total_pages = await self.get_book_page_count(book_id, user, pwd)

        is_completed = completed or (total_pages > 1 and page >= total_pages)
        if is_completed:
            percentage = 100
            if total_pages > 1:
                page = max(page, total_pages)
            date_finished = now_iso
        else:
            percentage = max(1, min(99, round((page / total_pages) * 100))) if total_pages > 1 else (100 if completed else 0)
            date_finished = None

        # Update cache immediately so next read is instant
        progress_dto = {
            "page": page,
            "completed": is_completed,
            "readDate": date_finished or now_iso,
            "created": now_iso,
            "lastModified": now_iso,
            "deviceId": "komic",
            "deviceName": "Komic"
        }
        u = (user or "default").lower().strip()
        p_key = f"{u}:{book_id}"
        read_progress_cache[p_key] = progress_dto
        if u == "default":
            read_progress_cache[book_id] = progress_dto
        try:
            db.save_read_progress(u, book_id, page, is_completed, date_finished or now_iso, progress_dto)
        except Exception:
            pass
        b_key = f"{u}:{book_id}"
        if b_key in book_cache:
            book_cache[b_key]["readProgress"] = progress_dto
        elif book_id in book_cache:
            book_cache[book_id]["readProgress"] = progress_dto

        # Reading session tracking
        session_key = f"{user}:{book_id}"
        prev_session = active_sessions.get(session_key)
        start_page = page
        start_iso = now_iso
        duration = 10 # default minimum session duration

        if prev_session and (now_time - prev_session["last_update"]) < 1800:
            start_page = prev_session["start_page"]
            start_iso = prev_session["start_iso"]
            duration = max(1, int(now_time - prev_session["start_time"]))
            prev_session["last_page"] = page
            prev_session["last_update"] = now_time
        else:
            active_sessions[session_key] = {
                "start_time": now_time,
                "start_iso": now_iso,
                "start_page": page,
                "last_page": page,
                "last_update": now_time
            }

        # 1. Update native Grimmory progress
        payload = {
            "cbxProgress": {
                "page": page,
                "percentage": percentage
            },
            "pdfProgress": {
                "page": page,
                "percentage": percentage
            },
            "epubProgress": {
                "page": page,
                "percentage": percentage
            },
            "dateFinished": date_finished,
            "progressValid": True
        }

        success = False
        try:
            resp = await self.client.put(
                f"/api/v1/app/books/{book_id}/progress",
                json=payload,
                headers=native_headers
            )
            success = resp.status_code in [200, 204]
        except Exception:
            pass

        # 2. Update native Grimmory book read status
        status_val = "READ" if is_completed else "READING"
        try:
            await self.client.put(
                f"/api/v1/app/books/{book_id}/status",
                json={"status": status_val, "readStatus": status_val},
                headers=native_headers
            )
        except Exception:
            pass

        # 3. Record reading session to Grimmory (non-blocking)
        try:
            asyncio.create_task(
                self.record_reading_session(
                    book_id=book_id,
                    user=user,
                    pwd=pwd,
                    start_iso=start_iso,
                    end_iso=now_iso,
                    duration=duration,
                    start_page=start_page,
                    end_page=page
                )
            )
        except Exception:
            pass

        # 4. If completed, clear session
        if is_completed:
            active_sessions.pop(session_key, None)

        return success

    async def reset_read_progress(self, book_id: str, user: str, pwd: str) -> bool:
        """Reset progress in Grimmory."""
        u = (user or "default").lower().strip()
        p_key = f"{u}:{book_id}"
        read_progress_cache.pop(p_key, None)
        if u == "default":
            read_progress_cache.pop(book_id, None)
        try:
            db.delete_read_progress(u, book_id)
        except Exception:
            pass
        active_sessions.pop(f"{user}:{book_id}", None)
        b_key = f"{u}:{book_id}"
        if b_key in book_cache:
            book_cache[b_key]["readProgress"] = None
        elif book_id in book_cache:
            book_cache[book_id]["readProgress"] = None

        native_headers = await self.get_native_headers(user, pwd)
        try:
            await self.client.put(
                f"/api/v1/app/books/{book_id}/status",
                json={"status": "UNREAD", "readStatus": "UNREAD"},
                headers=native_headers
            )
        except Exception:
            pass

        try:
            resp = await self.client.post(
                "/api/v1/books/reset-progress",
                json=[int(book_id)],
                headers=native_headers
            )
            return resp.status_code in [200, 204]
        except Exception:
            return False

    async def get_adjacent_book(
        self,
        book_id: str,
        direction: str,
        user: str,
        pwd: str
    ) -> Optional[Dict[str, Any]]:
        """Find the previous or next book in the series for book_id."""
        current_book = await self.get_book_dto(book_id, user, pwd)
        if not current_book:
            return None

        series_id = current_book.get("seriesId")
        if not series_id or "-standalone-" in series_id:
            return None

        books = db.get_books_by_series(series_id)
        if not books:
            books = await self.get_series_books_custom(series_id, user, pwd)

        if not books:
            return None

        def _sort_key(b):
            meta = b.get("metadata") or {}
            num = meta.get("numberSort")
            if num is None:
                try:
                    num = float(b.get("number", 0))
                except Exception:
                    num = 0.0
            return (float(num), str(b.get("id")))

        books.sort(key=_sort_key)

        curr_idx = next((i for i, b in enumerate(books) if str(b.get("id")) == str(book_id)), None)
        if curr_idx is None:
            return None

        target_idx = curr_idx - 1 if direction == "previous" else curr_idx + 1
        if 0 <= target_idx < len(books):
            target_book = books[target_idx]
            await self.enrich_book(target_book, user, pwd, fetch_dimensions=True)
            return target_book

        return None

    def _is_book_finished(self, book_obj: Dict[str, Any], user: Optional[str] = None, progress: Optional[Dict[str, Any]] = None) -> bool:
        """Strictly determine if a book is completed or finished."""
        if not book_obj or not isinstance(book_obj, dict):
            return False
        b_id = str(book_obj.get("id"))
        u = (user or "default").lower().strip()
        p_key = f"{u}:{b_id}"

        # 1. User-scoped cache and database progress is the highest authority
        user_prog = progress or read_progress_cache.get(p_key) or (read_progress_cache.get(b_id) if u == "default" else None)
        if user_prog is None:
            user_prog = db.get_read_progress(u, b_id)
        if isinstance(user_prog, dict):
            if user_prog.get("completed") is True:
                return True
            page = user_prog.get("page", 0)
            pages_count = (book_obj.get("media") or {}).get("pagesCount", 0) or db.get_book_page_count(b_id) or 0
            if pages_count > 1 and page >= pages_count:
                return True
            return False

        # 2. Check direct readProgress object on book if present
        prog = book_obj.get("readProgress")
        if isinstance(prog, dict):
            if prog.get("completed") is True:
                return True
            page = prog.get("page", 0)
            pages_count = (book_obj.get("media") or {}).get("pagesCount", 0)
            if pages_count > 1 and page >= pages_count:
                return True
            return False

        # 3. Check Grimmory native raw status fields
        read_status = str(book_obj.get("readStatus") or book_obj.get("status") or "").upper().strip()
        if read_status == "READ":
            return True
        if book_obj.get("completed") is True or book_obj.get("isRead") is True:
            return True
        if book_obj.get("dateFinished") is not None and str(book_obj.get("dateFinished")).strip() not in ("", "null", "None"):
            return True

        for p_key_f in ["cbxProgress", "pdfProgress", "epubProgress"]:
            p = book_obj.get(p_key_f)
            if isinstance(p, dict):
                try:
                    pct = float(p.get("percentage", 0))
                    if pct >= 100:
                        return True
                except Exception:
                    pass

        read_prog_val = book_obj.get("readProgress")
        if isinstance(read_prog_val, (int, float)):
            try:
                if float(read_prog_val) >= 1.0 or float(read_prog_val) >= 100.0:
                    return True
            except Exception:
                pass

        koreader = book_obj.get("koreaderProgress")
        if isinstance(koreader, dict):
            try:
                k_pct = float(koreader.get("percentage", 0))
                if k_pct >= 1.0 or k_pct >= 100.0:
                    return True
            except Exception:
                pass

        return False

    def _is_book_in_progress(self, book_obj: Dict[str, Any], user: Optional[str] = None, progress: Optional[Dict[str, Any]] = None) -> bool:
        if not book_obj or not isinstance(book_obj, dict):
            return False
        b_id = str(book_obj.get("id"))
        u = (user or "default").lower().strip()
        p_key = f"{u}:{b_id}"

        user_prog = progress or read_progress_cache.get(p_key) or (read_progress_cache.get(b_id) if u == "default" else None)
        if user_prog is None:
            user_prog = db.get_read_progress(u, b_id)

        if self._is_book_finished(book_obj, user=user, progress=user_prog):
            return False

        read_status = str(book_obj.get("readStatus") or book_obj.get("status") or "").upper().strip()
        if read_status == "READ":
            return False
        if read_status == "UNREAD":
            return False
        if read_status == "READING":
            return True

        # Check user-scoped progress first
        if isinstance(user_prog, dict):
            if user_prog.get("completed") is True:
                return False
            page = user_prog.get("page", 0)
            if page > 1 or (page == 1 and user_prog.get("readDate")):
                return True
            return False

        prog = book_obj.get("readProgress")
        if isinstance(prog, dict):
            if prog.get("completed") is True:
                return False
            page = prog.get("page", 0)
            if page > 1 or (page == 1 and prog.get("readDate")):
                return True

        for p_key_f in ["cbxProgress", "pdfProgress", "epubProgress"]:
            p = book_obj.get(p_key_f)
            if isinstance(p, dict):
                try:
                    pct = float(p.get("percentage", 0))
                    page = p.get("page", 0)
                    if (page > 1 or pct > 0) and pct < 100:
                        return True
                except Exception:
                    pass

        read_prog_val = book_obj.get("readProgress")
        if isinstance(read_prog_val, (int, float)):
            try:
                f_val = float(read_prog_val)
                if 0.0 < f_val < 1.0 or 0 < f_val < 100:
                    return True
            except Exception:
                pass

        koreader = book_obj.get("koreaderProgress")
        if isinstance(koreader, dict):
            try:
                k_pct = float(koreader.get("percentage", 0))
                if 0.0 < k_pct < 1.0 or 0 < k_pct < 100:
                    return True
            except Exception:
                pass

        return False

    def _is_book_unread(self, book_obj: Dict[str, Any], user: Optional[str] = None) -> bool:
        return not self._is_book_finished(book_obj, user=user) and not self._is_book_in_progress(book_obj, user=user)

    async def reconcile_read_progress(self, user: str, pwd: str) -> int:
        """Re-validate all cached in-progress books with Grimmory to heal stale records."""
        try:
            native_headers = await self.get_native_headers(user, pwd)
            u = (user or "default").lower().strip()
            in_prog_rows = db.get_all_in_progress(user=u)
            if not in_prog_rows:
                return 0

            sem = asyncio.Semaphore(8)

            async def _reconcile_one(b_id: str):
                async with sem:
                    try:
                        resp = await self.client.get(f"/api/v1/app/books/{b_id}/progress", headers=native_headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            if not data or not isinstance(data, dict):
                                return

                            read_status = str(data.get("readStatus") or data.get("status") or "").upper().strip()
                            date_fin = data.get("dateFinished") or (data.get("lastReadTime") if read_status == "READ" else None)
                            last_read = data.get("lastReadTime") or date_fin

                            pct = 0
                            if "readProgress" in data and isinstance(data["readProgress"], (int, float)):
                                pct_val = float(data["readProgress"])
                                pct = round(pct_val * 100) if pct_val <= 1.0 else round(pct_val)
                            elif "koreaderProgress" in data and isinstance(data["koreaderProgress"], dict):
                                k_pct = float(data["koreaderProgress"].get("percentage", 0))
                                pct = round(k_pct * 100) if k_pct <= 1.0 else round(k_pct)

                            is_completed = (
                                read_status == "READ" or date_fin or pct >= 100 or
                                data.get("completed") is True or data.get("isRead") is True
                            )

                            count = db.get_book_page_count(b_id) or 1
                            now_iso = last_read or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            p_key = f"{u}:{b_id}"

                            if is_completed:
                                prog_dto = {
                                    "page": count,
                                    "completed": True,
                                    "readDate": now_iso,
                                    "created": now_iso,
                                    "lastModified": now_iso,
                                    "deviceId": "komic",
                                    "deviceName": "Komic"
                                }
                                read_progress_cache[p_key] = prog_dto
                                read_progress_cache[b_id] = prog_dto
                                db.save_read_progress(u, b_id, count, True, now_iso, prog_dto)
                                if u != "default":
                                    db.save_read_progress("default", b_id, count, True, now_iso, prog_dto)
                            elif read_status == "UNREAD" or (pct == 0 and not last_read):
                                read_progress_cache.pop(p_key, None)
                                read_progress_cache.pop(b_id, None)
                                db.delete_read_progress(u, b_id)
                                if u != "default":
                                    db.delete_read_progress("default", b_id)
                            elif read_status == "READING" or pct > 0:
                                page = max(1, round(pct * count / 100)) if pct > 0 else 1
                                prog_dto = {
                                    "page": page,
                                    "completed": False,
                                    "readDate": now_iso,
                                    "created": now_iso,
                                    "lastModified": now_iso,
                                    "deviceId": "komic",
                                    "deviceName": "Komic"
                                }
                                read_progress_cache[p_key] = prog_dto
                                read_progress_cache[b_id] = prog_dto
                                db.save_read_progress(u, b_id, page, False, now_iso, prog_dto)
                                if u != "default":
                                    db.save_read_progress("default", b_id, page, False, now_iso, prog_dto)
                    except Exception:
                        pass

            await asyncio.gather(*[_reconcile_one(b_id) for b_id, _ in in_prog_rows], return_exceptions=True)
            active_now = len(db.get_all_in_progress(user=u))
            logger.info(f"[Reconcile] Read progress reconciled with Grimmory for user '{u}': {active_now} active in-progress books.")
            return active_now
        except Exception as e:
            logger.debug(f"[Reconcile] Error during reconciliation: {e}")
            return len(db.get_all_in_progress(user=(user or 'default').lower().strip()))

    async def get_books_by_read_status(
        self,
        statuses: List[str],
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        series_id: Optional[str] = None,
        library_id: Optional[str] = None,
        sort: str = ""
    ) -> Dict[str, Any]:
        """Fetch books filtered by readStatus (e.g. READ, UNREAD, IN_PROGRESS)."""
        norm_statuses = {str(s).upper().strip() for s in statuses if str(s).strip()}
        if not norm_statuses:
            norm_statuses = {"UNREAD"}

        # Fast path: If only IN_PROGRESS is asked for and no series_id is specified
        if norm_statuses == {"IN_PROGRESS"} and not series_id:
            return await self.get_continue_reading_books(user, pwd, page=page, size=size, library_id=library_id)

        all_books = []
        if series_id:
            if "-standalone-" in series_id:
                b_id = series_id.split("-standalone-")[-1]
                b = await self.get_book_dto(b_id, user, pwd)
                all_books = [ensure_book_dto(b)] if b else []
            elif "-u-" in series_id:
                all_books = await self.get_series_books_custom(series_id, user, pwd)
            else:
                all_books = db.get_books_by_series(series_id)
                if not all_books:
                    all_books = await self.get_series_books_custom(series_id, user, pwd)
        else:
            all_books = db.get_all_books(library_id=library_id)
            if not all_books:
                all_books = await self.fetch_and_cache_native_books(user, pwd, library_id=library_id)

        # Verify user library permissions
        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            all_books = [b for b in all_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]

        # Filter books by status
        matched_books = []
        u = (user or "default").lower().strip()
        user_prog_map = db.get_all_read_progress_map(u)
        default_prog_map = db.get_all_read_progress_map("default") if u != "default" else {}
        for b in all_books:
            b_id = str(b.get("id"))
            if library_id:
                b_lib = str(b.get("libraryId") or b.get("library_id", ""))
                if b_lib and b_lib != str(library_id):
                    continue
            b_copy = dict(b)
            p_key = f"{u}:{b_id}"
            u_prog = read_progress_cache.get(p_key) or user_prog_map.get(b_id) or (read_progress_cache.get(b_id) if u == "default" else None) or default_prog_map.get(b_id)
            b_copy["readProgress"] = u_prog

            is_fin = self._is_book_finished(b_copy, user=user, progress=u_prog)
            is_inp = self._is_book_in_progress(b_copy, user=user, progress=u_prog)
            is_unr = (not is_fin) and (not is_inp)

            matched = False
            if "READ" in norm_statuses and is_fin:
                matched = True
            elif "IN_PROGRESS" in norm_statuses and is_inp:
                matched = True
            elif "UNREAD" in norm_statuses and is_unr:
                matched = True

            if matched:
                matched_books.append(b_copy)

        sort_lower = sort.lower()
        if "readdate" in sort_lower or "readprogress" in sort_lower:
            matched_books.sort(
                key=lambda x: (x.get("readProgress") or {}).get("readDate") or "",
                reverse=True
            )
        elif "releasedate" in sort_lower:
            matched_books.sort(
                key=lambda x: (x.get("metadata") or {}).get("releaseDate") or "",
                reverse=True
            )
        elif any(k in sort_lower for k in ["created", "added", "lastmodified"]):
            matched_books.sort(
                key=lambda x: x.get("created") or x.get("lastModified") or "",
                reverse=True
            )
        else:
            try:
                matched_books.sort(
                    key=lambda x: float((x.get("metadata") or {}).get("numberSort", x.get("number", 1.0)))
                )
            except Exception:
                pass

        total = len(matched_books)
        start = page * size
        paged_content = matched_books[start:start + size]

        await self.enrich_books_page_count(paged_content, user, pwd)
        for b in paged_content:
            ensure_book_dto(b)

        return ensure_page_dto({
            "content": paged_content,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def get_continue_reading_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch books currently being read (Weiterlesen / Keep Reading).
        Strictly returns books that are IN_PROGRESS. Never returns finished/read books or unread books.
        """
        if not self._has_reconciled_progress:
            self._has_reconciled_progress = True
            asyncio.create_task(self.reconcile_read_progress(user, pwd))

        u = (user or "default").lower().strip()
        raw_books = []
        seen_ids = set()

        # 1. Grimmory continue-reading
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/continue-reading", params={"size": 100}, headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("content", []) if isinstance(data, dict) else []
                for b in items:
                    b_id = str(b.get("id"))
                    if b_id and b_id not in seen_ids and not self._is_book_finished(b, user=user):
                        dto = raw_app_book_to_dto(b)
                        if not self._is_book_finished(dto, user=user):
                            raw_books.append(dto)
                            seen_ids.add(b_id)
        except Exception:
            pass

        # 2. In-progress from SQLite DB & in-memory cache
        try:
            db_in_progress = db.get_all_in_progress(user=u)
            for b_id, prog in db_in_progress:
                if str(b_id) not in seen_ids and not prog.get("completed"):
                    cached_b = await self.get_book_dto(str(b_id), user, pwd)
                    if cached_b and not self._is_book_finished(cached_b, user=user):
                        raw_books.append(cached_b)
                        seen_ids.add(str(b_id))
        except Exception:
            pass

        prefix = f"{u}:"
        for key, prog in list(read_progress_cache.items()):
            if not prog or prog.get("completed") or prog.get("page", 0) <= 0:
                continue
            if key.startswith(prefix):
                b_id = key[len(prefix):]
            elif ":" not in key and f"{u}:{key}" not in read_progress_cache:
                b_id = key
            else:
                continue

            if b_id not in seen_ids:
                cached_b = await self.get_book_dto(b_id, user, pwd)
                if cached_b and not self._is_book_finished(cached_b, user=user):
                    raw_books.append(cached_b)
                    seen_ids.add(b_id)

        # 3. Filter by library permissions
        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            raw_books = [b for b in raw_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]

        if library_id:
            raw_books = [b for b in raw_books if str(b.get("libraryId") or b.get("library_id")) == str(library_id)]

        # 4. Strict filter: NEVER finished
        final_books = []
        for b in raw_books:
            if not self._is_book_finished(b, user=user):
                final_books.append(b)

        # Sort by most recent reading activity
        final_books.sort(
            key=lambda x: (x.get("readProgress") or {}).get("readDate") or x.get("lastModified") or "",
            reverse=True
        )

        total = len(final_books)
        start = page * size
        page_items = final_books[start:start + size]
        await self.enrich_books_page_count(page_items, user, pwd)
        for b in page_items:
            ensure_book_dto(b, user=user)

        return ensure_page_dto({
            "content": page_items,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def get_ondeck_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch On Deck books (Als nächstes lesen).
        Returns the current in-progress book or the next unread book for each active series.
        Strictly excludes any 100% or finished books.
        """
        if not self._has_reconciled_progress:
            self._has_reconciled_progress = True
            asyncio.create_task(self.reconcile_read_progress(user, pwd))

        u = (user or "default").lower().strip()
        user_libs = await self.get_user_library_ids(user, pwd)
        active_series_ids = db.get_active_series_ids(user=u)
        u_map = db.get_all_read_progress_map(u)
        default_map = db.get_all_read_progress_map("default") if u != "default" else {}

        ondeck_books = []
        seen_book_ids = set()

        # For each active series, determine the on-deck book
        for s_id in active_series_ids:
            s_books = db.get_books_by_series(s_id)
            if not s_books:
                continue

            # Check library access
            if user_libs is not None:
                first_b = s_books[0]
                if str(first_b.get("libraryId") or first_b.get("library_id", "")) not in user_libs:
                    continue
            if library_id:
                first_b = s_books[0]
                if str(first_b.get("libraryId") or first_b.get("library_id")) != str(library_id):
                    continue

            # Find active in-progress book or next unread in series
            in_prog_book = None
            highest_read_idx = -1
            latest_read_date = ""

            for idx, b in enumerate(s_books):
                b_id = str(b.get("id"))
                b_copy = dict(b)
                p_key = f"{u}:{b_id}"
                prog = read_progress_cache.get(p_key) or u_map.get(b_id) or (read_progress_cache.get(b_id) if u == "default" else None) or default_map.get(b_id)
                if prog:
                    b_copy["readProgress"] = prog

                if self._is_book_finished(b_copy, user=user, progress=prog):
                    highest_read_idx = max(highest_read_idx, idx)
                    r_date = (b_copy.get("readProgress") or {}).get("readDate") or ""
                    if r_date > latest_read_date:
                        latest_read_date = r_date
                elif self._is_book_in_progress(b_copy, user=user, progress=prog):
                    if in_prog_book is None:
                        in_prog_book = b_copy
                        r_date = (b_copy.get("readProgress") or {}).get("readDate") or ""
                        if r_date > latest_read_date:
                            latest_read_date = r_date

            target_book = None
            if in_prog_book and not self._is_book_finished(in_prog_book, user=user):
                target_book = in_prog_book
            elif highest_read_idx >= 0 and highest_read_idx + 1 < len(s_books):
                candidate = dict(s_books[highest_read_idx + 1])
                c_id = str(candidate.get("id"))
                c_prog = read_progress_cache.get(f"{u}:{c_id}") or u_map.get(c_id) or (read_progress_cache.get(c_id) if u == "default" else None) or default_map.get(c_id)
                if c_prog:
                    candidate["readProgress"] = c_prog
                if not self._is_book_finished(candidate, user=user, progress=c_prog):
                    target_book = candidate

            if target_book and str(target_book.get("id")) not in seen_book_ids:
                if not self._is_book_finished(target_book, user=user):
                    target_book["_latestReadDate"] = latest_read_date
                    ondeck_books.append(target_book)
                    seen_book_ids.add(str(target_book.get("id")))

        # Merge active in-progress books from Grimmory continue-reading if not yet included
        try:
            native_headers = await self.get_native_headers(user, pwd)
            resp = await self.client.get("/api/v1/app/books/continue-reading", params={"size": 100}, headers=native_headers, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("content", []) if isinstance(data, dict) else []
                for item in items:
                    b_id = str(item.get("id"))
                    if b_id and b_id not in seen_book_ids and not self._is_book_finished(item, user=user):
                        dto = raw_app_book_to_dto(item)
                        if not self._is_book_finished(dto, user=user):
                            if user_libs is None or str(dto.get("libraryId") or dto.get("library_id", "")) in user_libs:
                                if not library_id or str(dto.get("libraryId") or dto.get("library_id")) == str(library_id):
                                    ondeck_books.append(dto)
                                    seen_book_ids.add(b_id)
        except Exception:
            pass

        # Also merge active in-progress books from DB and cache
        try:
            db_in_progress = db.get_all_in_progress(user=u)
            for b_id, prog in db_in_progress:
                if str(b_id) not in seen_book_ids and not prog.get("completed"):
                    cached_b = await self.get_book_dto(str(b_id), user, pwd)
                    if cached_b and not self._is_book_finished(cached_b, user=user):
                        if user_libs is None or str(cached_b.get("libraryId") or cached_b.get("library_id", "")) in user_libs:
                            if not library_id or str(cached_b.get("libraryId") or cached_b.get("library_id")) == str(library_id):
                                ondeck_books.append(cached_b)
                                seen_book_ids.add(str(b_id))
        except Exception:
            pass

        prefix = f"{u}:"
        for key, prog in list(read_progress_cache.items()):
            if not prog or prog.get("completed") or prog.get("page", 0) <= 0:
                continue
            if key.startswith(prefix):
                b_id = key[len(prefix):]
            elif ":" not in key and f"{u}:{key}" not in read_progress_cache:
                b_id = key
            else:
                continue

            if b_id not in seen_book_ids:
                cached_b = await self.get_book_dto(b_id, user, pwd)
                if cached_b and not self._is_book_finished(cached_b, user=user):
                    if user_libs is None or str(cached_b.get("libraryId") or cached_b.get("library_id", "")) in user_libs:
                        if not library_id or str(cached_b.get("libraryId") or cached_b.get("library_id")) == str(library_id):
                            ondeck_books.append(cached_b)
                            seen_book_ids.add(b_id)

        # Sort on deck by most recently active series
        ondeck_books.sort(
            key=lambda x: x.get("_latestReadDate") or (x.get("readProgress") or {}).get("readDate") or x.get("lastModified") or "",
            reverse=True
        )

        for b in ondeck_books:
            b.pop("_latestReadDate", None)

        total = len(ondeck_books)
        start = page * size
        page_items = ondeck_books[start:start + size]
        await self.enrich_books_page_count(page_items, user, pwd)
        for b in page_items:
            ensure_book_dto(b, user=user)

        return ensure_page_dto({
            "content": page_items,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def get_released_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch recently released books.
        Strict requirement: ONLY return books that have a non-empty releaseDate/publishedDate in Grimmory!
        """
        candidate_books = []
        seen_ids = set()

        # 1. Fetch cached books with release date from SQLite DB
        db_candidates = db.get_books_with_release_date(library_id=library_id)
        for b in db_candidates:
            b_id = str(b.get("id"))
            if b_id and b_id not in seen_ids:
                candidate_books.append(b)
                seen_ids.add(b_id)

        # 2. Query Grimmory native recently added books only if DB has no release dates yet
        if not candidate_books:
            native_headers = await self.get_native_headers(user, pwd)
            try:
                resp = await self.client.get("/api/v1/app/books/recently-added", params={"size": 500}, headers=native_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_list = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                    for item in raw_list:
                        b_id = str(item.get("id"))
                        if b_id and b_id not in seen_ids:
                            dto = raw_app_book_to_dto(item)
                            rd = dto.get("metadata", {}).get("releaseDate") or item.get("publishedDate") or item.get("releaseDate")
                            if rd and str(rd).strip() not in ("", "null", "None"):
                                dto["metadata"]["releaseDate"] = str(rd).strip()
                                candidate_books.append(dto)
                                seen_ids.add(b_id)
            except Exception:
                pass

        # 3. If needed, query Grimmory native endpoint
        if not candidate_books:
            try:
                native_headers = await self.get_native_headers(user, pwd)
                resp = await self.client.get("/api/v1/books", headers=native_headers)
                if resp.status_code == 200:
                    raw_data = resp.json()
                    k_items = raw_data if isinstance(raw_data, list) else raw_data.get("content", []) if isinstance(raw_data, dict) else []
                    for raw_b in k_items:
                        b = raw_app_book_to_dto(raw_b)
                        b_id = str(b.get("id"))
                        rd = b.get("metadata", {}).get("releaseDate")
                        if rd and str(rd).strip() not in ("", "null", "None") and b_id not in seen_ids:
                            candidate_books.append(b)
                            seen_ids.add(b_id)
            except Exception:
                pass

        # 4. Strict filter: MUST have non-empty releaseDate and user access
        user_libs = await self.get_user_library_ids(user, pwd)
        valid_released_books = []
        for b in candidate_books:
            if user_libs is not None and str(b.get("libraryId") or b.get("library_id", "")) in user_libs:
                pass
            elif user_libs is not None:
                continue
            if library_id and str(b.get("libraryId") or b.get("library_id")) != str(library_id):
                continue
            rd = b.get("metadata", {}).get("releaseDate")
            if rd and str(rd).strip() not in ("", "null", "None"):
                valid_released_books.append(b)

        # 5. Sort by releaseDate DESC
        def _release_key(b):
            return str(b.get("metadata", {}).get("releaseDate") or "")

        valid_released_books.sort(key=_release_key, reverse=True)

        total = len(valid_released_books)
        start = page * size
        page_items = valid_released_books[start:start + size]

        await self.enrich_books_page_count(page_items, user, pwd)
        for b in page_items:
            ensure_book_dto(b)

        return ensure_page_dto({
            "content": page_items,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def get_latest_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch recently added books."""
        # 1. Try SQLite persistent cache first
        db_books = db.get_latest_books(library_id=library_id, limit=200)
        if db_books:
            user_libs = await self.get_user_library_ids(user, pwd)
            if user_libs is not None:
                db_books = [b for b in db_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]
            total = len(db_books)
            start = page * size
            page_items = db_books[start:start + size]
            await self.enrich_books_page_count(page_items, user, pwd)
            for b in page_items:
                ensure_book_dto(b)
            return ensure_page_dto({
                "content": page_items,
                "totalElements": total,
                "number": page,
                "size": size
            }, default_page=page, default_size=size)

        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/recently-added", params={"size": 500}, headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_books = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                if raw_books:
                    user_libs = await self.get_user_library_ids(user, pwd)
                    if user_libs is not None:
                        raw_books = [
                            b for b in raw_books
                            if str(b.get("libraryId") or b.get("library_id", "")) in user_libs
                        ]
                    if library_id:
                        raw_books = [
                            b for b in raw_books
                            if str(b.get("libraryId") or b.get("library_id")) == str(library_id)
                        ]
                    total = len(raw_books)
                    start = page * size
                    page_items = raw_books[start:start + size]
                    paged_content = [raw_app_book_to_dto(item) for item in page_items if item.get("id")]
                    await self.enrich_books_page_count(paged_content, user, pwd)
                    for b in paged_content:
                        ensure_book_dto(b)
                    return ensure_page_dto({
                        "content": paged_content,
                        "totalElements": total,
                        "number": page,
                        "size": size
                    }, default_page=page, default_size=size)
        except Exception:
            pass

        # Fallback to native /api/v1/books
        all_b = await self.fetch_and_cache_native_books(user, pwd, library_id=library_id)
        if all_b:
            start = page * size
            paged = all_b[start:start + size]
            return ensure_page_dto({
                "content": paged,
                "totalElements": len(all_b),
                "number": page,
                "size": size
            }, default_page=page, default_size=size)

        return ensure_page_dto({"content": []}, default_page=page, default_size=size)

    async def get_updated_series(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch recently updated series based on chronological book activity."""
        # 1. Check SQLite DB for series first
        all_series = db.get_all_series(library_id=library_id)
        if not all_series:
            all_series = await self.get_all_series(user, pwd, library_id=library_id)

        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            all_series = [s for s in all_series if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]

        recent_series_names = []
        recent_books = db.get_latest_books(library_id=library_id, limit=200)
        for b in recent_books:
            s_name = b.get("seriesName") or (b.get("metadata") or {}).get("series") or b.get("seriesTitle")
            if s_name and s_name not in recent_series_names:
                recent_series_names.append(s_name)

        if not recent_series_names:
            native_headers = await self.get_native_headers(user, pwd)
            try:
                resp = await self.get_client().get("/api/v1/app/books/recently-added", params={"size": 100}, headers=native_headers, timeout=5.0)
                if resp.status_code == 200:
                    raw = resp.json()
                    raw_books = raw.get("content", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
                    for b in raw_books:
                        if library_id and str(b.get("libraryId") or b.get("library_id")) != str(library_id):
                            continue
                        s_name = b.get("seriesName") or b.get("seriesTitle")
                        if s_name and s_name not in recent_series_names:
                            recent_series_names.append(s_name)
            except Exception:
                pass

        # Rank series by chronological appearance in recently-added books
        name_to_rank = {name.lower(): rank for rank, name in enumerate(recent_series_names)}

        def series_sort_key(s):
            name = (s.get("name") or s.get("metadata", {}).get("title") or "").lower()
            return name_to_rank.get(name, 999999)

        all_series.sort(key=series_sort_key)

        total = len(all_series)
        start = page * size
        paged_content = all_series[start:start + size]
        u = (user or "default").lower().strip()
        u_map = db.get_all_read_progress_map(u)
        for s in paged_content:
            disambiguate_series_dto(s)
            ensure_series_dto(s, user=user, progress_map=u_map)

        return ensure_page_dto({
            "content": paged_content,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def get_all_series(
        self,
        user: str,
        pwd: str,
        library_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch all series (from SQLite DB or native Grimmory books) with caching."""
        cache_key = f"{user}:{library_id or 'all'}"
        if cache_key in self.all_series_cache:
            return list(self.all_series_cache[cache_key])

        cached = db.get_all_series(library_id=library_id)
        if not cached:
            await self.fetch_and_cache_native_books(user, pwd, library_id=library_id)
            cached = db.get_all_series(library_id=library_id)

        all_series = list(cached) if cached else []

        # Also merge custom_series that match the library_id
        for s_id, custom_info in self.custom_series.items():
            if not library_id or str(custom_info.get("lib_id")) == str(library_id):
                s_dto = custom_info.get("dto", {})
                if s_dto and not any(existing.get("id") == s_id for existing in all_series):
                    all_series.append(s_dto)

        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            all_series = [s for s in all_series if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]

        self.all_series_cache[cache_key] = all_series
        return all_series

    async def search_series(
        self,
        query: str,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[Union[str, List[str], Set[str]]] = None
    ) -> Dict[str, Any]:
        """Search series by title/name."""
        q_clean = query.strip()
        matching = db.search_series(q_clean, library_id=library_id)

        # Also search custom series that match
        q_lower = q_clean.lower()
        for s_id, custom_info in self.custom_series.items():
            if not library_id or str(custom_info.get("lib_id")) == str(library_id):
                s_name = (custom_info.get("name") or "").lower()
                if q_lower in s_name and not any(existing.get("id") == s_id for existing in matching):
                    s_dto = custom_info.get("dto", {})
                    if s_dto:
                        matching.append(s_dto)

        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            matching = [s for s in matching if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]

        # If DB had no matches, only then try upstream Grimmory series
        if not matching:
            all_series = await self.get_all_series(user, pwd, library_id=library_id)
            matching = [
                s for s in all_series
                if q_lower in (s.get("name") or "").lower() or q_lower in (s.get("metadata", {}).get("title") or "").lower()
            ]
            if user_libs is not None:
                matching = [s for s in matching if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]

        total = len(matching)
        start = page * size
        paged = matching[start:start + size]
        u = (user or "default").lower().strip()
        u_map = db.get_all_read_progress_map(u)
        for s in paged:
            disambiguate_series_dto(s)
            ensure_series_dto(s, user=user, progress_map=u_map)

        return ensure_page_dto({
            "content": paged,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    async def search_books(
        self,
        query: str,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[Union[str, List[str], Set[str]]] = None
    ) -> Dict[str, Any]:
        """Search books by title/metadata via persistent DB first, then native Grimmory search."""
        q_clean = query.strip()
        matching_books = []

        # 1. Search persistent SQLite database first for instant snappy results
        try:
            db_matched = db.search_books(q_clean, library_id)
            if db_matched:
                matching_books.extend(db_matched)
        except Exception:
            pass

        # 2. If nothing found in DB, try Grimmory native search with strict timeout
        if not matching_books:
            native_headers = await self.get_native_headers(user, pwd)
            for param_key in ["query", "search", "q"]:
                try:
                    resp = await self.client.get(
                        "/api/v1/app/books/search",
                        params={param_key: q_clean, "size": 100},
                        headers=native_headers,
                        timeout=3.0
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        raw = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                        if raw:
                            for b in raw:
                                if library_id and str(b.get("libraryId") or b.get("library_id")) != str(library_id):
                                    continue
                                matching_books.append(raw_app_book_to_dto(b))
                            break
                except Exception:
                    pass

        # Verify user library permissions
        user_libs = await self.get_user_library_ids(user, pwd)
        if user_libs is not None:
            matching_books = [
                b for b in matching_books
                if str(b.get("libraryId") or b.get("library_id", "")) in user_libs
            ]

        total = len(matching_books)
        start = page * size
        paged = matching_books[start:start + size]
        await self.enrich_books_page_count(paged, user, pwd)
        for b in paged:
            ensure_book_dto(b, user=user)

        return ensure_page_dto({
            "content": paged,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    def register_custom_series(self, unique_id: str, lib_id: str, name: str, dto: Dict[str, Any]) -> None:
        """Register a disambiguated series mapping."""
        self.custom_series[unique_id] = {
            "lib_id": str(lib_id),
            "name": name,
            "dto": dto
        }

    async def ensure_custom_series_loaded(self, unique_id: str, user: str, pwd: str) -> None:
        """Populate custom_series cache for a library if missing."""
        if unique_id in self.custom_series:
            return
        db_s = db.get_series(unique_id)
        if db_s:
            s_name = db_s.get("name") or db_s.get("metadata", {}).get("title") or ""
            lib_id = str(db_s.get("libraryId", "1"))
            self.register_custom_series(unique_id, lib_id, s_name, db_s)
            return
        if "-u-" not in unique_id:
            return
        lib_id = unique_id.split("-u-")[0]
        try:
            await self.get_all_series(user, pwd, library_id=lib_id)
        except Exception:
            pass

    async def get_series_books_custom(
        self,
        unique_id: str,
        user: str,
        pwd: str
    ) -> List[Dict[str, Any]]:
        """Fetch books for a disambiguated non-ASCII series."""
        if unique_id in self.custom_series_books_cache:
            return self.custom_series_books_cache[unique_id]

        # Check persistent database
        db_books = db.get_books_by_series(unique_id)
        if db_books:
            self.custom_series_books_cache[unique_id] = db_books
            return db_books

        if unique_id not in self.custom_series:
            db_s = db.get_series(unique_id)
            if db_s:
                s_name = db_s.get("name") or db_s.get("metadata", {}).get("title") or ""
                lib_id = str(db_s.get("libraryId", "1"))
                self.register_custom_series(unique_id, lib_id, s_name, db_s)
            else:
                await self.ensure_custom_series_loaded(unique_id, user, pwd)

        info = self.custom_series.get(unique_id)
        if not info:
            return []

        lib_id = info["lib_id"]
        s_name = info["name"]
        native_headers = await self.get_native_headers(user, pwd)

        # 1. Try native Grimmory /api/v1/app/series/{name}/books (fetching all pages)
        enc = urllib.parse.quote(s_name, safe="")
        try:
            page_idx = 0
            all_content = []
            while True:
                resp = await self.client.get(
                    f"/api/v1/app/series/{enc}/books",
                    params={"size": 500, "page": page_idx},
                    headers=native_headers
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                if isinstance(data, list):
                    all_content.extend(data)
                    break
                elif isinstance(data, dict):
                    page_books = data.get("content", [])
                    all_content.extend(page_books)
                    total_pages = data.get("totalPages", 1)
                    page_idx += 1
                    if page_idx >= total_pages or not page_books:
                        break
                else:
                    break

            if all_content:
                filtered = [b for b in all_content if str(b.get("libraryId", lib_id)) == str(lib_id)]
                chosen = filtered if filtered else all_content
                dtos = [raw_app_book_to_dto(b, series_id_override=unique_id) for b in chosen if b.get("id")]
                dtos.sort(key=lambda x: x.get("metadata", {}).get("numberSort", 1.0))
                self.custom_series_books_cache[unique_id] = dtos
                try:
                    db.save_books_batch(dtos)
                except Exception:
                    pass
                if unique_id in self.custom_series:
                    self.custom_series[unique_id]["dto"]["booksCount"] = len(dtos)
                    self.custom_series[unique_id]["dto"].setdefault("metadata", {})["totalBookCount"] = len(dtos)
                    try:
                        db.save_series(self.custom_series[unique_id]["dto"])
                    except Exception:
                        pass
                return dtos
        except Exception:
            pass

        # 2. Try search: /api/v1/app/books/search?q={name}
        try:
            resp = await self.client.get(f"/api/v1/app/books/search?q={enc}&size=500", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                matched = [b for b in content if (b.get("title") == s_name or b.get("seriesName") == s_name) and str(b.get("libraryId", lib_id)) == str(lib_id)]
                if matched:
                    dtos = [raw_app_book_to_dto(b, series_id_override=unique_id) for b in matched if b.get("id")]
                    dtos.sort(key=lambda x: x.get("metadata", {}).get("numberSort", 1.0))
                    self.custom_series_books_cache[unique_id] = dtos
                    try:
                        db.save_books_batch(dtos)
                    except Exception:
                        pass
                    if unique_id in self.custom_series:
                        self.custom_series[unique_id]["dto"]["booksCount"] = len(dtos)
                        self.custom_series[unique_id]["dto"].setdefault("metadata", {})["totalBookCount"] = len(dtos)
                        try:
                            db.save_series(self.custom_series[unique_id]["dto"])
                        except Exception:
                            pass
                    return dtos
        except Exception:
            pass

        # 3. If no books under series, search books in library where title or seriesName matches
        try:
            resp = await self.client.get(f"/api/v1/app/books?libraryId={lib_id}&size=500", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                all_books = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                matched = [b for b in all_books if b.get("title") == s_name or b.get("seriesName") == s_name]
                if matched:
                    dtos = [raw_app_book_to_dto(b, series_id_override=unique_id) for b in matched if b.get("id")]
                    dtos.sort(key=lambda x: x.get("metadata", {}).get("numberSort", 1.0))
                    self.custom_series_books_cache[unique_id] = dtos
                    try:
                        db.save_books_batch(dtos)
                    except Exception:
                        pass
                    if unique_id in self.custom_series:
                        self.custom_series[unique_id]["dto"]["booksCount"] = len(dtos)
                        self.custom_series[unique_id]["dto"].setdefault("metadata", {})["totalBookCount"] = len(dtos)
                        try:
                            db.save_series(self.custom_series[unique_id]["dto"])
                        except Exception:
                            pass
                    return dtos
        except Exception:
            pass

        # 4. Fallback: filter Grimmory native books by seriesTitle == s_name
        try:
            for path in ["/api/v1/books", f"/api/v1/app/books?libraryId={lib_id}"]:
                resp = await self.client.get(path, headers=native_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    all_raw = data if isinstance(data, list) else data.get("content", []) if isinstance(data, dict) else []
                    matched = [b for b in all_raw if b.get("seriesName") == s_name or b.get("title") == s_name]
                    if matched:
                        dtos = [raw_app_book_to_dto(b, series_id_override=unique_id) for b in matched if b.get("id")]
                        dtos.sort(key=lambda x: x.get("metadata", {}).get("numberSort", 1.0))
                        self.custom_series_books_cache[unique_id] = dtos
                        try:
                            db.save_books_batch(dtos)
                        except Exception:
                            pass
                        if unique_id in self.custom_series:
                            self.custom_series[unique_id]["dto"]["booksCount"] = len(dtos)
                            self.custom_series[unique_id]["dto"].setdefault("metadata", {})["totalBookCount"] = len(dtos)
                            try:
                                db.save_series(self.custom_series[unique_id]["dto"])
                            except Exception:
                                pass
                        return dtos
        except Exception:
            pass

        return []

grimmory_client = GrimmoryClient()

