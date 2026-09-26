import hashlib
from typing import Dict, Any, List, Optional


def compute_unique_series_id(lib_id: str, series_name: str) -> str:
    """Generate a collision-free deterministic series ID."""
    name_hash = hashlib.md5(series_name.encode("utf-8")).hexdigest()[:8]
    return f"{lib_id}-u-{name_hash}"


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


def ensure_series_dto(
    series: Dict[str, Any],
    user: Optional[str] = None,
    progress_map: Optional[Dict[str, Any]] = None,
    compute_read_counts: bool = True
) -> Dict[str, Any]:
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
    
    s_name = series.get("name")
    s_id = str(series.get("id", ""))
    if not s_name or str(s_name).strip().lower() in ("unknown series", "unknown"):
        # If there are books in DB for this series, pick the book's name
        try:
            from app.db import db
            s_books = db.get_books_by_series(s_id)
            if s_books:
                b_name = s_books[0].get("name") or (s_books[0].get("metadata") or {}).get("title")
                if b_name:
                    s_name = b_name
        except Exception:
            pass
        if not s_name:
            s_name = "Untitled Series"
        series["name"] = s_name
    else:
        series.setdefault("name", s_name)

    series.setdefault("url", f"/api/v1/series/{series.get('id', '')}")

    if s_id and compute_read_counts:
        try:
            from app.db import db
            s_books = db.get_books_by_series(s_id)
            if s_books:
                from app.grimmory_client import grimmory_client
                series["booksCount"] = len(s_books)
                u = (user or "default").lower().strip()
                u_map = progress_map if progress_map is not None else db.get_all_read_progress_map(u)
                read_cnt = sum(1 for b in s_books if grimmory_client._is_book_finished(b, user=user, progress=u_map.get(str(b.get("id")))))
                inp_cnt = sum(1 for b in s_books if grimmory_client._is_book_in_progress(b, user=user, progress=u_map.get(str(b.get("id")))))
                series["booksReadCount"] = read_cnt
                series["booksInProgressCount"] = inp_cnt
                series["booksUnreadCount"] = max(0, len(s_books) - read_cnt - inp_cnt)
        except Exception:
            pass

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


