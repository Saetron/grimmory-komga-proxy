import base64
import time
import httpx
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple
from cachetools import TTLCache
from app.config import settings
from app.dto_utils import ensure_page_dto, ensure_book_dto, raw_app_book_to_dto
import asyncio

# In-memory caches:
# Cache JWT tokens for native Grimmory API (1 hour TTL)
token_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
# Cache page info per book (1 hour TTL)
page_cache: TTLCache = TTLCache(maxsize=5000, ttl=3600)
# Cache book DTOs (5 min TTL)
book_cache: TTLCache = TTLCache(maxsize=5000, ttl=300)


class GrimmoryClient:
    def __init__(self):
        self.base_url = settings.GRIMMORY_URL
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop = None
        self.custom_series: Dict[str, Dict[str, Any]] = {}

    def get_client(self) -> httpx.AsyncClient:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._client is None or self._client.is_closed or self._client_loop != loop:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0, connect=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
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
                    return user, pwd
            except Exception:
                pass
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

    async def komga_request(
        self,
        method: str,
        path: str,
        user: str,
        pwd: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> httpx.Response:
        """Forward request to Grimmory's /komga base path."""
        target_path = f"/komga{path}" if not path.startswith("/komga") else path
        req_headers = self.get_basic_auth_header(user, pwd)
        if headers:
            req_headers.update(headers)
        return await self.client.request(
            method,
            target_path,
            params=params,
            json=json_data,
            headers=req_headers
        )

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

    async def get_book_pages_metadata(self, book_id: str, user: str, pwd: str) -> List[Dict[str, Any]]:
        """
        Fetch page metadata for a book.
        Grimmory's /komga/api/v1/books/{id}/pages returns [], so we construct
        accurate PageDto objects from native cbx page dimensions & info.
        """
        if book_id in page_cache:
            return page_cache[book_id]

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
            # 5. Try Grimmory Komga layer: /komga/api/v1/books/{book_id}/pages
            try:
                komga_resp = await self.komga_request("GET", f"/api/v1/books/{book_id}/pages", user, pwd)
                if komga_resp.status_code == 200:
                    k_data = komga_resp.json()
                    if isinstance(k_data, list) and len(k_data) > 0:
                        for idx, p in enumerate(k_data, start=1):
                            num = p.get("number") or idx
                            pages.append({
                                "number": num,
                                "fileName": p.get("fileName") or f"{num:03d}.jpg",
                                "mediaType": p.get("mediaType", "image/jpeg"),
                                "width": p.get("width", 1080),
                                "height": p.get("height", 1920),
                                "sizeBytes": 0,
                                "size": "0 B"
                            })
            except Exception:
                pass

        if not pages:
            # Fallback 1 page
            pages = [{
                "number": 1,
                "fileName": "001.jpg",
                "mediaType": "image/jpeg",
                "width": 1080,
                "height": 1920,
                "sizeBytes": 0,
                "size": "0 B"
            }]

        page_cache[book_id] = pages
        return pages

    async def get_book_dto(self, book_id: str, user: str, pwd: str) -> Optional[Dict[str, Any]]:
        """Fetch book DTO from Grimmory's Komga layer and enrich it."""
        if book_id in book_cache:
            return book_cache[book_id]
        resp = await self.komga_request("GET", f"/api/v1/books/{book_id}", user, pwd)
        if resp.status_code != 200:
            return None
        book = resp.json()
        await self.enrich_book(book, user, pwd, fetch_dimensions=True)
        book_cache[book_id] = book
        return book

    async def enrich_book(self, book: Dict[str, Any], user: str, pwd: str, fetch_dimensions: bool = False) -> None:
        """Ensure all required BookDto fields are present and optionally attach dimensions & progress."""
        book_id = str(book.get("id"))
        if fetch_dimensions or book_id in page_cache:
            pages = await self.get_book_pages_metadata(book_id, user, pwd)
            if pages:
                if "media" not in book or not isinstance(book["media"], dict):
                    book["media"] = {"status": "READY", "mediaType": "application/x-cbz", "mediaProfile": "DIVINA"}
                book["media"]["pagesCount"] = len(pages)

        # Check and attach readProgress if fetched or in cache
        if "readProgress" not in book and (fetch_dimensions or book_id in book_cache):
            progress = await self.get_read_progress(book_id, user, pwd)
            if progress:
                book["readProgress"] = progress

        ensure_book_dto(book)

    async def get_read_progress(self, book_id: str, user: str, pwd: str) -> Optional[Dict[str, Any]]:
        """Fetch read progress from Grimmory native API and format as Komga ReadProgressDto."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get(f"/api/v1/app/books/{book_id}/progress", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    return None
                
                page = 1
                completed = False
                if "cbxProgress" in data and data["cbxProgress"]:
                    page = data["cbxProgress"].get("page", 1)
                elif "pdfProgress" in data and data["pdfProgress"]:
                    page = data["pdfProgress"].get("page", 1)

                date_finished = data.get("dateFinished")
                if date_finished:
                    completed = True

                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return {
                    "page": page,
                    "completed": completed,
                    "readDate": date_finished or now_iso,
                    "created": now_iso,
                    "lastModified": now_iso,
                    "deviceId": "komic",
                    "deviceName": "Komic"
                }
        except Exception:
            pass
        return None

    async def update_read_progress(self, book_id: str, page: int, completed: bool, user: str, pwd: str) -> bool:
        """Update read progress in Grimmory."""
        native_headers = await self.get_native_headers(user, pwd)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {
            "cbxProgress": {
                "page": page,
                "percentage": 100 if completed else 0
            },
            "pdfProgress": {
                "page": page,
                "percentage": 100 if completed else 0
            },
            "dateFinished": now_iso if completed else None,
            "progressValid": True
        }
        try:
            resp = await self.client.put(
                f"/api/v1/app/books/{book_id}/progress",
                json=payload,
                headers=native_headers
            )
            return resp.status_code in [200, 204]
        except Exception:
            return False

    async def reset_read_progress(self, book_id: str, user: str, pwd: str) -> bool:
        """Reset progress in Grimmory."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.post(
                "/api/v1/books/reset-progress",
                json=[int(book_id)],
                headers=native_headers
            )
            return resp.status_code in [200, 204]
        except Exception:
            return False

    async def get_ondeck_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch books currently reading (on deck)."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/continue-reading", headers=native_headers)
            if resp.status_code == 200:
                raw_books = resp.json()
                if isinstance(raw_books, list):
                    if library_id:
                        raw_books = [
                            b for b in raw_books
                            if str(b.get("libraryId") or b.get("library_id")) == str(library_id)
                        ]
                    total = len(raw_books)
                    start = page * size
                    page_items = raw_books[start:start + size]
                    paged_content = [raw_app_book_to_dto(item) for item in page_items if item.get("id")]
                    return ensure_page_dto({
                        "content": paged_content,
                        "totalElements": total,
                        "number": page,
                        "size": size
                    }, default_page=page, default_size=size)
        except Exception:
            pass

        return ensure_page_dto({"content": []}, default_page=page, default_size=size)

    async def get_latest_books(
        self,
        user: str,
        pwd: str,
        page: int = 0,
        size: int = 20,
        library_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch recently added books."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/recently-added", headers=native_headers)
            if resp.status_code == 200:
                raw_books = resp.json()
                if isinstance(raw_books, list) and len(raw_books) > 0:
                    if library_id:
                        raw_books = [
                            b for b in raw_books
                            if str(b.get("libraryId") or b.get("library_id")) == str(library_id)
                        ]
                    total = len(raw_books)
                    start = page * size
                    page_items = raw_books[start:start + size]
                    paged_content = [raw_app_book_to_dto(item) for item in page_items if item.get("id")]
                    return ensure_page_dto({
                        "content": paged_content,
                        "totalElements": total,
                        "number": page,
                        "size": size
                    }, default_page=page, default_size=size)
        except Exception:
            pass

        # Fallback to standard /komga/api/v1/books
        params = {"page": page, "size": size}
        if library_id:
            params["library_id"] = library_id
        resp = await self.komga_request(
            "GET",
            "/api/v1/books",
            user,
            pwd,
            params=params
        )
        if resp.status_code == 200:
            data = resp.json()
            if "content" in data and isinstance(data["content"], list):
                for b in data["content"]:
                    ensure_book_dto(b)
            return ensure_page_dto(data, default_page=page, default_size=size)

        return ensure_page_dto({"content": []}, default_page=page, default_size=size)

    def register_custom_series(self, unique_id: str, lib_id: str, name: str, dto: Dict[str, Any]) -> None:
        """Register a disambiguated series mapping."""
        self.custom_series[unique_id] = {
            "lib_id": str(lib_id),
            "name": name,
            "dto": dto
        }

    async def get_series_books_custom(
        self,
        unique_id: str,
        user: str,
        pwd: str
    ) -> List[Dict[str, Any]]:
        """Fetch books for a disambiguated non-ASCII series."""
        info = self.custom_series.get(unique_id)
        if not info and "-u-" in unique_id:
            # Self-healing: if server restarted, fetch library series to populate custom_series
            lib_id = unique_id.split("-u-")[0]
            try:
                from app.dto_utils import disambiguate_series_dto
                resp = await self.komga_request("GET", f"/api/v1/series?library_id={lib_id}&size=100", user, pwd)
                if resp.status_code == 200:
                    for s in resp.json().get("content", []):
                        disambiguate_series_dto(s)
            except Exception:
                pass
            info = self.custom_series.get(unique_id)

        if not info:
            return []

        lib_id = info["lib_id"]
        s_name = info["name"]
        native_headers = await self.get_native_headers(user, pwd)

        # 1. Try native Grimmory /api/v1/app/series/{name}/books
        enc = urllib.parse.quote(s_name, safe="")
        try:
            resp = await self.client.get(f"/api/v1/app/series/{enc}/books", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                if content:
                    return [raw_app_book_to_dto(b) for b in content if b.get("id")]
        except Exception:
            pass

        # 2. Try search: /api/v1/app/books/search?q={name}
        try:
            resp = await self.client.get(f"/api/v1/app/books/search?q={enc}&size=50", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                matched = [b for b in content if (b.get("title") == s_name or b.get("seriesName") == s_name) and str(b.get("libraryId", lib_id)) == str(lib_id)]
                if matched:
                    return [raw_app_book_to_dto(b) for b in matched if b.get("id")]
        except Exception:
            pass

        # 3. If no books under series, search books in library where title or seriesName matches
        try:
            resp = await self.client.get(f"/api/v1/app/books?libraryId={lib_id}&size=100", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                all_books = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                matched = [b for b in all_books if b.get("title") == s_name or b.get("seriesName") == s_name]
                if matched:
                    return [raw_app_book_to_dto(b) for b in matched if b.get("id")]
        except Exception:
            pass

        return []

grimmory_client = GrimmoryClient()

