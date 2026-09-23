from fastapi import APIRouter, Header, Request, Response, HTTPException, status
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client

router = APIRouter(prefix="/api/v1/series", tags=["Series"])

def ensure_series_dto(series: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required Komga SeriesDto fields are present."""
    series.setdefault("booksCount", 0)
    series.setdefault("booksReadCount", 0)
    series.setdefault("booksUnreadCount", series.get("booksCount", 0))
    series.setdefault("booksInProgressCount", 0)
    series.setdefault("deleted", False)
    series.setdefault("oneshot", False)
    series.setdefault("metadata", {
        "status": "ONGOING",
        "created": series.get("created", ""),
        "lastModified": series.get("lastModified", ""),
        "title": series.get("name", ""),
        "titleSort": series.get("name", ""),
        "summary": "",
        "readingDirection": "RIGHT_TO_LEFT",
        "publisher": "",
        "ageRating": None,
        "language": "en",
        "genres": [],
        "tags": [],
        "totalBookCount": series.get("booksCount", 0)
    })
    return series

@router.get("")
async def list_series(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        for s in data["content"]:
            ensure_series_dto(s)
    return data

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

@router.get("/latest")
@router.get("/new")
@router.get("/updated")
async def list_series_special(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    if "sort" not in params:
        params["sort"] = "lastModified,desc"
    resp = await grimmory_client.komga_request("GET", "/api/v1/series", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            for s in data["content"]:
                ensure_series_dto(s)
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

    # Handle virtual standalone series
    if "-standalone-" in series_id:
        b_id = series_id.split("-standalone-")[-1]
        book = await grimmory_client.get_book_dto(b_id, user, pwd)
        content = [book] if book else []
        return {
            "content": content,
            "pageable": {"sort": {"sorted": False, "unsorted": True, "empty": True}, "offset": 0, "pageNumber": 0, "pageSize": 20, "paged": True, "unpaged": False},
            "totalElements": len(content),
            "totalPages": 1 if content else 0,
            "last": True,
            "number": 0,
            "sort": {"sorted": False, "unsorted": True, "empty": True},
            "size": 20,
            "numberOfElements": len(content),
            "first": True,
            "empty": len(content) == 0
        }

    params = dict(request.query_params)
    resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{series_id}/books", user, pwd, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch books for series")
    data = resp.json()
    if "content" in data and isinstance(data["content"], list):
        for b in data["content"]:
            await grimmory_client.enrich_book(b, user, pwd)
    return data

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