def ensure_book_dto(book: Dict[str, Any], user: Optional[str] = None) -> Dict[str, Any]:
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
    media = book.get("media") or {}
    m_type = str(media.get("mediaType", "")).lower()
    ext = "epub" if "epub" in m_type else "pdf" if "pdf" in m_type else "cbz"
    b_name = book.get("name") or f"book-{book.get('id', '')}"
    safe_name = "".join(c for c in b_name if c.isalnum() or c in (" ", "-", "_", ".", "[", "]", "(", ")")).strip() or f"book-{book.get('id', '')}"
    if not safe_name.lower().endswith(f".{ext}"):
        safe_name = f"{safe_name}.{ext}"
    lib_id = str(book.get("libraryId", "1"))
    book.setdefault("url", f"/books/{lib_id}/{safe_name}")

    # Fallback series info for standalone books (or books without a series)
    b_title = str(book.get("name") or (book.get("metadata") or {}).get("title") or f"Book {book.get('id', '0')}").strip()
    s_title = str(book.get("seriesTitle") or "").strip()
    if not s_title or s_title.lower() in ("unknown series", "unknown", "standalone", "series"):
        s_title = b_title
        book["seriesTitle"] = b_title

    s_id = str(book.get("seriesId", ""))
    if not s_id or "-unknown-series" in s_id:
        lib_id = str(book.get("libraryId", "0"))
        b_id = str(book.get("id", "0"))
        s_id = f"{lib_id}-standalone-{b_id}"
        book["seriesId"] = s_id
        book["oneshot"] = True

    # Disambiguate seriesId if seriesTitle has non-ASCII or seriesId has trailing dash
    lib_id = str(book.get("libraryId", "0"))
    if "-standalone-" not in s_id and s_title and (s_id.endswith("-") or any(ord(c) > 127 for c in s_title)):
        unique_id = compute_unique_series_id(lib_id, s_title)
        book["seriesId"] = unique_id
        from app.grimmory_client import grimmory_client
        if unique_id not in grimmory_client.custom_series:
            grimmory_client.register_custom_series(unique_id, lib_id, s_title, {
                "id": unique_id,
                "libraryId": lib_id,
                "name": s_title,
                "url": f"/api/v1/series/{unique_id}",
                "created": book.get("created", ""),
                "lastModified": book.get("lastModified", ""),
                "booksCount": 1,
                "oneshot": False
            })
    elif "-standalone-" in s_id:
        from app.grimmory_client import grimmory_client
        if s_id not in grimmory_client.custom_series:
            s_dto = {
                "id": s_id,
                "libraryId": lib_id,
                "name": s_title or b_title,
                "url": f"/api/v1/series/{s_id}",
                "created": book.get("created", ""),
                "lastModified": book.get("lastModified", ""),
                "booksCount": 1,
                "oneshot": True
            }
            grimmory_client.register_custom_series(s_id, lib_id, s_title or b_title, s_dto)
            try:
                from app.db import db
                db.save_series(s_dto)
            except Exception:
                pass

    # MediaDto
    media = book.get("media")
    if not isinstance(media, dict):
        media = {}
        book["media"] = media
    media.setdefault("status", "READY")
    media.setdefault("mediaType", "application/x-cbz")
    m_type = str(media.get("mediaType", "")).lower()
    if "epub" in m_type:
        media["mediaProfile"] = "EPUB"
    elif "pdf" in m_type:
        media["mediaProfile"] = "PDF"
    else:
        media.setdefault("mediaProfile", "DIVINA")

    b_id = str(book.get("id"))
    from app.grimmory_client import page_cache, page_count_cache
    if b_id in page_count_cache and page_count_cache[b_id] > 0:
        media["pagesCount"] = page_count_cache[b_id]
    elif b_id in page_cache and len(page_cache[b_id]) > 0:
        media["pagesCount"] = len(page_cache[b_id])
    elif "pagesCount" not in media or media["pagesCount"] is None or media["pagesCount"] <= 0:
        if "epub" in m_type:
            size_kb = (book.get("sizeBytes") or 0) // 1024
            usable_kb = max(5, size_kb - 60) if size_kb > 0 else 50
            media["pagesCount"] = max(1, int(usable_kb / 2.0))
        else:
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
    meta.setdefault("created", now_iso)
    meta.setdefault("lastModified", now_iso)

    if "readProgress" not in book or book.get("readProgress") is None:
        try:
            from app.grimmory_client import read_progress_cache
            from app.db import db
            u = (user or "default").lower().strip()
            p_key = f"{u}:{b_id}"
            prog = (
                read_progress_cache.get(p_key) or
                (read_progress_cache.get(b_id) if (u == "default" or ":" not in b_id and b_id in read_progress_cache) else None) or
                db.get_read_progress(u, b_id)
            )
            if prog:
                book["readProgress"] = prog
        except Exception:
            pass

    prog = book.get("readProgress")
    if isinstance(prog, dict) and prog.get("completed") is True:
        p_count = media.get("pagesCount", 1)
        if p_count > 1:
            prog["page"] = max(prog.get("page", 1), p_count)

    return book


