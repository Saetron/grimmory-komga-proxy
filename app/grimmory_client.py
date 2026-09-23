import base64
import time
import httpx
from typing import Optional, Dict, Any, List, Tuple
from cachetools import TTLCache
from app.config import settings

# In-memory caches:
# Cache JWT tokens for native Grimmory API (1 hour TTL)
token_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
# Cache page info per book (1 hour TTL)
page_cache: TTLCache = TTLCache(maxsize=5000, ttl=3600)
# Cache book DTOs (5 min TTL)
book_cache: TTLCache = TTLCache(maxsize=5000, ttl=300)

import asyncio

class GrimmoryClient:
    def __init__(self):
        self.base_url = settings.GRIMMORY_URL
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop = None

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

        # 1. Try CBX page dimensions & page info
        dimensions_list: List[Dict[str, Any]] = []
        info_dict: Dict[int, str] = {}
        try:
            dim_resp = await self.client.get(f"/api/v1/cbx/{book_id}/page-dimensions", headers=native_headers)
            if dim_resp.status_code == 200:
                dimensions_list = dim_resp.json()
        except Exception:
            pass

        try:
            info_resp = await self.client.get(f"/api/v1/cbx/{book_id}/page-info", headers=native_headers)
            if info_resp.status_code == 200:
                for item in info_resp.json():
                    p_num = item.get("pageNumber")
                    p_name = item.get("displayName")
                    if p_num is not None:
                        info_dict[p_num] = p_name or f"{p_num:03d}"
        except Exception:
            pass

        pages: List[Dict[str, Any]] = []
        if dimensions_list:
            for item in dimensions_list:
                num = item.get("pageNumber", 1)
                file_name = info_dict.get(num, f"{num:03d}")
                if not any(file_name.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    file_name = f"{file_name}.jpg"
                
                page_dto = {
                    "number": num,
                    "fileName": file_name,
                    "mediaType": "image/jpeg",
                    "width": item.get("width", 1080),
                    "height": item.get("height", 1920),
                    "sizeBytes": 0,
                    "size": "0 B"
                }
                pages.append(page_dto)
        else:
            # 2. Try CBX raw pages list (array of ints)
            try:
                pages_resp = await self.client.get(f"/api/v1/cbx/{book_id}/pages", headers=native_headers)
                if pages_resp.status_code == 200:
                    page_numbers = pages_resp.json()
                    if isinstance(page_numbers, list) and len(page_numbers) > 0:
                        for p in page_numbers:
                            pages.append({
                                "number": int(p),
                                "fileName": f"{int(p):03d}.jpg",
                                "mediaType": "image/jpeg",
                                "width": 1080,
                                "height": 1920,
                                "sizeBytes": 0,
                                "size": "0 B"
                            })
            except Exception:
                pass

        if not pages:
            # 3. Try PDF pages
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

        if pages:
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
        await self.enrich_book(book, user, pwd)
        book_cache[book_id] = book
        return book

    async def enrich_book(self, book: Dict[str, Any], user: str, pwd: str) -> None:
        """Ensure media.pagesCount is accurate and attach readProgress if present."""
        book_id = str(book.get("id"))
        pages = await self.get_book_pages_metadata(book_id, user, pwd)
        if pages:
            if "media" not in book or not isinstance(book["media"], dict):
                book["media"] = {"status": "READY", "mediaType": "application/x-cbz", "mediaProfile": "DIVINA"}
            book["media"]["pagesCount"] = len(pages)

        # Fallback series info for standalone books
        if not book.get("seriesId"):
            lib_id = book.get("libraryId", "0")
            book["seriesId"] = f"{lib_id}-standalone-{book_id}"
            book.setdefault("seriesTitle", book.get("name", "Standalone"))
            book["oneshot"] = True

        # Check and attach readProgress
        progress = await self.get_read_progress(book_id, user, pwd)
        if progress:
            book["readProgress"] = progress

    async def get_read_progress(self, book_id: str, user: str, pwd: str) -> Optional[Dict[str, Any]]:
        """Fetch read progress from Grimmory native API and format as Komga ReadProgressDto."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get(f"/api/v1/app/books/{book_id}/progress", headers=native_headers)
            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    return None
                
                # Check cbxProgress, pdfProgress, or fileProgress
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

    async def get_ondeck_books(self, user: str, pwd: str, page: int = 0, size: int = 20) -> Dict[str, Any]:
        """Fetch books currently reading (on deck)."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/continue-reading", headers=native_headers)
            if resp.status_code == 200:
                raw_books = resp.json()
                total = len(raw_books)
                start = page * size
                page_items = raw_books[start:start + size]

                tasks = [self.get_book_dto(str(item.get("id")), user, pwd) for item in page_items if item.get("id")]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                paged_content = [b for b in results if isinstance(b, dict)]

                total_pages = (total + size - 1) // size if total > 0 else 0

                return {
                    "content": paged_content,
                    "pageable": {
                        "sort": {"sorted": False, "unsorted": True, "empty": True},
                        "offset": start,
                        "pageNumber": page,
                        "pageSize": size,
                        "paged": True,
                        "unpaged": False
                    },
                    "totalElements": total,
                    "totalPages": total_pages,
                    "last": page >= total_pages - 1,
                    "number": page,
                    "sort": {"sorted": False, "unsorted": True, "empty": True},
                    "size": size,
                    "numberOfElements": len(paged_content),
                    "first": page == 0,
                    "empty": len(paged_content) == 0
                }
        except Exception:
            pass

        return {
            "content": [],
            "pageable": {
                "sort": {"sorted": False, "unsorted": True, "empty": True},
                "offset": 0,
                "pageNumber": 0,
                "pageSize": size,
                "paged": True,
                "unpaged": False
            },
            "totalElements": 0,
            "totalPages": 0,
            "last": True,
            "number": page,
            "sort": {"sorted": False, "unsorted": True, "empty": True},
            "size": size,
            "numberOfElements": 0,
            "first": True,
            "empty": True
        }

    async def get_latest_books(self, user: str, pwd: str, page: int = 0, size: int = 20) -> Dict[str, Any]:
        """Fetch recently added books."""
        native_headers = await self.get_native_headers(user, pwd)
        try:
            resp = await self.client.get("/api/v1/app/books/recently-added", headers=native_headers)
            if resp.status_code == 200:
                raw_books = resp.json()
                total = len(raw_books)
                start = page * size
                page_items = raw_books[start:start + size]

                tasks = [self.get_book_dto(str(item.get("id")), user, pwd) for item in page_items if item.get("id")]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                paged_content = [b for b in results if isinstance(b, dict)]

                total_pages = (total + size - 1) // size if total > 0 else 0

                return {
                    "content": paged_content,
                    "pageable": {
                        "sort": {"sorted": False, "unsorted": True, "empty": True},
                        "offset": start,
                        "pageNumber": page,
                        "pageSize": size,
                        "paged": True,
                        "unpaged": False
                    },
                    "totalElements": total,
                    "totalPages": total_pages,
                    "last": page >= total_pages - 1,
                    "number": page,
                    "sort": {"sorted": False, "unsorted": True, "empty": True},
                    "size": size,
                    "numberOfElements": len(paged_content),
                    "first": page == 0,
                    "empty": len(paged_content) == 0
                }
        except Exception:
            pass

        # Fallback to standard /komga/api/v1/books sorted by created,desc
        resp = await self.komga_request(
            "GET",
            "/api/v1/books",
            user,
            pwd,
            params={"page": page, "size": size, "sort": "created,desc"}
        )
        if resp.status_code == 200:
            data = resp.json()
            for b in data.get("content", []):
                await self.enrich_book(b, user, pwd)
            return data

        return {
            "content": [],
            "pageable": {
                "sort": {"sorted": False, "unsorted": True, "empty": True},
                "offset": 0,
                "pageNumber": 0,
                "pageSize": size,
                "paged": True,
                "unpaged": False
            },
            "totalElements": 0,
            "totalPages": 0,
            "last": True,
            "number": page,
            "sort": {"sorted": False, "unsorted": True, "empty": True},
            "size": size,
            "numberOfElements": 0,
            "first": True,
            "empty": True
        }

grimmory_client = GrimmoryClient()
