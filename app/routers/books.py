from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_book_dto, extract_search_filters

router = APIRouter(prefix="/api/v1/books", tags=["Books"])

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

    # If series_id is specified in query, Grimmory requires querying /series/{id}/books
    series_id = params.pop("series_id", None) or params.pop("seriesId", None)
    if series_id:
        if "-standalone-" in series_id:
            b_id = series_id.split("-standalone-")[-1]
            book = await grimmory_client.get_book_dto(b_id, user, pwd)
            content = [ensure_book_dto(book)] if book else []
            return ensure_page_dto({"content": content}, default_page=page, default_size=size)

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

        resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/books", user, pwd, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if "content" in data and isinstance(data["content"], list):
                await grimmory_client.enrich_books_page_count(data["content"], user, pwd)
                for b in data["content"]:
                    ensure_book_dto(b)
            return ensure_page_dto(data, default_page=page, default_size=size)
        return ensure_page_dto({"content": []}, default_page=page, default_size=size)

    search_query = params.get("search")
    if search_query:
        return await grimmory_client.search_books(search_query, user, pwd, page=page, size=size, library_id=library_id)

    read_status_param = params.get("read_status", "") or params.get("readStatus", "")
    if "in_progress" in str(read_status_param).lower() or "readprogress" in sort.lower() or "readdate" in sort.lower():
        return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size, library_id=library_id)

    if "releasedate" in sort.lower():
        return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort.lower() for k in ["createddate", "lastmodified", "created", "addedon"]):
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)

    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        await grimmory_client.enrich_books_page_count(data["content"], user, pwd)
        for b in data["content"]:
            ensure_book_dto(b)
    return ensure_page_dto(data, default_page=page, default_size=size)


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

    # 1. Check if filtering by series
    series_id = filters.get("series_id") or params.get("series_id") or params.get("seriesId")
    if series_id:
        if "-standalone-" in series_id:
            b_id = series_id.split("-standalone-")[-1]
            book = await grimmory_client.get_book_dto(b_id, user, pwd)
            content = [ensure_book_dto(book)] if book else []
            return ensure_page_dto({"content": content}, default_page=page, default_size=size)

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

        # Grimmory ONLY returns books for a series via /series/{id}/books
        params.pop("series_id", None)
        params.pop("seriesId", None)
        resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/books", user, pwd, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if "content" in data and isinstance(data["content"], list):
                await grimmory_client.enrich_books_page_count(data["content"], user, pwd)
                for b in data["content"]:
                    ensure_book_dto(b)
            return ensure_page_dto(data, default_page=page, default_size=size)
        return ensure_page_dto({"content": []}, default_page=page, default_size=size)


    # Extract library filter
    if "library_id" in filters:
        params["library_id"] = filters["library_id"]
    if "libraryId" in params:
        params["library_id"] = params.pop("libraryId")
    library_id = params.get("library_id")

    # 2. Check if filtering by read status / in-progress
    read_status = filters.get("read_status", [])
    query_read_status = params.get("read_status", "") or params.get("readStatus", "")
    is_in_prog = any("in_progress" in str(s).lower() for s in (read_status if isinstance(read_status, list) else [read_status]))
    if is_in_prog or "in_progress" in str(query_read_status).lower() or "readprogress" in sort.lower() or "readdate" in sort.lower():
        return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size, library_id=library_id)

    search_query = params.get("search") or filters.get("search")
    if search_query:
        return await grimmory_client.search_books(search_query, user, pwd, page=page, size=size, library_id=library_id)

    # 3. Check if sorting by recently added or released
    sort_str = (sort + " " + str(body.get("sort", ""))).lower()
    if "releasedate" in sort_str:
        return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)

    if any(k in sort_str for k in ["createddate", "lastmodified", "created", "addedon"]):
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)

    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            await grimmory_client.enrich_books_page_count(data["content"], user, pwd)
            for b in data["content"]:
                ensure_book_dto(b)
        return ensure_page_dto(data, default_page=page, default_size=size)

    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.get("/ondeck")
async def get_ondeck_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/latest")
async def get_latest_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    return await grimmory_client.get_latest_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/released")
async def get_released_books_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    page = int(params.get("page", 0))
    size = int(params.get("size", 20))
    library_id = params.get("library_id") or params.get("libraryId")
    return await grimmory_client.get_released_books(user, pwd, page=page, size=size, library_id=library_id)


@router.get("/duplicates")
async def get_duplicate_books() -> Dict[str, Any]:
    return ensure_page_dto({"content": []})


