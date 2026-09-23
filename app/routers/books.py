from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_book_dto

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

    if "readProgress.readDate" in sort:
        return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size)
    if "createdDate" in sort or "metadata.releaseDate" in sort:
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size)

    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
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

    read_status = body.get("readStatus", [])
    if isinstance(read_status, list) and "IN_PROGRESS" in read_status:
        return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size)
    if "readProgress.readDate" in sort:
        return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size)
    if "createdDate" in sort or "metadata.releaseDate" in sort:
        return await grimmory_client.get_latest_books(user, pwd, page=page, size=size)

    if "seriesIds" in body and body["seriesIds"]:
        params["series_id"] = body["seriesIds"][0]
    if "libraryIds" in body and body["libraryIds"]:
        params["library_id"] = body["libraryIds"][0]
    if "searchTerm" in body and body["searchTerm"]:
        params["search"] = body["searchTerm"]

    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
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
    page = int(request.query_params.get("page", 0))
    size = int(request.query_params.get("size", 20))
    return await grimmory_client.get_ondeck_books(user, pwd, page=page, size=size)


@router.get("/latest")
async def get_latest_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    page = int(request.query_params.get("page", 0))
    size = int(request.query_params.get("size", 20))
    return await grimmory_client.get_latest_books(user, pwd, page=page, size=size)


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

    # Fallback to Grimmory native CBX page image
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    native_resp = await grimmory_client.client.get(
        f"/api/v1/cbx/{book_id}/pages/{page_number}",
        headers=native_headers
    )
    if native_resp.status_code == 200:
        return StreamingResponse(
            content=iter([native_resp.content]),
            status_code=200,
            media_type=native_resp.headers.get("Content-Type", "image/jpeg")
        )

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


@router.get("/{book_id}/read-progress")
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
