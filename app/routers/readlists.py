from fastapi import APIRouter, Header, Request, HTTPException
from typing import Optional, Dict, Any, List
from app.grimmory_client import grimmory_client
from app.dto_utils import ensure_page_dto

router = APIRouter(prefix="/api/v1", tags=["ReadLists & Collections"])

@router.get("/readlists")
async def get_readlists() -> Dict[str, Any]:
    return ensure_page_dto({"content": []})

@router.get("/collections")
async def get_collections(
    request: Request,
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    params = dict(request.query_params)
    resp = await grimmory_client.komga_request("GET", "/api/v1/collections", user, pwd, params=params)
    if resp.status_code == 200:
        data = resp.json()
        return ensure_page_dto(data)
    return ensure_page_dto({"content": []})

@router.get("/authors")
@router.get("/authors/names")
async def get_authors(
    authorization: Optional[str] = Header(None)
) -> List[Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    native_headers = await grimmory_client.get_native_headers(user, pwd)
    try:
        resp = await grimmory_client.client.get("/api/v1/app/authors", headers=native_headers)
        if resp.status_code == 200:
            authors_data = resp.json()
            if isinstance(authors_data, list):
                return [a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in authors_data]
    except Exception:
        pass
    return []

@router.get("/authors/roles")
async def get_author_roles() -> List[str]:
    return ["WRITER", "PENCILLER", "INKER", "COLORIST", "LETTERER", "COVER", "EDITOR", "TRANSLATOR"]

@router.get("/age-ratings")
async def get_age_ratings() -> List[int]:
    return []
