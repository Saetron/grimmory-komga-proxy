from fastapi import APIRouter, Header, Response, status, HTTPException
from typing import Optional, Dict, Any
from app.grimmory_client import grimmory_client

router = APIRouter(tags=["Authentication"])

@router.get("/actuator/info")
async def actuator_info() -> Dict[str, Any]:
    return {
        "build": {
            "version": "1.12.0",
            "artifact": "komga",
            "name": "komga",
            "time": "2026-09-23T00:00:00Z"
        },
        "git": {
            "branch": "master",
            "commit": {"id": "1.12.0", "time": "2026-09-23T00:00:00Z"}
        }
    }

@router.get("/api/v1/users/me")
@router.get("/api/v2/users/me")
async def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Komga\""}
        )

    token = await grimmory_client.get_native_token(user, pwd)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"Komga\""}
        )

    data = {
        "id": user,
        "email": f"{user}@grimmory.local" if "@" not in user else user,
        "roles": ["ROLE_ADMIN", "ROLE_FILE_DOWNLOAD", "ROLE_PAGE_STREAMING", "USER", "ADMIN", "FILE_DOWNLOAD", "PAGE_STREAMING"],
        "sharedAllLibraries": True,
        "sharedLibrariesIds": [],
        "sharedLibrariesExcludedIds": [],
        "labelsAllow": [],
        "labelsExclude": [],
        "ageRestriction": None
    }

    try:
        from app.sync_service import sync_service
        sync_service.maybe_trigger_sync_on_login(user, pwd)
    except Exception:
        pass
    roles = data.get("roles", [])
    for standard_role in ["USER", "FILE_DOWNLOAD", "PAGE_STREAMING", "ADMIN"]:
        if standard_role not in roles:
            roles.append(standard_role)
    data["roles"] = roles
    data.setdefault("sharedAllLibraries", True)

    user_libs = await grimmory_client.get_user_library_ids(user, pwd)
    if user_libs:
        data["sharedLibrariesIds"] = sorted(list(user_libs))
    else:
        data.setdefault("sharedLibrariesIds", [])

    data.setdefault("labelsAllow", [])
    data.setdefault("labelsExclude", [])
    return data

@router.get("/api/v1/login/set-cookie")
async def login_set_cookie(response: Response, authorization: Optional[str] = Header(None)) -> Response:
    user, pwd = grimmory_client.extract_credentials(authorization)
    if user:
        response.set_cookie(
            key="KOMGA-SESSION",
            value=f"{user}:{pwd}",
            httponly=True,
            samesite="lax"
        )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

@router.get("/api/logout")
async def logout(response: Response) -> Response:
    response.delete_cookie(key="KOMGA-SESSION")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

@router.get("/api/v1/client-settings/global")
@router.get("/api/v1/client-settings/global/list")
async def get_global_client_settings() -> Dict[str, Any]:
    return {}

@router.get("/api/v1/client-settings/user")
@router.get("/api/v1/client-settings/user/list")
async def get_user_client_settings() -> Dict[str, Any]:
    return {}
