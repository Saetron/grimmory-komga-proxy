from fastapi import APIRouter, Header, HTTPException, status
from typing import Optional, List, Dict, Any
from app.config import settings
from app.grimmory_client import grimmory_client
from app.db import db

router = APIRouter(prefix="/api/v1/libraries", tags=["Libraries"])

def ensure_library_dto(lib: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required Komga LibraryDto fields are present with correct string and boolean types."""
    lib = dict(lib)
    lib_id = str(lib.get("id", "1"))
    lib["id"] = lib_id
    lib["name"] = str(lib.get("name") or settings.CUSTOM_LIBRARY_NAMES.get(lib_id, f"Library {lib_id}"))
    lib["root"] = str(lib.get("root") or f"/books/{lib['name']}")

    raw_interval = lib.get("scanInterval")
    lib["scanInterval"] = "EVERY_6H" if raw_interval in ("EVERY_6_HOURS", None, "") else str(raw_interval)
    lib["scanDirectoryExclusions"] = list(lib.get("scanDirectoryExclusions") or [])
    lib["seriesCover"] = str(lib.get("seriesCover") or "FIRST")

    defaults = {
        "unavailable": False,
        "scanCbx": True,
        "scanPdf": True,
        "scanEpub": True,
        "scanForceModifiedTime": False,
        "scanOnStartup": False,
        "repairExtensions": False,
        "convertToCbz": False,
        "emptyTrashAfterScan": False,
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
        "oneshotsDirectory": None
    }
    for k, v in defaults.items():
        if k not in lib or lib[k] is None:
            if k == "oneshotsDirectory":
                lib[k] = None
            else:
                lib[k] = v
        elif isinstance(v, bool):
            lib[k] = bool(lib[k])

    return lib

@router.get("", response_model=List[Dict[str, Any]])
async def list_libraries(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    libraries = await grimmory_client.get_libraries(user, pwd)

    # Merge with SQLite libraries (cached or auto-discovered from books and series)
    db_libs = db.get_all_libraries()
    existing_ids = {str(l.get("id")) for l in libraries if l.get("id")}
    for dl in db_libs:
        dl_id = str(dl.get("id"))
        if dl_id and dl_id not in existing_ids:
            libraries.append(dl)
            existing_ids.add(dl_id)

    if not libraries:
        libraries = [{"id": "1", "name": "Default"}]

    return [ensure_library_dto(lib) for lib in libraries]

@router.get("/{library_id}", response_model=Dict[str, Any])
async def get_library(library_id: str, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    user, pwd = grimmory_client.extract_credentials(authorization)
    target_id = str(library_id)

    libraries = await grimmory_client.get_libraries(user, pwd)
    for lib in libraries:
        if str(lib.get("id")) == target_id:
            return ensure_library_dto(lib)

    db_lib = db.get_library(target_id)
    if db_lib:
        return ensure_library_dto(db_lib)

    if target_id in db.get_all_library_ids():
        name = settings.CUSTOM_LIBRARY_NAMES.get(target_id, f"Library {target_id}")
        return ensure_library_dto({"id": target_id, "name": name})

    raise HTTPException(status_code=404, detail="Library not found")


