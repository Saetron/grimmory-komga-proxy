from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_series_dto, ensure_book_dto, extract_search_filters, disambiguate_series_dto
from app.db import db

router = APIRouter(prefix="/api/v1/series", tags=["Series"])

@router.get("")
async def list_series(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))

    # Normalize libraryId to library_id for Grimmory compatibility
    if "libraryId" in params:
        params["library_id"] = params.pop("libraryId")
    library_id = params.get("library_id")
    sort = params.get("sort", "")

    search_query = params.get("search")
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

        sort_lower = sort.lower()
        if "titlesort" in sort_lower or "name" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: str(s.get("metadata", {}).get("titleSort") or s.get("name", "")).lower(), reverse=reverse)
        elif "created" in sort_lower or "added" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: str(s.get("created") or s.get("lastModified") or ""), reverse=reverse)
        elif "bookscount" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: int(s.get("booksCount", 0)), reverse=reverse)

        total = len(cached_series)
        start = page * size
        paged_content = cached_series[start:start + size]
        for s in paged_content:
            disambiguate_series_dto(s)
            ensure_series_dto(s)
        return ensure_page_dto({
            "content": paged_content,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        for s in data["content"]:
            disambiguate_series_dto(s)
            ensure_series_dto(s)
    return ensure_page_dto(data, default_page=page, default_size=size)


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
    body = {}

    # Normalize libraryId in query
    if "libraryId" in params:
        params["library_id"] = params.pop("libraryId")

    try:
        raw_body = await request.json()
        if isinstance(raw_body, dict):
            body = raw_body
        filters = extract_search_filters(body)
        if "library_id" in filters:
            params["library_id"] = filters["library_id"]
        if "search" in filters:
            params["search"] = filters["search"]
    except Exception:
        pass

    library_id = params.get("library_id")
    sort_val = sort
    if not sort_val and "sort" in body:
        sort_val = str(body["sort"])

    search_query = params.get("search") or filters.get("search")
    if search_query:
        return await grimmory_client.search_series(search_query, user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort_val.lower() for k in ["lastmodified", "created", "updated"]):
        return await grimmory_client.get_updated_series(user, pwd, page=page, size=size, library_id=library_id)

    # Check SQLite DB first for instant snappy response
    cached_series = db.get_all_series(library_id=library_id)
    if cached_series:
        user_libs = await grimmory_client.get_user_library_ids(user, pwd)
        if user_libs is not None:
            cached_series = [s for s in cached_series if str(s.get("libraryId") or s.get("library_id", "")) in user_libs]

        sort_lower = sort_val.lower()
        if "titlesort" in sort_lower or "name" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: str(s.get("metadata", {}).get("titleSort") or s.get("name", "")).lower(), reverse=reverse)
        elif "created" in sort_lower or "added" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: str(s.get("created") or s.get("lastModified") or ""), reverse=reverse)
        elif "bookscount" in sort_lower:
            reverse = "desc" in sort_lower
            cached_series.sort(key=lambda s: int(s.get("booksCount", 0)), reverse=reverse)

        total = len(cached_series)
        start = page * size
        paged_content = cached_series[start:start + size]
        for s in paged_content:
            disambiguate_series_dto(s)
            ensure_series_dto(s)
        return ensure_page_dto({
            "content": paged_content,
            "totalElements": total,
            "number": page,
            "size": size
        }, default_page=page, default_size=size)

    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            for s in data["content"]:
                disambiguate_series_dto(s)
                ensure_series_dto(s)
        return ensure_page_dto(data, default_page=page, default_size=size)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)



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

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}", user, pwd)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Series not found")
    data = resp.json()
    disambiguate_series_dto(data)
    return ensure_series_dto(data, user=user)


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

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books for series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        await grimmory_client.enrich_books_page_count(data["content"], user, pwd)
        for b in data["content"]:
            ensure_book_dto(b)
    return ensure_page_dto(data, default_page=page, default_size=size)


@router.get("/{series_id}/thumbnail")
async def get_series_thumbnail(
    series_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)

    # Handle virtual standalone series
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{b_id}/thumbnail", user, pwd)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
        )

    # Handle custom disambiguated series: use first book's thumbnail
    if "-u-" in series_id:
        books = await grimmory_client.get_series_books_custom(series_id, user, pwd)
        if books:
            first_b_id = str(books[0]["id"])
            resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{first_b_id}/thumbnail", user, pwd)
            if resp.status_code == 200:
                return Response(
                    content=resp.content,
                    status_code=200,
                    headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
                )

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/thumbnail", user, pwd)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
    )