def raw_app_book_to_dto(raw: Dict[str, Any], series_id_override: Optional[str] = None) -> Dict[str, Any]:
    """Convert Grimmory native /api/v1/app/books/* object into a compliant Komga BookDto."""
    b_id = str(raw["id"])
    lib_id = str(raw.get("libraryId", "1"))
    title = str(raw.get("title") or raw.get("name") or f"Book {b_id}").strip()

    raw_series = raw.get("seriesName")
    is_series_missing = (not raw_series) or not str(raw_series).strip() or str(raw_series).strip().lower() in ("unknown series", "unknown")

    if not is_series_missing:
        series_name = str(raw_series).strip()
        is_standalone = False
    else:
        series_name = title
        is_standalone = True

    if series_id_override:
        series_id = series_id_override
    elif is_standalone:
        series_id = f"{lib_id}-standalone-{b_id}"
    elif any(ord(c) > 127 for c in series_name):
        series_id = compute_unique_series_id(lib_id, series_name)
    else:
        series_id = f"{lib_id}-{series_name.lower().replace(' ', '-')}"
    num = raw.get("seriesNumber", 1.0)
    added_on = raw.get("addedOn") or "2026-09-23T00:00:00Z"

    file_type = raw.get("primaryFileType")
    media_type = "application/x-cbz" if file_type == "CBX" else "application/pdf" if file_type == "PDF" else "application/epub+zip"
    media_profile = "DIVINA" if file_type == "CBX" else "PDF" if file_type == "PDF" else "EPUB"

    # Check for embedded progress in raw object or cache
    page_prog = 1
    pct_prog = 0
    has_prog = False
    if "cbxProgress" in raw and isinstance(raw["cbxProgress"], dict):
        page_prog = raw["cbxProgress"].get("page", 1)
        pct_prog = raw["cbxProgress"].get("percentage", 0)
        has_prog = True
    elif "pdfProgress" in raw and isinstance(raw["pdfProgress"], dict):
        page_prog = raw["pdfProgress"].get("page", 1)
        pct_prog = raw["pdfProgress"].get("percentage", 0)
        has_prog = True
    elif "epubProgress" in raw and isinstance(raw["epubProgress"], dict):
        page_prog = raw["epubProgress"].get("page", 1)
        pct_prog = raw["epubProgress"].get("percentage", 0)
        has_prog = True

    date_fin = raw.get("dateFinished")
    is_comp = bool(
        date_fin or pct_prog == 100 or raw.get("completed") or raw.get("isRead") or
        raw.get("readStatus") == "READ" or raw.get("status") == "READ"
    )

    file_size_kb = raw.get("fileSizeKb", 0)
    pages_cnt = 1
    raw_pc = raw.get("pageCount") or raw.get("pagesCount") or raw.get("pages") or raw.get("numberOfPages")
    if isinstance(raw_pc, int) and raw_pc > 0:
        pages_cnt = raw_pc
    elif file_type == "EPUB":
        if pct_prog > 0 and page_prog > 0:
            pages_cnt = max(1, round(page_prog * 100 / pct_prog))
        elif file_size_kb > 0:
            usable_kb = max(5, file_size_kb - 60)
            pages_cnt = max(1, int(usable_kb / 2.0))

    if is_comp:
        pct_prog = 100
        page_prog = max(page_prog, pages_cnt)

    from app.grimmory_client import read_progress_cache
    cached_prog = read_progress_cache.get(b_id)
    read_prog = None
    if cached_prog:
        read_prog = cached_prog
        if is_comp:
            read_prog["completed"] = True
            if pages_cnt > 1:
                read_prog["page"] = max(read_prog.get("page", 1), pages_cnt)
    elif has_prog or is_comp:
        read_prog = {
            "page": page_prog,
            "completed": is_comp,
            "readDate": date_fin or raw.get("lastRead") or raw.get("coverUpdatedOn") or added_on,
            "created": added_on,
            "lastModified": added_on,
            "deviceId": "komic",
            "deviceName": "Komic"
        }

    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_", ".", "[", "]", "(", ")")).strip() or f"book-{b_id}"
    ext = "cbz" if file_type == "CBX" else "pdf" if file_type == "PDF" else "epub"
    if not safe_title.lower().endswith(f".{ext}"):
        safe_title = f"{safe_title}.{ext}"

    dto = {
        "id": b_id,
        "seriesId": series_id,
        "seriesTitle": series_name,
        "libraryId": lib_id,
        "name": title,
        "url": f"/books/{lib_id}/{safe_title}",
        "number": int(num) if isinstance(num, (int, float)) and num == int(num) else num,
        "created": added_on,
        "lastModified": raw.get("coverUpdatedOn") or added_on,
        "fileLastModified": added_on,
        "sizeBytes": raw.get("fileSizeKb", 0) * 1024,
        "size": f"{raw.get('fileSizeKb', 0) // 1024} MB",
        "readProgress": read_prog,
        "media": {
            "status": "READY",
            "mediaType": media_type,
            "mediaProfile": media_profile,
            "pagesCount": pages_cnt,
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
        "oneshot": is_standalone
    }

    try:
        from app.grimmory_client import grimmory_client
        if series_id not in grimmory_client.custom_series:
            s_dto = {
                "id": series_id,
                "libraryId": lib_id,
                "name": series_name,
                "url": f"/api/v1/series/{series_id}",
                "created": added_on,
                "lastModified": raw.get("coverUpdatedOn") or added_on,
                "booksCount": 1,
                "oneshot": is_standalone
            }
            grimmory_client.register_custom_series(series_id, lib_id, series_name, s_dto)
            from app.db import db
            db.save_series(s_dto)
    except Exception:
        pass

    return ensure_book_dto(dto)


def extract_search_filters(body: Any) -> Dict[str, Any]:
    """Recursively extract series_id, library_id, read_status, search from Komga search payloads."""
    filters: Dict[str, Any] = {}
    if not isinstance(body, (dict, list)):
        return filters

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = k.lower()
                if k_lower in ["seriesid", "seriesids", "series_id"]:
                    if isinstance(v, dict):
                        val = v.get("value")
                        vals = v.get("values")
                        if val is not None and "series_id" not in filters:
                            filters["series_id"] = str(val)
                        elif vals and isinstance(vals, list) and len(vals) > 0 and "series_id" not in filters:
                            filters["series_id"] = str(vals[0])
                    elif isinstance(v, list) and len(v) > 0 and "series_id" not in filters:
                        filters["series_id"] = str(v[0])
                    elif isinstance(v, (str, int)) and "series_id" not in filters:
                        filters["series_id"] = str(v)

                elif k_lower in ["libraryid", "libraryids", "library_id"]:
                    if isinstance(v, dict):
                        val = v.get("value")
                        vals = v.get("values")
                        if val is not None:
                            val_str = str(val).strip()
                            if val_str:
                                filters["library_id"] = val_str
                                filters["library_ids"] = [val_str]
                            else:
                                filters.pop("library_id", None)
                                filters["library_ids"] = []
                        elif vals and isinstance(vals, list):
                            clean_vals = [str(x) for x in vals if str(x).strip()]
                            if clean_vals:
                                filters["library_id"] = clean_vals[0]
                                filters["library_ids"] = clean_vals
                            else:
                                filters.pop("library_id", None)
                                filters["library_ids"] = []
                    elif isinstance(v, list):
                        clean_list = [str(x) for x in v if str(x).strip()]
                        if clean_list:
                            filters["library_id"] = clean_list[0]
                            filters["library_ids"] = clean_list
                        else:
                            filters.pop("library_id", None)
                            filters["library_ids"] = []
                    elif isinstance(v, (str, int)):
                        val_str = str(v).strip()
                        if val_str:
                            if "," in val_str:
                                split_ids = [x.strip() for x in val_str.split(",") if x.strip()]
                                if split_ids:
                                    filters["library_ids"] = split_ids
                                    filters["library_id"] = split_ids[0]
                                else:
                                    filters.pop("library_id", None)
                                    filters["library_ids"] = []
                            else:
                                filters["library_id"] = val_str
                                filters["library_ids"] = [val_str]
                        else:
                            filters.pop("library_id", None)
                            filters["library_ids"] = []

                elif k_lower in ["readstatus", "read_status"]:
                    if isinstance(v, dict):
                        val = v.get("value")
                        vals = v.get("values")
                        if val:
                            filters["read_status"] = [val] if isinstance(val, str) else list(val)
                        elif vals and isinstance(vals, list):
                            filters["read_status"] = vals
                    elif isinstance(v, list):
                        filters["read_status"] = v
                    elif isinstance(v, str):
                        filters["read_status"] = [v]

                elif k_lower in ["searchterm", "fulltextsearch", "search", "q", "query"]:
                    if isinstance(v, str) and v and "search" not in filters:
                        filters["search"] = v
                    elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], str) and "search" not in filters:
                        filters["search"] = v[0]
                elif k_lower in ["title", "name"]:
                    if isinstance(v, dict):
                        val = v.get("value")
                        if isinstance(val, str) and val and "search" not in filters:
                            filters["search"] = val
                    elif isinstance(v, str) and v and "search" not in filters:
                        filters["search"] = v

                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(body)
    return filters


def disambiguate_series_dto(s: Dict[str, Any]) -> str:
    """Ensure every series has a unique, deterministic ID, fixing Grimmory's non-ASCII clashing bug."""
    s_id = str(s.get("id", ""))
    lib_id = str(s.get("libraryId", "0"))
    s_name = s.get("name") or s.get("metadata", {}).get("title", "")

    has_non_ascii = any(ord(c) > 127 for c in s_name)
    has_trailing_dash = s_id.endswith("-")

    if has_trailing_dash or has_non_ascii:
        name_for_hash = s_name if s_name else s_id
        unique_id = compute_unique_series_id(lib_id, name_for_hash)
        s["id"] = unique_id
        s["url"] = f"/api/v1/series/{unique_id}"
        from app.grimmory_client import grimmory_client
        grimmory_client.register_custom_series(unique_id, lib_id, s_name, s)
        return unique_id

    return s_id



