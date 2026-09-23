from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client

router = APIRouter(prefix="/api/v1/books", tags=["Books"])

@router.get("")
async def list_books(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        for b in data["content"]:
            await grimmory_client.enrich_book(b, user, pwd)
    return data

@router.post("/list")
async def list_books_post(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """Support POST /api/v1/books/list used by Komic/Komelia."""
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            if "seriesIds" in body and body["seriesIds"]:
                params["series_id"] = body["seriesIds"][0]
            if "libraryIds" in body and body["libraryIds"]:
                params["library_id"] = body["libraryIds"][0]
            if "searchTerm" in body and body["searchTerm"]:
                params["search"] = body["searchTerm"]
    except Exception:
        pass

    resp = await grimmory_client.komga_request("GET", "/api/v1/books", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            for b in data["content"]:
                await grimmory_client.enrich_book(b, user, pwd)
        return data

    return {
        "content": [],
        "pageable": {"sort": {"sorted": False, "unsorted": True, "empty": True}, "offset": 0, "pageNumber": 0, "pageSize": 20, "paged": True, "unpaged": False},
        "totalElements": 0,
        "totalPages": 0,
        "last": True,
        "number": 0,
        "sort": {"sorted": False, "unsorted": True, "empty": True},
        "size": 20,
        "numberOfElements": 0,
        "first": True,
        "empty": True
    }

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
    return {
        "content": [],
        "pageable": {"sort": {"sorted": False, "unsorted": True, "empty": True}, "offset": 0, "pageNumber": 0, "pageSize": 20, "paged": True, "unpaged": False},
        "totalElements": 0,
        "totalPages": 0,
        "last": True,
        "number": 0,
        "sort": {"sorted": False, "unsorted": True, "empty": True},
        "size": 20,
        "numberOfElements": 0,
        "first": True,
        "empty": True
    }

@router.get("/{book_id}")
async def get_book(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    book = await grimmory_client.get_book_dto(book_id, user, pwd)
    if not book:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Book not found")
    return book

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

@router.get("/{book_id}/file")
async def download_book_file(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> StreamingResponse:
    user, pwd = grimmory_client.extract_credentials(authorization)
    target_url = f"{grimmory_client.base_url}/komga/api/v1/books/{book_id}/file"
    auth_header = grimmory_client.get_basic_auth_header(user, pwd)

    req = grimmory_client.client.build_request("GET", target_url, headers=auth_header)
    resp = await grimmory_client.client.send(req, stream=True)

    if resp.status_code != 200:
        await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail="File download failed")

    return StreamingResponse(
        resp.aiter_bytes(),
        status_code=resp.status_code,
        headers={
            "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
            "Content-Disposition": resp.headers.get("Content-Disposition", f"attachment; filename=\"book_{book_id}\"")
        }
    )

@router.get("/{book_id}/pages", response_model=List[Dict[str, Any]])
async def get_book_pages(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> List[Dict[str, Any]]:
    """Return synthesized array of PageDto matching Komga specification."""
    user, pwd = grimmory_client.extract_credentials(authorization)
    pages = await grimmory_client.get_book_pages_metadata(book_id, user, pwd)
    return pages

@router.get("/{book_id}/pages/{page_number}")
@router.get("/{book_id}/pages/{page_number}/raw")
@router.get("/{book_id}/pages/{page_number}/thumbnail")
async def get_book_page_image(
    book_id: str,
    page_number: int,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    # Stream page from Grimmory's /komga/api/v1/books/{book_id}/pages/{page_number}
    resp = await grimmory_client.komga_request(
        "GET",
        f"/api/v1/books/{book_id}/pages/{page_number}",
        user,
        pwd
    )
    if resp.status_code == 200:
        return Response(
            content=resp.content,
            status_code=200,
            headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
        )

    # Fallback to Grimmory native media endpoint
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    fallback_resp = await grimmory_client.client.get(
        f"/api/v1/media/book/{book_id}/cbx/pages/{page_number}",
        headers=native_headers
    )
    if fallback_resp.status_code == 200:
        return Response(
            content=fallback_resp.content,
            status_code=200,
            headers={"Content-Type": fallback_resp.headers.get("Content-Type", "image/jpeg")}
        )

    raise HTTPException(status_code=resp.status_code, detail="Page not found")

@router.get("/{book_id}/read-progress")
async def get_read_progress(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    progress = await grimmory_client.get_read_progress(book_id, user, pwd)
    if not progress:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No read progress found")
    return progress

@router.patch("/{book_id}/read-progress")
@router.put("/{book_id}/read-progress")
async def update_read_progress(
    book_id: str,
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    body = await request.json()
    page = body.get("page", 1)
    completed = body.get("completed", False)
    success = await grimmory_client.update_read_progress(book_id, page, completed, user, pwd)
    if not success:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update progress")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

@router.delete("/{book_id}/read-progress")
async def delete_read_progress(
    book_id: str,
    response: Response,
    authorization: Optional[str] = Header(None)
) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    await grimmory_client.reset_read_progress(book_id, user, pwd)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

@router.get("/{book_id}/readlists")
async def get_book_readlists() -> List[Dict[str, Any]]:
    return []

@router.get("/{book_id}/manifest")
@router.get("/{book_id}/manifest/divina")
async def get_book_manifest(
    book_id: str,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """WebPub Divina manifest for readers that request it."""
    user, pwd = grimmory_client.extract_credentials(authorization)
    book = await grimmory_client.get_book_dto(book_id, user, pwd)
    pages = await grimmory_client.get_book_pages_metadata(book_id, user, pwd)

    reading_order = []
    for p in pages:
        p_num = p["number"]
        reading_order.append({
            "href": f"/api/v1/books/{book_id}/pages/{p_num}",
            "type": p.get("mediaType", "image/jpeg"),
            "width": p.get("width"),
            "height": p.get("height")
        })

    title = book.get("name", f"Book {book_id}") if book else f"Book {book_id}"
    return {
        "@context": "https://readium.org/webpub-manifest/context.jsonld",
        "metadata": {
            "title": title,
            "readingProgression": "rtl",
            "numberOfPages": len(pages)
        },
        "readingOrder": reading_order
    }
