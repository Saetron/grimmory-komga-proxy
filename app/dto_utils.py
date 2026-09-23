from typing import Dict, Any, List, Optional

def ensure_page_dto(data: Dict[str, Any], default_page: int = 0, default_size: int = 20) -> Dict[str, Any]:
    """Ensure a paginated response has Spring Data Pageable & Sort structures expected by Komga clients."""
    if not isinstance(data, dict):
        return data

    content = data.get("content", [])
    if not isinstance(content, list):
        content = []
        data["content"] = content

    total_elements = data.get("totalElements")
    if total_elements is None:
        total_elements = len(content)

    size = data.get("size") or default_size or 20
    current_page = data.get("number") if data.get("number") is not None else default_page
    total_pages = data.get("totalPages")
    if total_pages is None:
        total_pages = (total_elements + size - 1) // size if total_elements > 0 else 0

    data["content"] = content
    data["totalElements"] = total_elements
    data["totalPages"] = total_pages
    data["number"] = current_page
    data["size"] = size
    data["numberOfElements"] = len(content)
    data["first"] = current_page == 0
    data["last"] = current_page >= max(0, total_pages - 1)
    data["empty"] = len(content) == 0

    data["pageable"] = {
        "sort": {
            "empty": False,
            "sorted": True,
            "unsorted": False
        },
        "offset": current_page * size,
        "pageNumber": current_page,
        "pageSize": size,
        "paged": True,
        "unpaged": False
    }
    data["sort"] = {
        "empty": False,
        "sorted": True,
        "unsorted": False
    }
    return data


