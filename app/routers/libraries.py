from fastapi import APIRouter, Header, HTTPException, status
from typing import Optional, List, Dict, Any
from app.grimmory_client import grimmory_client

router = APIRouter(prefix="/api/v1/libraries", tags=["Libraries"])

def ensure_library_dto(lib: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required Komga LibraryDto fields are present."""
    defaults = {
        "unavailable": False,
        "scanCbx": True,
        "scanPdf": True,
        "scanEpub": True,
        "scanForceModifiedTime": False,
        "scanInterval": "EVERY_6H",
        "scanOnStartup": False,
        "scanDirectoryExclusions": [],
        "repairExtensions": False,
        "convertToCbz": False,
        "emptyTrashAfterScan": False,
        "seriesCover": "FIRST",
        "hashFiles": True,
        "hashPages": False,
        "hashKoreader": False,
        "analyzeDimensions": True,
        "importComicInfoBook": True,
        "importComicInfoSeries": True,
        "importComicInfoCollection": True,
        "importComicInfoReadList": True,
        "importComicInfoSeriesAppendVolume": False,
        "importEpubBook": True,
        "importEpubSeries": True,
        "importMylarSeries": True,
        "importLocalArtwork": True,
        "importBarcodeIsbn": True,
        "root": "/books",
        "oneshotsDirectory": None
    }
    for k, v in defaults.items():
        if k not in lib:
            lib[k] = v
    if lib.get("scanInterval") == "EVERY_6_HOURS":
        lib["scanInterval"] = "EVERY_6H"
    return lib

@router.get("", response_model=List[Dict[str, Any]])
async def list_libraries(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    resp = await grimmory_client.komga_request("GET", "/api/v1/libraries", user, pwd)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch libraries")
    libraries = resp.json()
    return [ensure_library_dto(lib) for lib in libraries]

@router.get("/{library_id}", response_model=Dict[str, Any])
async def get_library(library_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    resp = await grimmory_client.komga_request("GET", f"/api/v1/libraries/{library_id}", user, pwd)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Library not found")
    return ensure_library_dto(resp.json())