@router.get("/{book_id}")
async def get_book(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    book = await grimmory_client.get_book_dto(book_id, user, pwd)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return ensure_book_dto(book)


@router.get("/{book_id}/previous")
async def get_book_previous(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    current_book = await grimmory_client.get_book_dto(book_id, user, pwd)
    s_id = current_book.get("seriesId", "") if current_book else ""

    if not s_id or "-u-" in s_id:
        book = await grimmory_client.get_adjacent_book(book_id, direction="previous", user=user, pwd=pwd)
        if not book:
            raise HTTPException(status_code=404, detail="No previous book")
        return ensure_book_dto(book)

    try:
        resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{book_id}/previous", user, pwd)
        if resp.status_code == 200:
            book = resp.json()
            await grimmory_client.enrich_book(book, user, pwd, fetch_dimensions=True)
            return ensure_book_dto(book)
    except Exception:
        pass

    book = await grimmory_client.get_adjacent_book(book_id, direction="previous", user=user, pwd=pwd)
    if not book:
        raise HTTPException(status_code=404, detail="No previous book")
    return ensure_book_dto(book)


@router.get("/{book_id}/next")
async def get_book_next(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    current_book = await grimmory_client.get_book_dto(book_id, user, pwd)
    s_id = current_book.get("seriesId", "") if current_book else ""

    if not s_id or "-u-" in s_id:
        book = await grimmory_client.get_adjacent_book(book_id, direction="next", user=user, pwd=pwd)
        if not book:
            raise HTTPException(status_code=404, detail="No next book")
        return ensure_book_dto(book)

    try:
        resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{book_id}/next", user, pwd)
        if resp.status_code == 200:
            book = resp.json()
            await grimmory_client.enrich_book(book, user, pwd, fetch_dimensions=True)
            return ensure_book_dto(book)
    except Exception:
        pass

    book = await grimmory_client.get_adjacent_book(book_id, direction="next", user=user, pwd=pwd)
    if not book:
        raise HTTPException(status_code=404, detail="No next book")
    return ensure_book_dto(book)


@router.get("/{book_id}/thumbnail")
async def get_book_thumbnail(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{book_id}/thumbnail", user, pwd)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
    )


@router.get("/{book_id}/pages")
async def get_book_pages(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> List[Dict[str, Any]]:
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
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    
    komga_resp = await grimmory_client.komga_request(
        "GET",
        f"/api/v1/books/{book_id}/pages/{page_number}",
        user,
        pwd,
        params=params
    )
    if komga_resp.status_code == 200:
        return StreamingResponse(
            content=iter([komga_resp.content]),
            status_code=200,
            media_type=komga_resp.headers.get("Content-Type", "image/jpeg")
        )

    # Fallback to Grimmory native page image endpoints
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    for path in [
        f"/api/v1/media/book/{book_id}/cbx/pages/{page_number}",
        f"/api/v1/cbx/{book_id}/pages/{page_number}",
        f"/api/v1/pdf/{book_id}/pages/{page_number}"
    ]:
        try:
            native_resp = await grimmory_client.client.get(path, headers=native_headers)
            if native_resp.status_code == 200:
                return StreamingResponse(
                    content=iter([native_resp.content]),
                    status_code=200,
                    media_type=native_resp.headers.get("Content-Type", "image/jpeg")
                )
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="Page not found")


@router.get("/{book_id}/file")
async def download_book_file(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> StreamingResponse:
    user, pwd = grimmory_client.extract_credentials(authorization)
    resp = await grimmory_client.komga_request("GET", f"/api/v1/books/{book_id}/file", user, pwd)
    if resp.status_code == 200:
        return StreamingResponse(
            content=iter([resp.content]),
            status_code=200,
            headers={
                "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
                "Content-Disposition": resp.headers.get("Content-Disposition", f'attachment; filename="book-{book_id}.cbz"')
            }
        )
    raise HTTPException(status_code=resp.status_code, detail="Failed to download book file")


# Support both /read-progress and /progression (Komga & Komic variants)
@router.get("/{book_id}/read-progress")
@router.get("/{book_id}/progression")
async def get_read_progress(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Optional[Dict[str, Any]]:
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
    user, pwd = grimmory_client.extract_credentials(authorization)
    try:
        body = await request.json()
    except Exception:
        body = {}

    page = body.get("page", 1)
    completed = body.get("completed", False)

    success = await grimmory_client.update_read_progress(book_id, page, completed, user, pwd)
    if success:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{book_id}/read-progress", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/{book_id}/progression", status_code=status.HTTP_204_NO_CONTENT)
async def delete_read_progress(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    await grimmory_client.reset_read_progress(book_id, user, pwd)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{book_id}/manifest")
@router.get("/{book_id}/manifest/divina")
async def get_book_manifest(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
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