def ensure_series_dto(series: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required Komga SeriesDto fields are present for strict Swift/Kotlin clients."""
    now_iso = series.get("created") or series.get("lastModified") or "2026-01-01T00:00:00Z"
    series.setdefault("booksCount", 0)
    series.setdefault("booksReadCount", 0)
    series.setdefault("booksUnreadCount", series.get("booksCount", 0))
    series.setdefault("booksInProgressCount", 0)
    series.setdefault("deleted", False)
    series.setdefault("oneshot", False)
    series.setdefault("created", now_iso)
    series.setdefault("lastModified", now_iso)
    series.setdefault("fileLastModified", now_iso)
    series.setdefault("name", series.get("name", "Unknown Series"))
    series.setdefault("url", f"/api/v1/series/{series.get('id', '')}")

    meta = series.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        series["metadata"] = meta

    meta.setdefault("status", "ONGOING")
    meta.setdefault("statusLock", False)
    meta.setdefault("created", now_iso)
    meta.setdefault("lastModified", now_iso)
    meta.setdefault("title", series.get("name", ""))
    meta.setdefault("titleLock", False)
    meta.setdefault("titleSort", series.get("name", ""))
    meta.setdefault("titleSortLock", False)
    meta.setdefault("summary", "")
    meta.setdefault("summaryLock", False)
    meta.setdefault("readingDirection", "LEFT_TO_RIGHT")
    meta.setdefault("readingDirectionLock", False)
    meta.setdefault("publisher", "")
    meta.setdefault("publisherLock", False)
    meta.setdefault("ageRating", None)
    meta.setdefault("ageRatingLock", False)
    meta.setdefault("language", "en")
    meta.setdefault("languageLock", False)
    meta.setdefault("genres", [])
    meta.setdefault("genresLock", False)
    meta.setdefault("tags", [])
    meta.setdefault("tagsLock", False)
    meta.setdefault("totalBookCount", series.get("booksCount", 0))
    meta.setdefault("totalBookCountLock", False)
    meta.setdefault("alternateTitles", [])
    meta.setdefault("alternateTitlesLock", False)
    meta.setdefault("links", [])
    meta.setdefault("linksLock", False)
    meta.setdefault("sharingLabels", [])
    meta.setdefault("sharingLabelsLock", False)

    bmeta = series.get("booksMetadata")
    if not isinstance(bmeta, dict):
        bmeta = {}
        series["booksMetadata"] = bmeta
    bmeta.setdefault("authors", [])
    bmeta.setdefault("tags", [])
    bmeta.setdefault("summary", "")
    bmeta.setdefault("summaryNumber", "")
    bmeta.setdefault("summaryLock", False)
    bmeta.setdefault("created", now_iso)
    bmeta.setdefault("lastModified", now_iso)

    return series


def ensure_book_dto(book: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required Komga BookDto, MediaDto, and BookMetadataDto fields are present."""
    now_iso = book.get("created") or book.get("lastModified") or "2026-01-01T00:00:00Z"
    book.setdefault("created", now_iso)
    book.setdefault("lastModified", now_iso)
    book.setdefault("fileLastModified", now_iso)
    book.setdefault("deleted", False)
    book.setdefault("oneshot", False)
    book.setdefault("sizeBytes", 0)
    book.setdefault("size", "0 B")
    book.setdefault("fileHash", "")
    book.setdefault("number", 1)
    book.setdefault("name", "Untitled")
    book.setdefault("url", f"/api/v1/books/{book.get('id', '')}")

    # Fallback series info for standalone books
    if not book.get("seriesId"):
        lib_id = book.get("libraryId", "0")
        b_id = str(book.get("id", "0"))
        book["seriesId"] = f"{lib_id}-standalone-{b_id}"
        book.setdefault("seriesTitle", book.get("name", "Standalone"))
        book["oneshot"] = True
    elif not book.get("seriesTitle"):
        book["seriesTitle"] = book.get("name", "Series")

    # MediaDto
    media = book.get("media")
    if not isinstance(media, dict):
        media = {}
        book["media"] = media
    media.setdefault("status", "READY")
    media.setdefault("mediaType", "application/x-cbz")
    media.setdefault("mediaProfile", "DIVINA")
    if "pagesCount" not in media or media["pagesCount"] is None:
        media["pagesCount"] = 1
    media.setdefault("comment", "")
    media.setdefault("epubDivinaCompatible", False)
    media.setdefault("epubIsKepub", False)

    # BookMetadataDto
    meta = book.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        book["metadata"] = meta
    meta.setdefault("title", book.get("name", ""))
    meta.setdefault("titleLock", False)
    meta.setdefault("summary", "")
    meta.setdefault("summaryLock", False)
    num_str = str(book.get("number", "1.0"))
    meta.setdefault("number", num_str)
    meta.setdefault("numberLock", False)
    try:
        num_val = float(num_str)
    except Exception:
        num_val = 1.0
    meta.setdefault("numberSort", num_val)
    meta.setdefault("numberSortLock", False)
    meta.setdefault("releaseDate", None)
    meta.setdefault("releaseDateLock", False)
    meta.setdefault("authors", [])
    meta.setdefault("authorsLock", False)
    meta.setdefault("tags", [])
    meta.setdefault("tagsLock", False)
    meta.setdefault("isbn", "")
    meta.setdefault("isbnLock", False)
    meta.setdefault("links", [])
    meta.setdefault("linksLock", False)
    meta.setdefault("created", now_iso)
    meta.setdefault("lastModified", now_iso)

    return book


def raw_app_book_to_dto(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Grimmory native /api/v1/app/books/* object into a compliant Komga BookDto."""
    b_id = str(raw["id"])
    lib_id = str(raw.get("libraryId", "1"))
    series_name = raw.get("seriesName") or "Unknown Series"
    series_id = f"{lib_id}-{series_name.lower().replace(' ', '-')}"
    num = raw.get("seriesNumber", 1.0)
    title = raw.get("title") or f"Book {b_id}"
    added_on = raw.get("addedOn") or "2026-09-23T00:00:00Z"

    file_type = raw.get("primaryFileType")
    media_type = "application/x-cbz" if file_type == "CBX" else "application/pdf" if file_type == "PDF" else "application/epub+zip"

    dto = {
        "id": b_id,
        "seriesId": series_id,
        "seriesTitle": series_name,
        "libraryId": lib_id,
        "name": title,
        "url": f"/api/v1/books/{b_id}",
        "number": int(num) if isinstance(num, (int, float)) and num == int(num) else num,
        "created": added_on,
        "lastModified": raw.get("coverUpdatedOn") or added_on,
        "fileLastModified": added_on,
        "sizeBytes": raw.get("fileSizeKb", 0) * 1024,
        "size": f"{raw.get('fileSizeKb', 0) // 1024} MB",
        "media": {
            "status": "READY",
            "mediaType": media_type,
            "mediaProfile": "DIVINA",
            "pagesCount": 1,
            "comment": "",
            "epubDivinaCompatible": False,
            "epubIsKepub": False
        },
        "metadata": {
            "title": title,
            "titleLock": False,
            "summary": "",
            "summaryLock": False,
            "number": str(num),
            "numberLock": False,
            "numberSort": float(num),
            "numberSortLock": False,
            "releaseDate": raw.get("publishedDate"),
            "releaseDateLock": False,
            "authors": [{"name": a, "role": "writer"} for a in raw.get("authors", [])],
            "authorsLock": False,
            "tags": raw.get("tags", []),
            "tagsLock": False,
            "isbn": "",
            "isbnLock": False,
            "links": [],
            "linksLock": False,
            "created": added_on,
            "lastModified": added_on
        },
        "deleted": False,
        "fileHash": "",
        "oneshot": False
    }
    return ensure_book_dto(dto)
