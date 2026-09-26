import logging
import httpx
from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_series_dto, ensure_book_dto, extract_search_filters, disambiguate_series_dto
from app.db import db

logger = logging.getLogger("grimmory-komga-bridge")

router = APIRouter(prefix="/api/v1/series", tags=["Series"])


def _sort_series(series_list: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    """Sort series list by the given sort parameter."""
    sort_lower = sort.lower()
    if "titlesort" in sort_lower or "name" in sort_lower:
        reverse = "desc" in sort_lower
        series_list.sort(key=lambda s: str(s.get("metadata", {}).get("titleSort") or s.get("name", "")).lower(), reverse=reverse)
    elif "created" in sort_lower or "added" in sort_lower:
        reverse = "desc" in sort_lower
        series_list.sort(key=lambda s: str(s.get("created") or s.get("lastModified") or ""), reverse=reverse)
    elif "bookscount" in sort_lower:
        reverse = "desc" in sort_lower
        series_list.sort(key=lambda s: int(s.get("booksCount", 0)), reverse=reverse)
    return series_list


async def _paginate_and_enrich_series(
    series_list: List[Dict[str, Any]], user: str, page: int, size: int
) -> Dict[str, Any]:
    """Paginate and enrich series DTOs with disambiguation and read counts."""
    total = len(series_list)
    start = page * size
    paged_content = series_list[start:start + size]
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


async def _list_series_common(
    user: str, pwd: str, page: int, size: int,
    library_id: Optional[str], sort: str,
    search_query: Optional[str]
) -> Dict[str, Any]:
    """Shared logic for GET and POST series listing."""
    if search_query:
        return await grimmory_client.search_series(search_query, user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort.lower() for k in ["lastmodified", "created", "updated"]):
        return await grimmory_client.get_updated_series(user, pwd, page=page, size=size, library_id=library_id)

    # Check SQLite DB first for instant snappy response
    cached_series = db.get_all_series(library_id=library_id)
    if cached_series:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            cached_series = [s for s in cached_series if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]
        if sort:
            _sort_series(cached_series, sort)
        return await _paginate_and_enrich_series(cached_series, user, page, size)

    all_s = await grimmory_client.get_all_series(user, pwd, library_id=library_id)
    if all_s:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            all_s = [s for s in all_s if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]
        return await _paginate_and_enrich_series(all_s, user, page, size)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.get("")
async def list_series(
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
    sort = params.get("sort", "")
    search_query = params.get("search") or params.get("searchTerm") or params.get("q") or params.get("query")
    return await _list_series_common(user, pwd, page, size, library_id, sort, search_query)


@router.post("/list")
async def list_series_post(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    Komic and Komelia use POST /api/v1/series/list with search/filter criteria in body.
    Grimmory returns 501, so we convert body filters to GET query params.
    """
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    sort = params.get("sort", "")

    filters = {}
    try:
        raw_body = await request.json()
        body = raw_body if isinstance(raw_body, dict) else {}
        filters = extract_search_filters(body)
        if not sort and "sort" in body:
            sort = str(body["sort"])
    except Exception:
        pass

    # Resolve library_id from filters or query params
    library_id = None
    if "library_ids" in filters:
        if filters["library_ids"]:
            library_id = filters["library_ids"][0] if len(filters["library_ids"]) == 1 else ",".join(filters["library_ids"])
    elif "library_id" in filters:
        library_id = filters["library_id"]
    if not library_id:
        library_id = params.get("library_id") or params.get("libraryId")
    if library_id in ("", "null", "None"):
        library_id = None

    search_query = (
        params.get("search") or params.get("searchTerm") or params.get("q") or
        params.get("query") or filters.get("search")
    )
    return await _list_series_common(user, pwd, page, size, library_id, sort, search_query)


@router.get("/latest")
@router.get("/new")
@router.get("/updated")
async def list_series_special(
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
    return await grimmory_client.get_updated_series(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/alphabetical-groups")
async def get_alphabetical_groups() -> List[Dict[str, Any]]:
    return []


@router.get("/genres")
async def get_genres() -> List[str]:
    return []


@router.get("/release-dates")
async def get_series_release_dates() -> List[str]:
    return []


@router.get("/{series_id}")
async def get_series(
    series_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)

    # Handle virtual standalone series
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        book = await grimmory_client.get_book_dto(b_id, user, pwd)
        if book:
            return ensure_series_dto({
                "id": series_id,
                "libraryId": book.get("libraryId", "0"),
                "name": book.get("name", "Standalone"),
                "url": f"/api/v1/series/{series_id}",
                "created": book.get("created", ""),
                "lastModified": book.get("lastModified", ""),
                "booksCount": 1,
                "oneshot": True
            }, user=user)

    # Handle custom disambiguated series
    if "-u-" in series_id:
        if series_id not in grimmory_client.custom_series:
            await grimmory_client.ensure_custom_series_loaded(series_id, user, pwd)
        if series_id in grimmory_client.custom_series:
            return ensure_series_dto(grimmory_client.custom_series[series_id]["dto"], user=user)

        books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
        if books:
            first_b = books[0]
            s_name = first_b.get("seriesTitle") or "Series"
            lib_id = str(first_b.get("libraryId", "0"))
            fallback_dto = {
                "id": series_id,
                "libraryId": lib_id,
                "name": s_name,
                "url": f"/api/v1/series/{series_id}",
                "created": first_b.get("created", ""),
                "lastModified": first_b.get("lastModified", ""),
                "booksCount": len(books),
                "oneshot": False
            }
            grimmory_client.register_custom_series(series_id, lib_id, s_name, fallback_dto)
            return ensure_series_dto(fallback_dto, user=user)

        raise HTTPException(status_code=404, detail="Series not found")

    db_series = db.get_series(series_id)
    if db_series:
        if await grimmory_client.user_can_access_series(db_series, user, pwd):
            disambiguate_series_dto(db_series)
            return ensure_series_dto(db_series, user=user)
        raise HTTPException(status_code=404, detail="Series not found")

    # Check books by series
    series_books = db.get_books_by_series(series_id)
    if not series_books:
        series_books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
    if series_books:
        first_b = series_books[0]
        s_name = first_b.get("seriesTitle") or first_b.get("metadata", {}).get("series") or "Series"
        lib_id = str(first_b.get("libraryId", "0"))
        fallback_dto = {
            "id": series_id,
            "libraryId": lib_id,
            "name": s_name,
            "url": f"/api/v1/series/{series_id}",
            "created": first_b.get("created", ""),
            "lastModified": first_b.get("lastModified", ""),
            "booksCount": len(series_books),
            "oneshot": False
        }
        db.save_series(fallback_dto)
        return ensure_series_dto(fallback_dto, user=user)

    raise HTTPException(status_code=404, detail="Series not found")


@router.get("/{series_id}/collections")
async def get_series_collections(
    series_id: str,
    authorization: Optional[str] = Header(None)
) -> List[Dict[str, Any]]:
    return []


@router.get("/{series_id}/books")
async def get_series_books(
    series_id: str,
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))

    read_status_param = params.get("read_status", "") or params.get("readStatus", "")
    if read_status_param:
        statuses = [s.strip().upper() for s in str(read_status_param).split(",") if s.strip()]
        return await grimmory_client.get_books_by_read_status(
            statuses, user, pwd, page=page, size=size, series_id=series_id, sort=params.get("sort", "")
        )

    # Handle virtual standalone series
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        book = await grimmory_client.get_book_dto(b_id, user, pwd)
        content = [ensure_book_dto(book)] if book else []
        return ensure_page_dto({"content": content}, default_page=page, default_size=size)

    # Handle custom disambiguated series
    if "-u-" in series_id:
        books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
        start = page * size
        paged_content = books[start:start + size]
        await grimmory_client.enrich_books_page_count(paged_content, user, pwd)
        for b in paged_content:
            ensure_book_dto(b)
        return ensure_page_dto({
            "content": paged_content,
            "totalElements": len(books),
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    # Check SQLite DB first for instant snappy response
    db_books = db.get_books_by_series(series_id)
    if not db_books:
        db_books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
    if db_books:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            db_books = [b for b in db_books if str(b.get("libraryId") or b.get("library_id", "")) in user_libs]
        total = len(db_books)
        start = page * size
        paged_content = db_books[start:start + size]
        await grimmory_client.enrich_books_page_count(paged_content, user, pwd)
        for b in paged_content:
            ensure_book_dto(b)
        return ensure_page_dto({
            "content": paged_content,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.get("/{series_id}/thumbnail")
async def get_series_thumbnail(
    series_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    from app.grimmory_client import thumbnail_cache
    cache_key = f"s:{series_id}"
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

    try:
        first_b_id = None
        # Handle virtual standalone series
        if "-standalone-" in series_id:
            first_b_id = series_id.split("-standalone-")[-1]
        elif "-u-" in series_id:
            books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
            if books:
                first_b_id = str(books[0]["id"])
        else:
            s_books = db.get_books_by_series(series_id)
            if s_books:
                first_b_id = str(s_books[0]["id"])
            else:
                books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
                if books:
                    first_b_id = str(books[0]["id"])

        candidate_paths = []
        if first_b_id:
            b_cache_key = f"b:{first_b_id}"
            if b_cache_key in thumbnail_cache:
                cached_content, cached_type = thumbnail_cache[b_cache_key]
                thumbnail_cache[cache_key] = (cached_content, cached_type)
                return Response(
                    content=cached_content,
                    status_code=200,
                    headers={
                        "Content-Type": cached_type,
                        "Cache-Control": "public, max-age=604800, immutable"
                    }
                )
            candidate_paths.extend([
                f"/api/v1/media/book/{first_b_id}/thumbnail",
                f"/api/v1/media/book/{first_b_id}/cover",
                f"/api/v1/app/books/{first_b_id}/cover",
                f"/api/v1/app/books/{first_b_id}/thumbnail",
                f"/api/v1/books/{first_b_id}/cover",
                f"/api/v1/books/{first_b_id}/thumbnail",
            ])

        candidate_paths.extend([
            f"/api/v1/media/series/{series_id}/thumbnail",
            f"/api/v1/media/series/{series_id}/cover",
            f"/api/v1/app/series/{series_id}/cover",
            f"/api/v1/app/series/{series_id}/thumbnail",
            f"/api/v1/series/{series_id}/thumbnail",
        ])

        for path in candidate_paths:
            try:
                resp = await grimmory_client.client.get(path, headers=native_headers)
                c_type = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and len(resp.content) > 0 and c_type.startswith("image/"):
                    thumbnail_cache[cache_key] = (resp.content, c_type)
                    if first_b_id:
                        thumbnail_cache[f"b:{first_b_id}"] = (resp.content, c_type)
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
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        logger.warning(f"[Thumbnail] Timeout or network error fetching thumbnail for series {series_id}: {e}")
        return Response(status_code=404, content=b"", media_type="image/jpeg")
    except Exception as e:
        logger.error(f"[Thumbnail] Error fetching thumbnail for series {series_id}: {e}")
        return Response(status_code=404, content=b"", media_type="image/jpeg")


@router.post("/{series_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
@router.patch("/{series_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
async def update_series_read_progress(
    series_id: str,
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    try:
        body = await request.json()
    except Exception:
        body = {}
    completed = body.get("completed", True)
    books = db.get_books_by_series(series_id)
    for b in books:
        b_id = str(b.get("id"))
        if b_id:
            if completed:
                p_count = (b.get("media") or {}).get("pagesCount", 1) or 1
                await grimmory_client.update_read_progress(b_id, page=p_count, completed=True, user=user, pwd=pwd)
            else:
                await grimmory_client.reset_read_progress(b_id, user=user, pwd=pwd)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{series_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
async def delete_series_read_progress(
    series_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    books = db.get_books_by_series(series_id)
    for b in books:
        b_id = str(b.get("id"))
        if b_id:
            await grimmory_client.reset_read_progress(b_id, user=user, pwd=pwd)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
