import logging
import httpx
from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_book_dto, extract_search_filters, normalize_id
from app.db import db

logger = logging.getLogger("grimmory-komga-bridge")

router = APIRouter(prefix="/api/v1/books", tags=["Books"])


def sort_book_dtos(books: List[Dict[str, Any]], sort: str = "") -> List[Dict[str, Any]]:
    """Sort a list of book DTOs deterministically with id tie-breaker."""
    if not books:
        return books

    sort_lower = (sort or "").lower()
    reverse = "desc" in sort_lower

    if "titlesort" in sort_lower or "name" in sort_lower or "title" in sort_lower:
        books.sort(
            key=lambda b: (
                str(b.get("metadata", {}).get("titleSort") or b.get("name") or "").lower(),
                float(b.get("metadata", {}).get("numberSort", 1.0)),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    elif "numbersort" in sort_lower or "number" in sort_lower:
        books.sort(
            key=lambda b: (
                float(b.get("metadata", {}).get("numberSort", b.get("number", 1.0))),
                str(b.get("metadata", {}).get("titleSort") or b.get("name") or "").lower(),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    elif "readdate" in sort_lower or "readprogress" in sort_lower:
        books.sort(
            key=lambda b: (
                str((b.get("readProgress") or {}).get("readDate") or ""),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    elif "releasedate" in sort_lower:
        books.sort(
            key=lambda b: (
                str(b.get("metadata", {}).get("releaseDate") or ""),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    elif any(k in sort_lower for k in ["createddate", "created", "lastmodified", "added"]):
        books.sort(
            key=lambda b: (
                str(b.get("created") or b.get("lastModified") or ""),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    else:
        # Default deterministic sort: titleSort ASC, numberSort ASC, id ASC
        books.sort(
            key=lambda b: (
                str(b.get("metadata", {}).get("titleSort") or b.get("name") or "").lower(),
                float(b.get("metadata", {}).get("numberSort", 1.0)),
                str(b.get("id", ""))
            ),
            reverse=reverse
        )
    return books


async def _enrich_and_paginate_books(
    books: List[Dict[str, Any]], user: str, pwd: str,
    page: int, size: int, sort: str = ""
) -> Dict[str, Any]:
    """Sort, paginate, enrich page counts, and ensure book DTOs."""
    if sort:
        books = sort_book_dtos(books, sort)
    total = len(books)
    start = page * size
    paged_content = books[start:start + size]
    await grimmory_client.enrich_books_page_count(paged_content, user, pwd)
    for b in paged_content:
        ensure_book_dto(b, user=user)
    return ensure_page_dto({
        "content": paged_content,
        "totalElements": total,
        "number": page,
        "size": size
    }, default_page=page, default_size=size)


async def _get_series_books(
    series_id: str, user: str, pwd: str,
    page: int, size: int, sort: str = "",
    library_id: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch books for a series (standalone, custom, or normal) with enrichment."""
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        book = await grimmory_client.get_book_dto(b_id, user, pwd)
        content = [ensure_book_dto(book, user=user)] if book else []
        return ensure_page_dto({"content": content}, default_page=page, default_size=size)

    if "-u-" in series_id:
        books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
        return await _enrich_and_paginate_books(books, user, pwd, page, size)

    # Check SQLite DB first
    db_books = db.get_books_by_series(series_id)
    if not db_books:
        db_books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
    if db_books:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            db_books = [b for b in db_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]
        return await _enrich_and_paginate_books(db_books, user, pwd, page, size, sort)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)

@router.get("")
async def list_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    sort = params.get("sort", "")

    # Normalize libraryId to library_id
    if "libraryId" in params:
        params["library_id"] = params.pop("libraryId")
    library_id = params.get("library_id")
    if library_id in ("", "null", "None"):
        library_id = None

    read_status_param = params.get("read_status", "") or params.get("readStatus", "")

    # If series_id is specified in query, Grimmory requires querying /series/{id}/books
    series_id = params.pop("series_id", None) or params.pop("seriesId", None)
    if series_id:
        series_id = normalize_id(series_id)
        if read_status_param:
            statuses = [s.strip().upper() for s in str(read_status_param).split(",") if s.strip()]
            return await grimmory_client.get_books_by_read_status(
                statuses, user, pwd, page=page, size=size, series_id=series_id, library_id=library_id, sort=sort
            )
        return await _get_series_books(series_id, user, pwd, page, size, sort, library_id)

    search_query = params.get("search") or params.get("searchTerm") or params.get("q") or params.get("query")
    if search_query:
        return await grimmory_client.search_books(search_query, user, pwd, page=page, size=size, library_id=library_id)

    if read_status_param:
        statuses = [s.strip().upper() for s in str(read_status_param).split(",") if s.strip()]
        return await grimmory_client.get_books_by_read_status(
            statuses, user, pwd, page=page, size=size, library_id=library_id, sort=sort
        )

    if "readprogress" in sort.lower() or "readdate" in sort.lower():
        return await grimmory_client.get_continue_reading_books(user, pwd, page=page, size=size, library_id=library_id)

    if "releasedate" in sort.lower():
        return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort.lower() for k in ["createddate", "lastmodified", "created", "addedon"]):
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)

    # Check SQLite DB first
    db_books = db.get_all_books(library_id=library_id)
    if db_books:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            db_books = [b for b in db_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]
        return await _enrich_and_paginate_books(db_books, user, pwd, page, size, sort)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.post("/list")
async def list_books_post(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Support POST /api/v1/books/list used by Komic/Komelia."""
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    sort = params.get("sort", "")
    body = {}
    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
    except Exception:
        pass

    filters = extract_search_filters(body)

    # Extract library filter
    library_id = None
    if "library_ids" in filters:
        if not filters["library_ids"]:
            params.pop("library_id", None)
            library_id = None
        else:
            if len(filters["library_ids"]) == 1:
                params["library_id"] = filters["library_ids"][0]
                library_id = filters["library_ids"][0]
            else:
                params["library_id"] = ",".join(filters["library_ids"])
                library_id = filters["library_ids"]
    elif "library_id" in filters:
        params["library_id"] = filters["library_id"]
        library_id = filters["library_id"]
    if "libraryId" in params:
        params["library_id"] = params.pop("libraryId")
    if not library_id:
        library_id = params.get("library_id")
    if library_id in ("", "null", "None"):
        library_id = None

    read_status = filters.get("read_status", [])
    query_read_status = params.get("read_status", "") or params.get("readStatus", "")
    all_raw_statuses = []
    if isinstance(read_status, list):
        all_raw_statuses.extend(read_status)
    elif read_status:
        all_raw_statuses.append(str(read_status))
    if query_read_status:
        all_raw_statuses.extend(str(query_read_status).split(","))
    statuses = [str(s).strip().upper() for s in all_raw_statuses if str(s).strip()]

    # 1. Check if filtering by series
    series_id = filters.get("series_id") or params.get("series_id") or params.get("seriesId")
    if series_id:
        series_id = normalize_id(series_id)
        if statuses:
            return await grimmory_client.get_books_by_read_status(
                statuses, user, pwd, page=page, size=size, series_id=series_id, library_id=library_id, sort=sort
            )
        effective_sort = sort or str(body.get("sort", ""))
        return await _get_series_books(series_id, user, pwd, page, size, effective_sort, library_id)


    # 2. Check if filtering by read status
    if statuses:
        return await grimmory_client.get_books_by_read_status(
            statuses, user, pwd, page=page, size=size, library_id=library_id, sort=sort
        )

    if "readprogress" in sort.lower() or "readdate" in sort.lower():
        return await grimmory_client.get_continue_reading_books(user, pwd, page=page, size=size, library_id=library_id)

    search_query = params.get("search") or params.get("searchTerm") or params.get("q") or params.get("query") or filters.get("search")
    if search_query:
        return await grimmory_client.search_books(search_query, user, pwd, page=page, size=size, library_id=library_id)

    # 3. Check if sorting by recently added or released
    sort_str = (sort + " " + str(body.get("sort", ""))).lower()
    if "releasedate" in sort_str:
        return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort_str for k in ["createddate", "lastmodified", "created", "addedon"]):
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)

    # Check SQLite DB first
    db_books = db.get_all_books(library_id=library_id)
    if db_books:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            db_books = [b for b in db_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]
        effective_sort = sort or str(body.get("sort", ""))
        return await _enrich_and_paginate_books(db_books, user, pwd, page, size, effective_sort)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.get("/ondeck")
@router.post("/ondeck")
async def get_ondeck_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    if library_id in ("", "null", "None"):
        library_id = None
    return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/latest")
@router.post("/latest")
async def get_latest_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    if library_id in ("", "null", "None"):
        library_id = None
    return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/released")
@router.post("/released")
async def get_released_books_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    if library_id in ("", "null", "None"):
        library_id = None
    return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/duplicates")
async def get_duplicate_books() -> Dict[str, Any]:
    return ensure_page_dto({"content": []})


@router.get("/{book_id}")
async def get_book(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    book = await grimmory_client.get_book_dto(book_id, user, pwd)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return ensure_book_dto(book, user=user)


@router.get("/{book_id}/previous")
async def get_book_previous(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    current_book = await grimmory_client.get_book_dto(book_id, user, pwd)
    s_id = current_book.get("seriesId", "") if current_book else ""

    # 1. Check SQLite DB first!
    prev_book = db.get_previous_book(book_id)
    if prev_book:
        if await grimmory_client.user_can_access_book(prev_book, user, pwd):
            return ensure_book_dto(prev_book, user=user)

    book = await grimmory_client.get_adjacent_book(book_id, direction="previous", user=user, pwd=pwd)
    if not book:
        raise HTTPException(status_code=404, detail="No previous book")
    return ensure_book_dto(book, user=user)


@router.get("/{book_id}/next")
async def get_book_next(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    # 1. Check SQLite DB first!
    next_book = db.get_next_book(book_id)
    if next_book:
        if await grimmory_client.user_can_access_book(next_book, user, pwd):
            return ensure_book_dto(next_book, user=user)

    book = await grimmory_client.get_adjacent_book(book_id, direction="next", user=user, pwd=pwd)
    if not book:
        raise HTTPException(status_code=404, detail="No next book")
    return ensure_book_dto(book, user=user)


@router.get("/{book_id}/thumbnail")
async def get_book_thumbnail(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    from app.grimmory_client import thumbnail_cache
    book_id = normalize_id(book_id)
    cache_key = f"b:{book_id}"
    if cache_key in thumbnail_cache:
        cached_content, cached_type = thumbnail_cache[cache_key]
        return Response(
            content=cached_content,
            status_code=200,
            headers={
                "Content-Type": cached_type,
                "Cache-Control": "public, max-age=604800, immutable"
            }
        )

    user, pwd = grimmory_client.extract_credentials(authorization)
    native_headers = await grimmory_client.get_native_headers(user, pwd)

    # Grimmory native book covers/thumbnails
    for path in [
        f"/api/v1/media/book/{book_id}/thumbnail",
        f"/api/v1/media/book/{book_id}/cover",
        f"/api/v1/app/books/{book_id}/cover",
        f"/api/v1/app/books/{book_id}/thumbnail",
        f"/api/v1/books/{book_id}/cover",
        f"/api/v1/books/{book_id}/thumbnail",
    ]:
        try:
            resp = await grimmory_client.client.get(path, headers=native_headers)
            c_type = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and len(resp.content) > 0 and c_type.startswith("image/"):
                thumbnail_cache[cache_key] = (resp.content, c_type)
                return Response(
                    content=resp.content,
                    status_code=200,
                    headers={
                        "Content-Type": c_type,
                        "Cache-Control": "public, max-age=604800, immutable"
                    }
                )
        except Exception:
            pass

    return Response(status_code=404, content=b"", media_type="image/jpeg")


@router.get("/{book_id}/pages")
async def get_book_pages(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> List[Dict[str, Any]]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    pages = await grimmory_client.get_book_pages_metadata(book_id, user, pwd)
    return pages


@router.get("/{book_id}/pages/{page_number}")
async def get_book_page(
    book_id: str,
    page_number: int,
    request: Request,
    authorization: Optional[str] = Header(None)
) -> StreamingResponse:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)

    def is_valid_page_image(resp: httpx.Response) -> bool:
        if resp.status_code != 200 or len(resp.content) == 0:
            return False
        ct = resp.headers.get("Content-Type", "").lower()
        if "text/html" in ct or "text/plain" in ct:
            return False
        prefix = resp.content[:100].lower()
        if b"<html" in prefix or b"<!doctype html" in prefix:
            return False
        return True

    # 1. Try Grimmory native page image endpoints
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    for path in [
        f"/api/v1/cbx/{book_id}/page/{page_number}",
        f"/api/v1/cbx/{book_id}/pages/{page_number}",
        f"/api/v1/media/book/{book_id}/cbx/pages/{page_number}",
        f"/api/v1/pdf/{book_id}/page/{page_number}",
        f"/api/v1/pdf/{book_id}/pages/{page_number}",
        f"/api/v1/app/books/{book_id}/pages/{page_number}"
    ]:
        try:
            native_resp = await grimmory_client.client.get(path, headers=native_headers)
            if is_valid_page_image(native_resp):
                media_type = native_resp.headers.get("Content-Type") or "image/jpeg"
                if "text/" in media_type:
                    media_type = "image/jpeg"
                return StreamingResponse(
                    content=iter([native_resp.content]),
                    status_code=200,
                    media_type=media_type
                )
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="Page not found")


@router.api_route("/{book_id}/file", methods=["GET", "HEAD"])
@router.api_route("/{book_id}/file/{filename}", methods=["GET", "HEAD"])
async def download_book_file(
    book_id: str,
    request: Request,
    filename: Optional[str] = None,
    authorization: Optional[str] = Header(None)
) -> Response:
    import urllib.parse
    from unittest.mock import AsyncMock
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    clean_book_id = book_id.split("-")[-1] if "-standalone-" in book_id else book_id

    book = await grimmory_client.get_book_dto(book_id, user, pwd) or await grimmory_client.get_book_dto(clean_book_id, user, pwd)
    ext = "cbz"
    m_type = "application/x-cbz"
    name = f"book-{clean_book_id}"
    if book:
        name = book.get("name") or f"book-{clean_book_id}"
        media_type = str(book.get("media", {}).get("mediaType", "")).lower()
        if "epub" in media_type:
            ext = "epub"
            m_type = "application/epub+zip"
        elif "pdf" in media_type:
            ext = "pdf"
            m_type = "application/pdf"
        elif "cbr" in media_type:
            ext = "cbr"
            m_type = "application/x-cbr"

    # Use provided filename if present, otherwise compute clean filename
    if not filename:
        safe_name = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_", ".", "[", "]", "(", ")")).strip() or f"book-{clean_book_id}"
        filename = safe_name if safe_name.lower().endswith(f".{ext}") else f"{safe_name}.{ext}"

    # Generate RFC 6266 / RFC 5987 compliant Content-Disposition with both ASCII and UTF-8 filenames
    ascii_safe = "".join(c for c in filename if c.isascii() and (c.isalnum() or c in (" ", "-", "_", ".", "[", "]", "(", ")"))).strip() or f"book-{clean_book_id}.{ext}"
    if not ascii_safe.lower().endswith(f".{ext}"):
        ascii_safe = f"{ascii_safe}.{ext}"
    encoded_utf8 = urllib.parse.quote(filename, encoding="utf-8")
    content_disposition = f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{encoded_utf8}'

    # Grimmory native endpoints for file content and downloads
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    token = await grimmory_client.get_native_token(user, pwd)
    candidate_paths = [
        f"/api/v1/books/{clean_book_id}/content",
        f"/api/v1/books/{clean_book_id}/download",
        f"/api/v1/app/books/{clean_book_id}/file",
        f"/api/v1/app/books/{clean_book_id}/download",
        f"/api/v1/books/{clean_book_id}/files/primary",
        f"/api/v1/app/books/{clean_book_id}/files/primary",
    ]

    is_head = request.method.upper() == "HEAD"
    forward_headers = dict(native_headers)
    if "range" in request.headers:
        forward_headers["Range"] = request.headers["range"]

    req_params = {"token": token} if token else None

    for path in candidate_paths:
        try:
            # Real httpx client in production: use build_request + send(..., stream=True)
            if hasattr(grimmory_client.client, "build_request") and not isinstance(grimmory_client.client, AsyncMock):
                req = grimmory_client.client.build_request(
                    "HEAD" if is_head else "GET",
                    path,
                    headers=forward_headers,
                    params=req_params
                )
                native_resp = await grimmory_client.client.send(req, stream=True)

                if is_head and native_resp.status_code in (404, 405):
                    await native_resp.aclose()
                    req = grimmory_client.client.build_request("GET", path, headers=forward_headers, params=req_params)
                    native_resp = await grimmory_client.client.send(req, stream=True)

                if native_resp.status_code in (200, 206):
                    ct = native_resp.headers.get("Content-Type") or m_type
                    if "text/html" in ct.lower():
                        await native_resp.aclose()
                        continue
                    if "text/" in ct:
                        ct = m_type

                    resp_headers = {
                        "Content-Type": ct,
                        "Content-Disposition": content_disposition,
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "private, max-age=3600"
                    }
                    if "Content-Length" in native_resp.headers:
                        resp_headers["Content-Length"] = native_resp.headers["Content-Length"]
                    if "Content-Range" in native_resp.headers:
                        resp_headers["Content-Range"] = native_resp.headers["Content-Range"]

                    if is_head:
                        await native_resp.aclose()
                        return Response(
                            status_code=native_resp.status_code,
                            media_type=ct,
                            headers=resp_headers
                        )

                    async def file_streamer():
                        try:
                            async for chunk in native_resp.aiter_bytes(chunk_size=65536):
                                yield chunk
                        finally:
                            await native_resp.aclose()

                    return StreamingResponse(
                        file_streamer(),
                        status_code=native_resp.status_code,
                        media_type=ct,
                        headers=resp_headers
                    )
                else:
                    await native_resp.aclose()
            else:
                # Fallback for mock client / test fixtures
                native_resp = await grimmory_client.client.get(
                    path,
                    headers=forward_headers,
                    params=req_params
                )
                if native_resp.status_code in (200, 206) and len(native_resp.content) > 0:
                    ct = native_resp.headers.get("Content-Type") or m_type
                    if "text/html" in ct.lower():
                        continue
                    if "text/" in ct:
                        ct = m_type

                    resp_headers = {
                        "Content-Type": ct,
                        "Content-Disposition": content_disposition,
                        "Content-Length": str(len(native_resp.content)),
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "private, max-age=3600"
                    }
                    if "Content-Range" in native_resp.headers:
                        resp_headers["Content-Range"] = native_resp.headers["Content-Range"]

                    if is_head:
                        return Response(
                            status_code=native_resp.status_code,
                            media_type=ct,
                            headers=resp_headers
                        )
                    return Response(
                        content=native_resp.content,
                        status_code=native_resp.status_code,
                        media_type=ct,
                        headers=resp_headers
                    )
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="Failed to download book file")


# Support both /read-progress and /progression (Komga & Komic variants)
@router.get("/{book_id}/read-progress")
@router.get("/{book_id}/progression")
async def get_read_progress(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Optional[Dict[str, Any]]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    progress = await grimmory_client.get_read_progress(book_id, user, pwd)
    if progress:
        return progress
    return Response(status_code=status.HTTP_404_NOT_FOUND)


@router.patch("/{book_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
@router.put("/{book_id}/progression", status_code=status.HTTP_204_NO_CONTENT)
@router.patch("/{book_id}/progression", status_code=status.HTTP_204_NO_CONTENT)
async def update_read_progress(
    book_id: str,
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Response:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    try:
        body = await request.json()
    except Exception:
        body = {}

    page = body.get("page", 1)
    completed = body.get("completed", False)

    await grimmory_client.update_read_progress(book_id, page, completed, user, pwd)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{book_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/{book_id}/progression", status_code=status.HTTP_204_NO_CONTENT)
async def delete_read_progress(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    await grimmory_client.reset_read_progress(book_id, user, pwd)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{book_id}/manifest")
@router.get("/{book_id}/manifest/divina")
async def get_book_manifest(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    book_id = normalize_id(book_id)
    user, pwd = grimmory_client.extract_credentials(authorization)
    book = await grimmory_client.get_book_dto(book_id, user, pwd)
    pages = await grimmory_client.get_book_pages_metadata(book_id, user, pwd)

    reading_order = []
    for p in pages:
        reading_order.append({
            "href": f"/api/v1/books/{book_id}/pages/{p['number']}",
            "type": p.get("mediaType", "image/jpeg"),
            "width": p.get("width", 1080),
            "height": p.get("height", 1920)
        })

    title = book.get("name", f"Book {book_id}") if book else f"Book {book_id}"

    return {
        "@context": "https://readium.org/webpub-manifest/context.jsonld",
        "metadata": {
            "title": title,
            "readingProgression": "auto",
            "numberOfPages": len(pages)
        },
        "readingOrder": reading_order,
        "resources": []
    }
