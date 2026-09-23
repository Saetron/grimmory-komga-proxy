from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto, ensure_series_dto, ensure_book_dto

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
    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        for s in data["content"]:
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
    try:
        body = await request.json()
        if isinstance(body, dict):
            if "libraryIds" in body and body["libraryIds"]:
                params["library_id"] = body["libraryIds"][0]
            if "searchTerm" in body and body["searchTerm"]:
                params["search"] = body["searchTerm"]
    except Exception:
        pass

    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            for s in data["content"]:
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
    if "sort" not in params:
        params["sort"] = "lastModified,desc"
    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            for s in data["content"]:
                ensure_series_dto(s)
        return ensure_page_dto(data, default_page=page, default_size=size)
    return ensure_page_dto({"content": []}, default_page=page, default_size=size)


@router.get("/alphabetical-groups")
async def get_alphabetical_groups() -> List[Dict[str, Any]]:
    return []


@router.get("/genres")
async def get_genres() -> List[str]:
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
            })

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}", user, pwd)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Series not found")
    return ensure_series_dto(resp.json())


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

    # Handle virtual standalone series
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        book = await grimmory_client.get_book_dto(b_id, user, pwd)
        content = [ensure_book_dto(book)] if book else []
        return ensure_page_dto({"content": content}, default_page=page, default_size=size)

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books for series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
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

    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/thumbnail", user, pwd)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("Content-Type", "image/jpeg")}
    )
