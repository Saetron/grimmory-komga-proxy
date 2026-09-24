import base64
import pytest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.grimmory_client import grimmory_client, token_cache, page_cache, page_count_cache, read_progress_cache, book_cache, active_sessions
from app.db import db

client = TestClient(app)

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"testuser:testpass").decode()
}

@pytest.fixture(autouse=True)
def clear_caches():
    token_cache.clear()
    page_cache.clear()
    page_count_cache.clear()
    read_progress_cache.clear()
    book_cache.clear()
    active_sessions.clear()
    grimmory_client.custom_series.clear()
    grimmory_client.all_series_cache.clear()
    grimmory_client.custom_series_books_cache.clear()
    grimmory_client.user_libraries_cache.clear()
    db.clear_all()


# ============================================================================
# Offline Unit & Mock Tests (Zero outbound network requests, 100% safe for CI)
# ============================================================================

def test_actuator_info():
    resp = client.get("/actuator/info")
    assert resp.status_code == 200
    data = resp.json()
    assert "build" in data
    assert data["build"]["artifact"] == "komga"


def test_user_me_unauthorized():
    resp = client.get("/api/v2/users/me")
    assert resp.status_code == 401


def test_user_me_mock():
    mock_komga_resp = httpx.Response(
        200,
        json={"id": "user1", "email": "reader@example.com", "roles": ["USER"]}
    )
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_komga_resp
        resp = client.get("/api/v2/users/me", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "reader@example.com"
        assert "PAGE_STREAMING" in data["roles"]
        assert "FILE_DOWNLOAD" in data["roles"]


def test_libraries_mock():
    mock_komga_resp = httpx.Response(
        200,
        json=[{"id": "lib-1", "name": "Manga", "root": "/media/manga"}]
    )
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_komga_resp
        resp = client.get("/api/v1/libraries", headers=AUTH_HEADER)
        assert resp.status_code == 200
        libs = resp.json()
        assert isinstance(libs, list)
        assert len(libs) == 1
        assert libs[0]["name"] == "Manga"


def test_series_post_list_translation_mock():
    mock_komga_resp = httpx.Response(
        200,
        json={
            "content": [{"id": "s-1", "name": "Series 1", "booksCount": 10}],
            "totalElements": 1,
            "totalPages": 1
        }
    )
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_komga_resp
        resp = client.post(
            "/api/v1/series/list",
            json={"libraryIds": ["lib-1"], "search": "Series"},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 1
        assert data["content"][0]["name"] == "Series 1"
        assert mock_req.call_count >= 1
        _, kwargs = mock_req.call_args_list[0]
        assert kwargs.get("params", {}).get("library_id") == "lib-1"

        # Searching for non-matching term returns 0
        resp_nomatch = client.post(
            "/api/v1/series/list",
            json={"libraryIds": ["lib-1"], "search": "nonexistent"},
            headers=AUTH_HEADER
        )
        assert resp_nomatch.status_code == 200
        assert len(resp_nomatch.json()["content"]) == 0


def test_series_special_endpoints_mock():
    mock_komga_resp = httpx.Response(200, json={"content": [{"id": "s-1", "name": "Series 1"}]})
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_komga_resp
        for ep in ["/api/v1/series/latest", "/api/v1/series/new", "/api/v1/series/updated"]:
            resp = client.get(ep, headers=AUTH_HEADER)
            assert resp.status_code == 200

        # For list endpoints like alphabetical-groups and genres:
        mock_req.return_value = httpx.Response(200, json=[])
        for ep in ["/api/v1/series/alphabetical-groups", "/api/v1/series/genres"]:
            resp = client.get(ep, headers=AUTH_HEADER)
            assert resp.status_code == 200


def test_book_detail_and_pages_mock():
    book_raw = {
        "id": "book-100",
        "name": "Chapter 1",
        "media": {"status": "READY", "pagesCount": 0}
    }
    dims_data = [
        {"pageNumber": 1, "width": 1080, "height": 1920},
        {"pageNumber": 2, "width": 1080, "height": 1920},
    ]

    async def mock_get(url, **kwargs):
        if "page-dimensions" in url:
            return httpx.Response(200, json=dims_data)
        elif "page-info" in url:
            return httpx.Response(200, json=[])
        elif "progress" in url:
            return httpx.Response(200, json=None)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        
        mock_komga.return_value = httpx.Response(200, json=book_raw)

        # GET /api/v1/books/{id}
        resp = client.get("/api/v1/books/book-100", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "book-100"
        assert data["media"]["pagesCount"] == 2

        # GET /api/v1/books/{id}/pages
        resp_pages = client.get("/api/v1/books/book-100/pages", headers=AUTH_HEADER)
        assert resp_pages.status_code == 200
        pages = resp_pages.json()
        assert len(pages) == 2
        assert pages[0]["number"] == 1
        assert pages[0]["width"] == 1080
        assert pages[0]["height"] == 1920


def test_books_ondeck_mock():
    raw_reading = [
        {"id": 101, "name": "Book 101", "libraryId": 14},
        {"id": 102, "name": "Book 102", "libraryId": 19}
    ]
    book_dto = {
        "id": "101",
        "name": "Book 101",
        "media": {"pagesCount": 20},
        "readProgress": {"page": 2, "completed": False}
    }

    async def mock_get(url, **kwargs):
        if "continue-reading" in url:
            return httpx.Response(200, json=raw_reading)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_book_dto", new_callable=AsyncMock, return_value=book_dto), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        # Without filter
        resp = client.get("/api/v1/books/ondeck", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 2

        # With library_id=14 filter
        resp_lib = client.get("/api/v1/books/ondeck?library_id=14", headers=AUTH_HEADER)
        assert resp_lib.status_code == 200
        data_lib = resp_lib.json()
        assert len(data_lib["content"]) == 1
        assert data_lib["content"][0]["id"] == "101"



def test_read_progress_lifecycle_mock():
    async def mock_put(url, **kwargs):
        return httpx.Response(204)

    async def mock_post(url, **kwargs):
        return httpx.Response(204)

    async def mock_get(url, **kwargs):
        if "progress" in url:
            return httpx.Response(200, json={
                "cbxProgress": {"page": 5, "percentage": 50},
                "dateFinished": None
            })
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.put = AsyncMock(side_effect=mock_put)
    mock_client.post = AsyncMock(side_effect=mock_post)
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        
        # Test PATCH progress
        patch_resp = client.patch(
            "/api/v1/books/book-100/read-progress",
            json={"page": 5, "completed": False},
            headers=AUTH_HEADER
        )
        assert patch_resp.status_code == 204

        # Test GET progress
        get_resp = client.get("/api/v1/books/book-100/read-progress", headers=AUTH_HEADER)
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["page"] == 5
        assert data["completed"] is False

        # Test DELETE progress
        del_resp = client.delete("/api/v1/books/book-100/read-progress", headers=AUTH_HEADER)
        assert del_resp.status_code == 204


def test_book_manifest_mock():
    book_raw = {
        "id": "book-100",
        "name": "Chapter 1",
        "media": {"status": "READY", "pagesCount": 1}
    }
    dims_data = [{"pageNumber": 1, "width": 1080, "height": 1920}]

    async def mock_get(url, **kwargs):
        if "page-dimensions" in url:
            return httpx.Response(200, json=dims_data)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        mock_komga.return_value = httpx.Response(200, json=book_raw)
        resp = client.get("/api/v1/books/book-100/manifest/divina", headers=AUTH_HEADER)
        assert resp.status_code == 200
        manifest = resp.json()
        assert "readingOrder" in manifest
        assert len(manifest["readingOrder"]) == 1


def test_page_dto_and_series_dto_compliance():
    mock_series_raw = {
        "content": [{
            "id": "s-1",
            "name": "Series 1",
            "metadata": {
                "title": "Series 1",
                "status": "ONGOING"
            }
        }],
        "totalElements": 1,
        "totalPages": 1
    }
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga:
        mock_komga.return_value = httpx.Response(200, json=mock_series_raw)
        resp = client.get("/api/v1/series/new", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        
        # Verify Spring Data Pageable fields exist
        assert "pageable" in data
        assert "sort" in data
        assert "offset" in data["pageable"]
        assert "pageNumber" in data["pageable"]
        assert "pageSize" in data["pageable"]
        assert data["first"] is True
        assert data["last"] is True

        # Verify SeriesDto required fields
        s = data["content"][0]
        assert "metadata" in s
        assert "created" in s["metadata"]
        assert "lastModified" in s["metadata"]
        assert "alternateTitlesLock" in s["metadata"]
        assert "linksLock" in s["metadata"]
        assert "sharingLabelsLock" in s["metadata"]


def test_books_post_list_in_progress_routing():
    raw_reading = [{
        "id": 201,
        "title": "Reading Book",
        "primaryFileType": "CBX",
        "addedOn": "2026-09-23T12:00:00Z"
    }]
    async def mock_get(url, **kwargs):
        if "continue-reading" in url:
            return httpx.Response(200, json=raw_reading)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        # When Komic queries in-progress books
        resp = client.post(
            "/api/v1/books/list?sort=readProgress.readDate,desc",
            json={"readStatus": ["IN_PROGRESS"]},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "pageable" in data
        assert "content" in data
        assert len(data["content"]) == 1
        book = data["content"][0]
        assert book["id"] == "201"
        assert "media" in book
        assert "metadata" in book
        assert "comment" in book["media"]
        assert "isbn" in book["metadata"]


def test_books_post_list_series_filter_routing():
    mock_books = {
        "content": [{
            "id": "book-in-series-1",
            "name": "Chapter 1",
            "seriesId": "series-abc"
        }],
        "totalElements": 1,
        "totalPages": 1
    }
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga:
        mock_komga.return_value = httpx.Response(200, json=mock_books)

        # 1. Test POST /books/list with condition.seriesId using 'value' (Komga SearchOperatorIs)
        resp = client.post(
            "/api/v1/books/list?sort=metadata.numberSort,asc",
            json={"condition": {"seriesId": {"operator": "is", "value": "series-abc"}}},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 1
        assert data["content"][0]["id"] == "book-in-series-1"

        # Verify it routed specifically to /api/v1/series/series-abc/books
        first_call = mock_komga.call_args_list[0]
        assert first_call[0][1] == "/api/v1/series/series-abc/books"
        # Verify series_id was not duplicated in params
        assert "series_id" not in first_call[1].get("params", {})

        # 2. Test with 'values' array
        resp2 = client.post(
            "/api/v1/books/list?sort=metadata.numberSort,asc",
            json={"condition": {"seriesId": {"operator": "is", "values": ["series-abc"]}}},
            headers=AUTH_HEADER
        )
        assert resp2.status_code == 200


def test_series_post_list_library_id_normalization():
    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga:
        mock_komga.return_value = httpx.Response(200, json={"content": [], "totalElements": 0, "totalPages": 0})

        # Test POST /series/list with nested condition.libraryId using 'value'
        resp = client.post(
            "/api/v1/series/list",
            json={"condition": {"libraryId": {"operator": "is", "value": "lib-42"}}},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        call_params = mock_komga.call_args[1].get("params", {})
        assert call_params.get("library_id") == "lib-42"

        # Test with condition.allOf
        resp_allof = client.post(
            "/api/v1/series/list",
            json={"condition": {"allOf": [{"libraryId": {"operator": "is", "value": "lib-42"}}]}},
            headers=AUTH_HEADER
        )
        assert resp_allof.status_code == 200
        call_params_allof = mock_komga.call_args[1].get("params", {})
        assert call_params_allof.get("library_id") == "lib-42"

        # Test GET /series?libraryId=lib-42 query param normalization
        resp_get = client.get("/api/v1/series?libraryId=lib-42", headers=AUTH_HEADER)
        assert resp_get.status_code == 200
        call_params_get = mock_komga.call_args[1].get("params", {})
        assert call_params_get.get("library_id") == "lib-42"
        assert "libraryId" not in call_params_get



def test_auxiliary_metadata_endpoints():
    with patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):
        # Authors V2
        resp = client.get("/api/v2/authors?unpaged=true", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert "pageable" in data

        # Auxiliary metadata arrays
        for endpoint in ["genres", "tags", "publishers", "languages", "sharing-labels"]:
            r = client.get(f"/api/v1/{endpoint}", headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json() == []

        # Series collections and release dates
        r_dates = client.get("/api/v1/series/release-dates", headers=AUTH_HEADER)
        assert r_dates.status_code == 200
        assert r_dates.json() == []

        r_colls = client.get("/api/v1/series/s-1/collections", headers=AUTH_HEADER)
        assert r_colls.status_code == 200
        assert r_colls.json() == []


def test_progression_alias_endpoints():
    mock_client = AsyncMock()
    mock_client.put = AsyncMock(return_value=httpx.Response(204))
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json={
        "cbxProgress": {"page": 3, "percentage": 30},
        "dateFinished": None
    }))

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # PUT /books/{id}/progression
        r_put = client.put(
            "/api/v1/books/book-200/progression",
            json={"page": 3, "completed": False},
            headers=AUTH_HEADER
        )
        assert r_put.status_code == 204

        # GET /books/{id}/progression
        r_get = client.get("/api/v1/books/book-200/progression", headers=AUTH_HEADER)
        assert r_get.status_code == 200
        assert r_get.json()["page"] == 3


def test_series_disambiguation_non_ascii():
    from app.dto_utils import disambiguate_series_dto

    # Normal series ID remains unchanged and is NOT registered in custom_series
    normal_s = {"id": "16-chainsaw-man", "libraryId": "16", "name": "Chainsaw Man"}
    assert disambiguate_series_dto(normal_s) == "16-chainsaw-man"
    assert "16-chainsaw-man" not in grimmory_client.custom_series

    # Clashing non-ASCII series with empty slug ("16--")
    series_a = {"id": "16--", "libraryId": "16", "name": "たまや大玉"}
    series_b = {"id": "16--", "libraryId": "16", "name": "シタギスキマ"}

    id_a = disambiguate_series_dto(series_a)
    id_b = disambiguate_series_dto(series_b)

    assert id_a.startswith("16-u-")
    assert id_b.startswith("16-u-")
    assert id_a != id_b
    assert series_a["id"] == id_a
    assert series_b["id"] == id_b
    assert series_a["url"] == f"/api/v1/series/{id_a}"

    # Verify both are registered in custom_series
    assert id_a in grimmory_client.custom_series
    assert id_b in grimmory_client.custom_series
    assert grimmory_client.custom_series[id_a]["name"] == "たまや大玉"
    assert grimmory_client.custom_series[id_b]["name"] == "シタギスキマ"


def test_book_pages_count_normalization():
    from app.dto_utils import ensure_book_dto

    # pagesCount: 0 should be normalized to 1 to prevent division by zero in Swift/Kotlin
    b0 = {"id": "b0", "media": {"pagesCount": 0}}
    ensure_book_dto(b0)
    assert b0["media"]["pagesCount"] == 1

    # pagesCount: None should be normalized to 1
    b_none = {"id": "b1", "media": {"pagesCount": None}}
    ensure_book_dto(b_none)
    assert b_none["media"]["pagesCount"] == 1

    # pagesCount: negative should be normalized to 1
    b_neg = {"id": "b2", "media": {"pagesCount": -5}}
    ensure_book_dto(b_neg)
    assert b_neg["media"]["pagesCount"] == 1

    # pagesCount: 15 should remain 15
    b15 = {"id": "b3", "media": {"pagesCount": 15}}
    ensure_book_dto(b15)
    assert b15["media"]["pagesCount"] == 15


def test_disambiguated_series_integration_mock():
    # 1. Mock series list with clashing "16--" series
    mock_series_data = {
        "content": [
            {"id": "16--", "libraryId": "16", "name": "シタギスキマ", "booksCount": 1},
            {"id": "16--", "libraryId": "16", "name": "たまや大玉", "booksCount": 1}
        ],
        "totalElements": 2,
        "totalPages": 1
    }

    mock_app_books = {
        "content": [
            {
                "id": 359,
                "libraryId": 16,
                "seriesName": "シタギスキマ",
                "title": "シタギスキマ",
                "seriesNumber": 1.0,
                "addedOn": "2026-09-20T12:00:00Z",
                "primaryFileType": "CBX",
                "fileSizeKb": 10240
            }
        ]
    }

    async def mock_app_get(url, **kwargs):
        if "app/series" in url or "app/books" in url:
            return httpx.Response(200, json=mock_app_books)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_app_get)

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        mock_komga.return_value = httpx.Response(200, json=mock_series_data)

        # GET /api/v1/series
        resp = client.get("/api/v1/series?library_id=16", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 2
        id1 = data["content"][0]["id"]
        id2 = data["content"][1]["id"]
        assert id1.startswith("16-u-")
        assert id2.startswith("16-u-")
        assert id1 != id2

        # GET /api/v1/series/{id}
        resp_single = client.get(f"/api/v1/series/{id1}", headers=AUTH_HEADER)
        assert resp_single.status_code == 200
        assert resp_single.json()["id"] == id1

        # GET /api/v1/series/{id}/books
        resp_books = client.get(f"/api/v1/series/{id1}/books", headers=AUTH_HEADER)
        assert resp_books.status_code == 200
        books_data = resp_books.json()
        assert len(books_data["content"]) == 1
        assert books_data["content"][0]["id"] == "359"
        assert books_data["content"][0]["media"]["pagesCount"] >= 1

        # POST /api/v1/books/list with series_id filter
        resp_post_books = client.post(
            "/api/v1/books/list",
            json={"seriesId": [id1]},
            headers=AUTH_HEADER
        )
        assert resp_post_books.status_code == 200
        post_books_data = resp_post_books.json()
        assert len(post_books_data["content"]) == 1
        assert post_books_data["content"][0]["id"] == "359"

        # GET /api/v1/series/{id}/thumbnail (should fetch thumbnail of first book 359)
        mock_komga.return_value = httpx.Response(200, content=b"fake-image", headers={"Content-Type": "image/jpeg"})
        resp_thumb = client.get(f"/api/v1/series/{id1}/thumbnail", headers=AUTH_HEADER)
        assert resp_thumb.status_code == 200
        assert resp_thumb.content == b"fake-image"
        # Ensure it called /api/v1/books/359/thumbnail
        assert mock_komga.call_args[0][1] == "/api/v1/books/359/thumbnail"


def test_normal_series_books_routing():
    """Verify normal series queries Grimmory's /komga/api/v1/series/{id}/books directly and enriches page count."""
    mock_komga_resp = httpx.Response(200, json={
        "content": [{"id": "145", "name": "Book 145", "media": {"status": "READY", "pagesCount": 0}}],
        "totalElements": 1,
        "totalPages": 1
    })

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json=list(range(1, 69))))

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        mock_komga.return_value = mock_komga_resp

        # POST /api/v1/books/list for normal series
        resp = client.post(
            "/api/v1/books/list",
            json={"seriesId": ["17-blade-runner-2029"]},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 1
        assert data["content"][0]["id"] == "145"
        # Must be enriched from 0 to 68
        assert data["content"][0]["media"]["pagesCount"] == 68

        # Ensure it called Grimmory's /api/v1/series/{id}/books
        assert any(c[0][1] == "/api/v1/series/17-blade-runner-2029/books" for c in mock_komga.call_args_list)


def test_book_page_count_enrichment_and_caching():
    """Verify CBX page count resolution, PDF fallback, and TTL caching."""
    mock_series_books = {
        "content": [
            {"id": "cbz-book-1", "name": "CBZ Comic", "media": {"status": "READY", "pagesCount": 0}},
            {"id": "pdf-book-2", "name": "PDF Doc", "media": {"status": "READY", "pagesCount": 0}},
        ],
        "totalElements": 2,
        "totalPages": 1
    }

    call_count = {"cbx": 0, "pdf": 0}

    async def mock_native_get(url, **kwargs):
        if "/cbx/cbz-book-1/pages" in url:
            call_count["cbx"] += 1
            return httpx.Response(200, json=list(range(1, 115))) # 114 pages
        elif "/cbx/pdf-book-2/pages" in url:
            return httpx.Response(404)
        elif "/pdf/pdf-book-2/pages" in url:
            call_count["pdf"] += 1
            return httpx.Response(200, json=45) # 45 pages
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        mock_komga.return_value = httpx.Response(200, json=mock_series_books)

        resp = client.get("/api/v1/series/10-test-series/books", headers=AUTH_HEADER)
        assert resp.status_code == 200
        books = resp.json()["content"]
        assert len(books) == 2
        assert books[0]["media"]["pagesCount"] == 114
        assert books[1]["media"]["pagesCount"] == 45
        assert call_count["cbx"] == 1
        assert call_count["pdf"] == 1

        # Second call should hit page_count_cache without calling client.get again
        mock_komga.return_value = httpx.Response(200, json={
            "content": [
                {"id": "cbz-book-1", "name": "CBZ Comic", "media": {"status": "READY", "pagesCount": 0}},
            ],
            "totalElements": 1,
            "totalPages": 1
        })
        resp2 = client.get("/api/v1/series/10-test-series/books", headers=AUTH_HEADER)
        assert resp2.status_code == 200
        assert resp2.json()["content"][0]["media"]["pagesCount"] == 114
        # Cache hit: call count remains 1
        assert call_count["cbx"] == 1


def test_cbx_page_dimensions_sequential_indexing():
    """Verify that when page-dimensions has no pageNumber key, pages get sequential 1-based numbers."""
    dims_data = [
        {"width": 1080, "height": 1920, "isWide": False},
        {"width": 1080, "height": 1920, "isWide": False},
        {"width": 1080, "height": 1920, "isWide": False},
    ]

    async def mock_get(url, **kwargs):
        if "page-dimensions" in url:
            return httpx.Response(200, json=dims_data)
        elif "page-info" in url:
            return httpx.Response(200, json=[])
        elif "pages" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        resp = client.get("/api/v1/books/book-sequential/pages", headers=AUTH_HEADER)
        assert resp.status_code == 200
        pages = resp.json()
        assert len(pages) == 3
        # Must be sequential 1, 2, 3 (NOT all 1!)
        assert pages[0]["number"] == 1
        assert pages[1]["number"] == 2
        assert pages[2]["number"] == 3


def test_cbx_pages_list_indexing():
    """Verify that when /api/v1/cbx/{id}/pages returns [1, 2, 3, 4, 5], pages are correctly built."""
    async def mock_get(url, **kwargs):
        if "cbx" in url and url.endswith("/pages"):
            return httpx.Response(200, json=[1, 2, 3, 4, 5])
        elif "page-dimensions" in url:
            return httpx.Response(200, json=[])
        elif "page-info" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        resp = client.get("/api/v1/books/book-pages-list/pages", headers=AUTH_HEADER)
        assert resp.status_code == 200
        pages = resp.json()
        assert len(pages) == 5
        assert [p["number"] for p in pages] == [1, 2, 3, 4, 5]


def test_book_next_and_previous_in_series():
    """Verify GET /api/v1/books/{id}/next and /previous navigate within series."""
    series_books = [
        {"id": "book-vol-1", "seriesId": "series-123", "number": 1, "metadata": {"numberSort": 1.0}},
        {"id": "book-vol-2", "seriesId": "series-123", "number": 2, "metadata": {"numberSort": 2.0}},
        {"id": "book-vol-3", "seriesId": "series-123", "number": 3, "metadata": {"numberSort": 3.0}},
    ]

    async def mock_komga(method, path, user, pwd, **kwargs):
        if path == "/api/v1/books/book-vol-2":
            return httpx.Response(200, json=series_books[1])
        elif path == "/api/v1/books/book-vol-1":
            return httpx.Response(200, json=series_books[0])
        elif path == "/api/v1/books/book-vol-3":
            return httpx.Response(200, json=series_books[2])
        elif path == "/api/v1/series/series-123/books":
            return httpx.Response(200, json={"content": series_books, "totalElements": 3})
        elif "/next" in path or "/previous" in path:
            return httpx.Response(404) # Grimmory returns 404
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(404))

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga), \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. From book 2, next should be book 3
        resp_next = client.get("/api/v1/books/book-vol-2/next", headers=AUTH_HEADER)
        assert resp_next.status_code == 200
        assert resp_next.json()["id"] == "book-vol-3"

        # 2. From book 2, previous should be book 1
        resp_prev = client.get("/api/v1/books/book-vol-2/previous", headers=AUTH_HEADER)
        assert resp_prev.status_code == 200
        assert resp_prev.json()["id"] == "book-vol-1"

        # 3. From book 3 (last), next should be 404
        resp_last_next = client.get("/api/v1/books/book-vol-3/next", headers=AUTH_HEADER)
        assert resp_last_next.status_code == 404

        # 4. From book 1 (first), previous should be 404
        resp_first_prev = client.get("/api/v1/books/book-vol-1/previous", headers=AUTH_HEADER)
        assert resp_first_prev.status_code == 404


def test_read_sync_auto_completion_and_session_tracking():
    """Verify progress auto-completion on final page, percentage calc, and session logging."""
    page_count_cache["book-test-sync"] = 20

    put_payloads = []
    session_payloads = []

    async def mock_put(url, **kwargs):
        if "/progress" in url:
            put_payloads.append(kwargs.get("json"))
            return httpx.Response(200)
        return httpx.Response(404)

    async def mock_post(url, **kwargs):
        if "reading-sessions" in url:
            session_payloads.append(kwargs.get("json"))
            return httpx.Response(200)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.put = AsyncMock(side_effect=mock_put)
    mock_client.post = AsyncMock(side_effect=mock_post)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. Reading page 10 of 20: 50%, completed = False
        resp = client.patch(
            "/api/v1/books/book-test-sync/read-progress",
            json={"page": 10, "completed": False},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 204
        assert len(put_payloads) == 1
        assert put_payloads[0]["cbxProgress"]["percentage"] == 50
        assert put_payloads[0]["dateFinished"] is None

        # Cache check
        cached = read_progress_cache["book-test-sync"]
        assert cached["page"] == 10
        assert cached["completed"] is False

        # 2. Reading page 20 of 20: auto-completes to 100% and dateFinished set
        resp2 = client.patch(
            "/api/v1/books/book-test-sync/read-progress",
            json={"page": 20, "completed": False},
            headers=AUTH_HEADER
        )
        assert resp2.status_code == 204
        assert len(put_payloads) == 2
        assert put_payloads[1]["cbxProgress"]["percentage"] == 100
        assert put_payloads[1]["dateFinished"] is not None

        # Cache check
        cached2 = read_progress_cache["book-test-sync"]
        assert cached2["page"] == 20
        assert cached2["completed"] is True


def test_japanese_series_collision_disambiguation_and_navigation():
    """Verify series sharing Grimmory slug (e.g. 25-comic-) receive unique IDs, separate books, separate covers, and working next-book navigation."""
    from app.dto_utils import disambiguate_series_dto

    # 1. Verify disambiguate_series_dto creates unique deterministic IDs for Japanese titles with clashing slugs
    s_rakuten = {"id": "25-comic-", "libraryId": "25", "name": "COMIC快楽天", "booksCount": 2}
    s_europa = {"id": "25-comic-", "libraryId": "25", "name": "COMICエウロパ", "booksCount": 1}

    id_rakuten = disambiguate_series_dto(s_rakuten)
    id_europa = disambiguate_series_dto(s_europa)

    assert id_rakuten.startswith("25-u-")
    assert id_europa.startswith("25-u-")
    assert id_rakuten != id_europa
    assert s_rakuten["id"] == id_rakuten
    assert s_europa["id"] == id_europa

    # 2. Mock Grimmory backend responses
    mock_series_list = {
        "content": [
            {"id": "25-comic-", "libraryId": "25", "name": "COMIC快楽天", "booksCount": 2},
            {"id": "25-comic-", "libraryId": "25", "name": "COMICエウロパ", "booksCount": 1}
        ],
        "totalElements": 2,
        "totalPages": 1,
        "number": 0,
        "size": 20
    }

    rakuten_books_native = [
        {"id": 101, "title": "COMIC快楽天 2024年01月号", "seriesName": "COMIC快楽天", "seriesNumber": 1.0, "libraryId": 25},
        {"id": 102, "title": "COMIC快楽天 2024年02月号", "seriesName": "COMIC快楽天", "seriesNumber": 2.0, "libraryId": 25}
    ]
    europa_books_native = [
        {"id": 201, "title": "COMICエウロパ Vol.1", "seriesName": "COMICエウロパ", "seriesNumber": 1.0, "libraryId": 25}
    ]

    async def mock_komga_request(method, path, user, pwd, **kwargs):
        if path.startswith("/api/v1/series"):
            import copy
            return httpx.Response(200, json=copy.deepcopy(mock_series_list))
        if path == "/api/v1/books/101":
            return httpx.Response(200, json={"id": "101", "seriesId": "25-comic-", "seriesTitle": "COMIC快楽天", "libraryId": "25", "number": 1.0, "name": "COMIC快楽天 2024年01月号"})
        if path == "/api/v1/books/102":
            return httpx.Response(200, json={"id": "102", "seriesId": "25-comic-", "seriesTitle": "COMIC快楽天", "libraryId": "25", "number": 2.0, "name": "COMIC快楽天 2024年02月号"})
        if path == "/api/v1/books/201":
            return httpx.Response(200, json={"id": "201", "seriesId": "25-comic-", "seriesTitle": "COMICエウロパ", "libraryId": "25", "number": 1.0, "name": "COMICエウロパ Vol.1"})
        if path == "/api/v1/books/101/thumbnail":
            return httpx.Response(200, content=b"JPEG_COVER_RAKUTEN", headers={"Content-Type": "image/jpeg"})
        if path == "/api/v1/books/201/thumbnail":
            return httpx.Response(200, content=b"JPEG_COVER_EUROPA", headers={"Content-Type": "image/jpeg"})
        return httpx.Response(404)

    async def mock_native_get(url, **kwargs):
        if "/api/v1/app/series/" in url and "/books" in url:
            if "%E5%BF%AB%E6%A5%BD%E5%A4%A9" in url or "COMIC快楽天" in url:
                return httpx.Response(200, json=rakuten_books_native)
            if "%E3%82%A8%E3%82%A6%E3%83%AD%E3%83%91" in url or "COMICエウロパ" in url:
                return httpx.Response(200, json=europa_books_native)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga_request), \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 3. GET /api/v1/series returns disambiguated IDs
        r_series = client.get("/api/v1/series?library_id=25", headers=AUTH_HEADER)
        assert r_series.status_code == 200
        content = r_series.json()["content"]
        assert len(content) == 2
        r_rak = next(s for s in content if s["name"] == "COMIC快楽天")
        r_eur = next(s for s in content if s["name"] == "COMICエウロパ")
        assert r_rak["id"] == id_rakuten
        assert r_eur["id"] == id_europa

        # 4. Series thumbnails use respective first book covers
        t_rak = client.get(f"/api/v1/series/{id_rakuten}/thumbnail", headers=AUTH_HEADER)
        assert t_rak.status_code == 200
        assert t_rak.content == b"JPEG_COVER_RAKUTEN"

        t_eur = client.get(f"/api/v1/series/{id_europa}/thumbnail", headers=AUTH_HEADER)
        assert t_eur.status_code == 200
        assert t_eur.content == b"JPEG_COVER_EUROPA"

        # 5. Series books endpoint returns distinct books
        b_rak = client.get(f"/api/v1/series/{id_rakuten}/books", headers=AUTH_HEADER)
        assert b_rak.status_code == 200
        rak_books = b_rak.json()["content"]
        assert len(rak_books) == 2
        assert [b["id"] for b in rak_books] == ["101", "102"]
        assert all(b["seriesId"] == id_rakuten for b in rak_books)

        b_eur = client.get(f"/api/v1/series/{id_europa}/books", headers=AUTH_HEADER)
        assert b_eur.status_code == 200
        eur_books = b_eur.json()["content"]
        assert len(eur_books) == 1
        assert eur_books[0]["id"] == "201"
        assert eur_books[0]["seriesId"] == id_europa

        # 6. Next & Previous book navigation in disambiguated series
        next_resp = client.get("/api/v1/books/101/next", headers=AUTH_HEADER)
        assert next_resp.status_code == 200
        assert next_resp.json()["id"] == "102"

        prev_resp = client.get("/api/v1/books/102/previous", headers=AUTH_HEADER)
        assert prev_resp.status_code == 200
        assert prev_resp.json()["id"] == "101"

        no_next = client.get("/api/v1/books/102/next", headers=AUTH_HEADER)
        assert no_next.status_code == 404

        # 7. Self-healing: clear custom_series cache, simulate server restart, fetch series
        grimmory_client.custom_series.clear()
        grimmory_client.custom_series_books_cache.clear()
        assert id_rakuten not in grimmory_client.custom_series
        s_detail = client.get(f"/api/v1/series/{id_rakuten}", headers=AUTH_HEADER)
        assert s_detail.status_code == 200
        assert s_detail.json()["id"] == id_rakuten
        assert s_detail.json()["name"] == "COMIC快楽天"


def test_series_books_pagination_exceeding_20_books():
    """Verify that series with more than 20 books fetch all pages from Grimmory native API and support Komic pagination seamlessly."""
    from app.dto_utils import disambiguate_series_dto

    s_penguin = {"id": "25-comic-", "libraryId": "25", "name": "COMICペンギンクラブ", "booksCount": 45}
    unique_id = disambiguate_series_dto(s_penguin)
    assert unique_id.startswith("25-u-")

    # Clear custom_series_books_cache to start fresh
    grimmory_client.custom_series_books_cache.clear()

    # Create 45 books
    all_45_books = [
        {"id": 1000 + i, "title": f"COMICペンギンクラブ #{i+1}", "seriesName": "COMICペンギンクラブ", "seriesNumber": float(i+1), "libraryId": 25}
        for i in range(45)
    ]

    requested_pages = []

    async def mock_native_get(url, **kwargs):
        if "COMIC%E3%83%9A%E3%83%B3%E3%82%AE%E3%83%B3%E3%82%AF%E3%83%A9%E3%83%96" in url or "COMICペンギンクラブ" in url:
            params = kwargs.get("params", {})
            page = params.get("page", 0)
            requested_pages.append(page)
            size = 20  # simulate backend having pageSize=20
            start = page * size
            page_slice = all_45_books[start:start + size]
            return httpx.Response(200, json={
                "content": page_slice,
                "totalElements": 45,
                "totalPages": 3,
                "number": page,
                "size": size
            })
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. Komic requests page 0 (size 20)
        p0 = client.post(
            "/api/v1/books/list?page=0&size=20&sort=metadata.numberSort,asc",
            json={"series_id": unique_id},
            headers=AUTH_HEADER
        )
        assert p0.status_code == 200
        d0 = p0.json()
        assert d0["totalElements"] == 45
        assert d0["totalPages"] == 3
        assert len(d0["content"]) == 20
        assert d0["content"][0]["id"] == "1000"
        assert d0["content"][19]["id"] == "1019"

        # 2. Komic requests page 1 (size 20)
        p1 = client.post(
            "/api/v1/books/list?page=1&size=20&sort=metadata.numberSort,asc",
            json={"series_id": unique_id},
            headers=AUTH_HEADER
        )
        assert p1.status_code == 200
        d1 = p1.json()
        assert d1["totalElements"] == 45
        assert len(d1["content"]) == 20
        assert d1["content"][0]["id"] == "1020"
        assert d1["content"][19]["id"] == "1039"

        # 3. Komic requests page 2 (remaining 5 books)
        p2 = client.post(
            "/api/v1/books/list?page=2&size=20&sort=metadata.numberSort,asc",
            json={"series_id": unique_id},
            headers=AUTH_HEADER
        )
        assert p2.status_code == 200
        d2 = p2.json()
        assert d2["totalElements"] == 45
        assert len(d2["content"]) == 5
        assert d2["content"][0]["id"] == "1040"
        assert d2["content"][4]["id"] == "1044"

        # Verify all pages (0, 1, 2) were fetched from Grimmory native API
        assert 0 in requested_pages
        assert 1 in requested_pages
        assert 2 in requested_pages


def test_ondeck_read_progress_enrichment_and_cache_merge():
    """Verify that On Deck enriches books with readProgress and merges cached in-progress books."""
    from app.grimmory_client import read_progress_cache

    # Book 201 has no progress embedded in native continue-reading response
    raw_reading = [
        {"id": 201, "name": "Book 201", "libraryId": 14}
    ]

    # Prepopulate read_progress_cache with Book 301 (active in-progress) and Book 401 (completed)
    read_progress_cache["201"] = {"page": 3, "completed": False}
    read_progress_cache["301"] = {"page": 12, "completed": False}
    read_progress_cache["401"] = {"page": 100, "completed": True}

    async def mock_get_book_dto(b_id, user, pwd):
        return {
            "id": str(b_id),
            "name": f"Book {b_id}",
            "libraryId": "14",
            "media": {"pagesCount": 50},
        }

    async def mock_native_get(url, **kwargs):
        if "continue-reading" in url:
            return httpx.Response(200, json=raw_reading)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_book_dto", side_effect=mock_get_book_dto), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. GET /api/v1/books/ondeck
        resp = client.get("/api/v1/books/ondeck", headers=AUTH_HEADER)
        assert resp.status_code == 200
        content = resp.json()["content"]
        ids = [b["id"] for b in content]
        # Should include both 201 (from continue-reading) and 301 (from cache), but NOT 401 (completed)
        assert "201" in ids
        assert "301" in ids
        assert "401" not in ids

        for b in content:
            # Komga clients REQUIRE readProgress with page and completed=False
            assert "readProgress" in b
            assert b["readProgress"] is not None
            assert "page" in b["readProgress"]
            assert b["readProgress"]["completed"] is False

        # 2. GET /api/v1/books?read_status=IN_PROGRESS should route to on-deck
        resp_filter = client.get("/api/v1/books?read_status=IN_PROGRESS", headers=AUTH_HEADER)
        assert resp_filter.status_code == 200
        f_ids = [b["id"] for b in resp_filter.json()["content"]]
        assert "201" in f_ids
        assert "301" in f_ids

        # 3. POST /api/v1/books/list with sort=readProgress.readDate,desc should route to on-deck
        resp_post = client.post(
            "/api/v1/books/list?sort=readProgress.readDate,desc",
            json={"readStatus": ["IN_PROGRESS"]},
            headers=AUTH_HEADER
        )
        assert resp_post.status_code == 200
        p_ids = [b["id"] for b in resp_post.json()["content"]]
        assert "201" in p_ids
        assert "301" in p_ids


def test_recently_updated_series_chronological_ordering():
    """Verify that recently updated series are ordered chronologically by book activity, not alphabetically."""
    # Grimmory's /komga/api/v1/series returns series alphabetically: Series A, Series B, Series Z
    komga_series_resp = {
        "content": [
            {"id": "s-a", "name": "Series A", "metadata": {"title": "Series A", "status": "ONGOING"}},
            {"id": "s-b", "name": "Series B", "metadata": {"title": "Series B", "status": "ONGOING"}},
            {"id": "s-z", "name": "Series Z", "metadata": {"title": "Series Z", "status": "ONGOING"}},
        ],
        "totalElements": 3,
        "totalPages": 1
    }

    # Grimmory's /api/v1/app/books/recently-added has Series Z first, then Series A
    recently_added_books = [
        {"id": 901, "name": "Z vol 1", "seriesName": "Series Z", "libraryId": 14},
        {"id": 902, "name": "A vol 2", "seriesName": "Series A", "libraryId": 14},
    ]

    async def mock_native_get(url, **kwargs):
        if "recently-added" in url:
            return httpx.Response(200, json=recently_added_books)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        mock_komga.return_value = httpx.Response(200, json=komga_series_resp)

        # 1. GET /api/v1/series/updated
        resp = client.get("/api/v1/series/updated", headers=AUTH_HEADER)
        assert resp.status_code == 200
        content = resp.json()["content"]
        assert len(content) == 3
        # Should be ordered: Series Z (most recent book), Series A (next most recent), Series B (no recent activity)
        assert content[0]["name"] == "Series Z"
        assert content[1]["name"] == "Series A"
        assert content[2]["name"] == "Series B"

        # 2. POST /api/v1/series/list with sort=lastModified,desc
        resp_post = client.post(
            "/api/v1/series/list?sort=lastModified,desc",
            json={},
            headers=AUTH_HEADER
        )
        assert resp_post.status_code == 200
        content_post = resp_post.json()["content"]
        assert content_post[0]["name"] == "Series Z"
        assert content_post[1]["name"] == "Series A"
        assert content_post[2]["name"] == "Series B"

        # 3. Pagination verification: page 0 size 1
        resp_p0 = client.get("/api/v1/series/updated?page=0&size=1", headers=AUTH_HEADER)
        assert resp_p0.status_code == 200
        d_p0 = resp_p0.json()
        assert d_p0["totalElements"] == 3
        assert len(d_p0["content"]) == 1
        assert d_p0["content"][0]["name"] == "Series Z"


def test_series_search_filtering():
    """Verify that searching series filters correctly case-insensitively and returns only matching series."""
    mock_series_data = {
        "content": [
            {"id": "1", "name": "Nekopara Extra", "metadata": {"title": "Nekopara Extra", "status": "ONGOING"}},
            {"id": "2", "name": "Blade Runner 2029", "metadata": {"title": "Blade Runner 2029", "status": "ENDED"}},
            {"id": "3", "name": "Neko Maid Cafe", "metadata": {"title": "Neko Maid Cafe", "status": "ONGOING"}},
        ],
        "totalElements": 3,
        "totalPages": 1
    }

    grimmory_client.all_series_cache.clear()

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga:
        mock_komga.return_value = httpx.Response(200, json=mock_series_data)

        # 1. GET /api/v1/series?search=Neko (case-insensitive)
        resp1 = client.get("/api/v1/series?search=neko", headers=AUTH_HEADER)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["totalElements"] == 2
        names1 = [s["name"] for s in data1["content"]]
        assert "Nekopara Extra" in names1
        assert "Neko Maid Cafe" in names1
        assert "Blade Runner 2029" not in names1

        # 2. POST /api/v1/series/list with searchTerm="Blade"
        grimmory_client.all_series_cache.clear()
        resp2 = client.post(
            "/api/v1/series/list",
            json={"searchTerm": "Blade"},
            headers=AUTH_HEADER
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["totalElements"] == 1
        assert data2["content"][0]["name"] == "Blade Runner 2029"

        # 3. POST /api/v1/series/list with search that doesn't match
        resp3 = client.post(
            "/api/v1/series/list",
            json={"search": "NonExistentSeries"},
            headers=AUTH_HEADER
        )
        assert resp3.status_code == 200
        data3 = resp3.json()
        assert data3["totalElements"] == 0
        assert len(data3["content"]) == 0


def test_books_search_filtering():
    """Verify that searching books routes to search_books and returns matching BookDtos."""
    raw_search_books = [
        {"id": 8801, "title": "Nekopara Vol 1", "seriesName": "Nekopara", "libraryId": 14}
    ]

    async def mock_native_get(url, **kwargs):
        if "search" in url:
            return httpx.Response(200, json=raw_search_books)
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. GET /api/v1/books?search=Nekopara
        resp1 = client.get("/api/v1/books?search=Nekopara", headers=AUTH_HEADER)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["totalElements"] == 1
        assert data1["content"][0]["id"] == "8801"

        # 2. POST /api/v1/books/list with {"search": "Nekopara"}
        resp2 = client.post(
            "/api/v1/books/list",
            json={"search": "Nekopara"},
            headers=AUTH_HEADER
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["totalElements"] == 1
        assert data2["content"][0]["id"] == "8801"


def test_read_progress_persistence_and_ondeck():
    """Verify that read progress persists across cache reload and appears in On Deck on startup."""
    import tempfile
    import os
    from app.grimmory_client import read_progress_cache, save_progress_cache, load_progress_cache

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        temp_progress_file = f.name

    try:
        with patch("app.grimmory_client.PROGRESS_FILE", temp_progress_file):
            # 1. Populate progress and save
            read_progress_cache["777"] = {"page": 25, "completed": False, "readDate": "2026-09-24T10:00:00Z"}
            save_progress_cache()

            # 2. Clear in-memory cache and reload from disk
            read_progress_cache.clear()
            assert "777" not in read_progress_cache
            load_progress_cache()
            assert "777" in read_progress_cache
            assert read_progress_cache["777"]["page"] == 25
            assert read_progress_cache["777"]["completed"] is False

            # 3. Simulate bridge startup query for On Deck
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=httpx.Response(200, json=[]))

            async def mock_get_book(b_id, user, pwd):
                return {
                    "id": str(b_id),
                    "name": f"Book {b_id}",
                    "libraryId": "14",
                    "media": {"pagesCount": 100}
                }

            with patch.object(grimmory_client, "get_client", return_value=mock_client), \
                 patch.object(grimmory_client, "get_book_dto", side_effect=mock_get_book), \
                 patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

                resp = client.get("/api/v1/books/ondeck", headers=AUTH_HEADER)
                assert resp.status_code == 200
                content = resp.json()["content"]
                assert any(b["id"] == "777" for b in content)
                b777 = next(b for b in content if b["id"] == "777")
                assert b777["readProgress"]["page"] == 25
                assert b777["readProgress"]["completed"] is False
    finally:
        if os.path.exists(temp_progress_file):
            os.remove(temp_progress_file)


def test_sqlite_db_cache_and_persistence():
    """Verify SQLite database persists series, books, and read progress with fast lookups."""
    # 1. Test series saving and retrieval
    series_dto = {
        "id": "ser-42",
        "libraryId": "14",
        "name": "One Piece",
        "booksCount": 105,
        "metadata": {"title": "One Piece"}
    }
    db.save_series(series_dto)
    fetched_series = db.get_series("ser-42")
    assert fetched_series is not None
    assert fetched_series["id"] == "ser-42"
    assert fetched_series["name"] == "One Piece"

    all_series = db.get_all_series(library_id="14")
    assert any(s["id"] == "ser-42" for s in all_series)

    search_res = db.search_series("Piece")
    assert any(s["id"] == "ser-42" for s in search_res)

    # 2. Test books batch saving and retrieval
    books = [
        {"id": "b-1", "seriesId": "ser-42", "libraryId": "14", "name": "Chapter 1", "number": 1.0, "media": {"pagesCount": 55}},
        {"id": "b-2", "seriesId": "ser-42", "libraryId": "14", "name": "Chapter 2", "number": 2.0, "media": {"pagesCount": 48}}
    ]
    db.save_books_batch(books)

    b1 = db.get_book("b-1")
    assert b1 is not None
    assert b1["name"] == "Chapter 1"

    series_books = db.get_books_by_series("ser-42")
    assert len(series_books) == 2
    assert series_books[0]["id"] == "b-1"
    assert series_books[1]["id"] == "b-2"

    book_search = db.search_books("Chapter 2")
    assert any(b["id"] == "b-2" for b in book_search)

    # 3. Test read progress saving and in-progress retrieval
    prog_dto = {
        "page": 20,
        "completed": False,
        "readDate": "2026-09-24T10:00:00Z"
    }
    db.save_read_progress("b-1", 20, False, "2026-09-24T10:00:00Z", prog_dto)
    read_p = db.get_read_progress("b-1")
    assert read_p is not None
    assert read_p["page"] == 20
    assert read_p["completed"] is False

    in_prog = db.get_all_in_progress()
    assert any(b_id == "b-1" for b_id, _ in in_prog)

    # Test delete progress
    db.delete_read_progress("b-1")
    assert db.get_read_progress("b-1") is None


@pytest.mark.anyio
async def test_sqlite_db_pages_zero_network():
    """Verify that cached pages in SQLite return immediately without making any HTTP requests."""
    book_id = "book-cached-999"
    cached_pages = [
        {"number": 1, "fileName": "001.jpg", "mediaType": "image/jpeg", "width": 1200, "height": 1800, "sizeBytes": 0, "size": "0 B"},
        {"number": 2, "fileName": "002.jpg", "mediaType": "image/jpeg", "width": 1200, "height": 1800, "sizeBytes": 0, "size": "0 B"},
        {"number": 3, "fileName": "003.jpg", "mediaType": "image/jpeg", "width": 1200, "height": 1800, "sizeBytes": 0, "size": "0 B"}
    ]
    # Save into DB
    db.save_book_pages(book_id, cached_pages, len(cached_pages))

    # Mock client that raises error if any network call is attempted
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=RuntimeError("Network call should not occur when DB has cached pages!"))

    with patch.object(grimmory_client, "get_client", return_value=mock_client):
        # Call get_book_pages_metadata
        pages = await grimmory_client.get_book_pages_metadata(book_id, "user", "pass")
        assert len(pages) == 3
        assert pages[0]["width"] == 1200
        assert mock_client.get.call_count == 0

        # Call get_book_page_count
        count = await grimmory_client.get_book_page_count(book_id, "user", "pass")
        assert count == 3
        assert mock_client.get.call_count == 0


def test_sqlite_db_performance():
    """Verify that SQLite queries complete in sub-millisecond time (<1ms per operation)."""
    import time

    # Pre-populate database with 500 books
    books = [
        {"id": f"bench-b-{i}", "seriesId": "bench-s-1", "libraryId": "14", "name": f"Benchmark Chapter {i}", "number": float(i), "media": {"pagesCount": 20}}
        for i in range(500)
    ]
    db.save_books_batch(books)

    start = time.perf_counter()
    iterations = 500
    for i in range(iterations):
        b = db.get_book(f"bench-b-{i}")
        assert b is not None
    duration = time.perf_counter() - start
    avg_ms = (duration / iterations) * 1000.0

    # Ensure average read time is well under 1ms
    assert avg_ms < 1.0, f"Average query time {avg_ms:.3f}ms exceeded 1ms target"


def test_sqlite_db_unwritable_fallback():
    """Verify that Database initialization does not crash if preferred path is unwritable."""
    from app.db import Database
    fallback_db = Database(db_path="/proc/sys/nonexistent/forbidden.db")
    assert fallback_db.db_path != "/proc/sys/nonexistent/forbidden.db"
    assert "bridge.db" in fallback_db.db_path or "bridge_mem" in fallback_db.db_path

    # Verify write and read work on fallback
    fallback_db.save_series({"id": "fallback-1", "name": "Fallback Test"})
    retrieved = fallback_db.get_series("fallback-1")
    assert retrieved is not None
    assert retrieved["name"] == "Fallback Test"


@pytest.mark.anyio
async def test_sync_service_full_sync():
    """Verify that sync_service.run_full_sync validates series, books, and read progress into SQLite."""
    from app.sync_service import sync_service

    # Mock Grimmory responses
    mock_series_data = {
        "content": [
            {"id": "sync-s-1", "libraryId": "14", "name": "Sync Series 1", "booksCount": 2, "metadata": {"title": "Sync Series 1"}}
        ],
        "totalPages": 1
    }
    mock_books_data = {
        "content": [
            {"id": "sync-b-1", "seriesId": "sync-s-1", "libraryId": "14", "name": "Sync Book 1", "number": 1.0, "media": {"pagesCount": 10}},
            {"id": "sync-b-2", "seriesId": "sync-s-1", "libraryId": "14", "name": "Sync Book 2", "number": 2.0, "media": {"pagesCount": 15}}
        ]
    }
    mock_cr_data = [
        {"id": "sync-b-1", "cbxProgress": {"page": 5, "percentage": 50}, "dateFinished": None}
    ]

    async def mock_komga(method, path, user, pwd, **kwargs):
        if "/api/v1/libraries" in path:
            return httpx.Response(200, json=[{"id": "14", "name": "Manga"}])
        if "/api/v1/series/sync-s-1/books" in path:
            return httpx.Response(200, json=mock_books_data)
        if "/api/v1/series" in path:
            return httpx.Response(200, json=mock_series_data)
        return httpx.Response(404)

    mock_client = AsyncMock()
    async def mock_get(url, **kwargs):
        if "continue-reading" in url:
            return httpx.Response(200, json=mock_cr_data)
        if "progress" in url:
            return httpx.Response(200, json={"cbxProgress": {"page": 5, "percentage": 50}, "dateFinished": None})
        return httpx.Response(404)
    mock_client.get = AsyncMock(side_effect=mock_get)

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga), \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        stats = await sync_service.run_full_sync("testuser", "testpass")
        assert stats["status"] == "success"
        assert stats["seriesCount"] == 1
        assert stats["booksCount"] == 2
        assert stats["readProgressCount"] == 1

        # Verify series and books are stored in SQLite DB
        cached_series = db.get_series("sync-s-1")
        assert cached_series is not None
        assert cached_series["name"] == "Sync Series 1"

        cached_books = db.get_books_by_series("sync-s-1")
        assert len(cached_books) == 2
        assert cached_books[0]["id"] == "sync-b-1"
        assert cached_books[1]["id"] == "sync-b-2"

        # Verify read progress is stored in SQLite DB
        prog = db.get_read_progress("sync-b-1")
        assert prog is not None
        assert prog["page"] == 5


def test_sync_endpoint_manual_trigger():
    """Verify manual sync trigger endpoint POST /api/v1/sync returns 200 with sync statistics."""
    from app.sync_service import sync_service

    async def mock_run_sync(user=None, pwd=None):
        return {
            "status": "success",
            "seriesCount": 5,
            "booksCount": 42,
            "readProgressCount": 3,
            "durationSeconds": 1.23,
            "error": None
        }

    with patch.object(sync_service, "run_full_sync", side_effect=mock_run_sync):
        resp = client.post("/api/v1/sync", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["seriesCount"] == 5
        assert data["booksCount"] == 42


def test_ondeck_strictly_excludes_finished_books():
    """Verify that On Deck / Keep Reading never returns completed or finished books."""
    raw_continue_reading = [
        {"id": "b-active-1", "title": "Active Reading", "cbxProgress": {"page": 5, "percentage": 50}, "dateFinished": None},
        {"id": "b-fin-status", "title": "Finished by Status", "readStatus": "READ"},
        {"id": "b-fin-comp", "title": "Finished by Completed Flag", "completed": True},
        {"id": "b-fin-date", "title": "Finished by Date", "dateFinished": "2026-09-24T10:00:00Z"},
        {"id": "b-fin-pct", "title": "Finished by 100 Percent", "cbxProgress": {"page": 20, "percentage": 100}}
    ]

    mock_client = AsyncMock()
    async def mock_get(url, **kwargs):
        if "continue-reading" in url:
            return httpx.Response(200, json=raw_continue_reading)
        if "b-active-1/progress" in url:
            return httpx.Response(200, json={"cbxProgress": {"page": 5, "percentage": 50}, "dateFinished": None})
        return httpx.Response(404)
    mock_client.get = AsyncMock(side_effect=mock_get)

    async def mock_get_dto(b_id, user, pwd):
        item = next((b for b in raw_continue_reading if b["id"] == b_id), {"id": b_id, "title": f"Book {b_id}"})
        return {
            "id": b_id,
            "name": item.get("title", f"Book {b_id}"),
            "libraryId": "14",
            "media": {"pagesCount": 20},
            "readStatus": item.get("readStatus"),
            "completed": item.get("completed"),
            "dateFinished": item.get("dateFinished"),
            "cbxProgress": item.get("cbxProgress")
        }

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_book_dto", side_effect=mock_get_dto), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. Test GET /api/v1/books/ondeck
        resp = client.get("/api/v1/books/ondeck", headers=AUTH_HEADER)
        assert resp.status_code == 200
        content = resp.json()["content"]
        assert len(content) == 1
        assert content[0]["id"] == "b-active-1"
        assert content[0]["readProgress"]["completed"] is False

        # 2. Test POST /api/v1/books/list with readStatus=["IN_PROGRESS"]
        post_resp = client.post(
            "/api/v1/books/list",
            json={"readStatus": ["IN_PROGRESS"]},
            headers=AUTH_HEADER
        )
        assert post_resp.status_code == 200
        post_content = post_resp.json()["content"]
        assert len(post_content) == 1
        assert post_content[0]["id"] == "b-active-1"


def test_recently_released_books_only_with_release_date():
    """Verify that recently released books only includes books that have a non-empty releaseDate."""
    books = [
        {"id": "b-rel-old", "name": "Old Release", "metadata": {"releaseDate": "2026-01-15"}, "media": {"pagesCount": 10}},
        {"id": "b-rel-new", "name": "New Release", "metadata": {"releaseDate": "2026-09-20"}, "media": {"pagesCount": 10}},
        {"id": "b-no-rel-1", "name": "No Release Date 1", "metadata": {"releaseDate": None}, "media": {"pagesCount": 10}},
        {"id": "b-no-rel-2", "name": "No Release Date 2", "metadata": {}, "media": {"pagesCount": 10}}
    ]
    db.save_books_batch(books)

    # 1. Test GET /api/v1/books?sort=metadata.releaseDate,desc
    resp = client.get("/api/v1/books?sort=metadata.releaseDate,desc", headers=AUTH_HEADER)
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert len(content) == 2
    assert content[0]["id"] == "b-rel-new"
    assert content[1]["id"] == "b-rel-old"
    assert all(b.get("metadata", {}).get("releaseDate") is not None for b in content)

    # 2. Test POST /api/v1/books/list with sort: metadata.releaseDate,desc
    post_resp = client.post(
        "/api/v1/books/list",
        json={"sort": "metadata.releaseDate,desc"},
        headers=AUTH_HEADER
    )
    assert post_resp.status_code == 200
    post_content = post_resp.json()["content"]
    assert len(post_content) == 2
    assert post_content[0]["id"] == "b-rel-new"
    assert post_content[1]["id"] == "b-rel-old"

    # 3. Test GET /api/v1/books/released
    rel_resp = client.get("/api/v1/books/released", headers=AUTH_HEADER)
    assert rel_resp.status_code == 200
    assert len(rel_resp.json()["content"]) == 2


def test_epub_page_calculation_and_metadata():
    """Verify EPUB mediaProfile, synthetic page count from size, and xhtml page generation."""
    # 1. Test raw EPUB book without endpoint response calculates synthetic page count from size
    raw_epub = {
        "id": "epub-novel-1",
        "title": "A Great Light Novel",
        "primaryFileType": "EPUB",
        "fileSizeKb": 560,
        "libraryId": "lib-books",
        "addedOn": "2026-09-24T10:00:00Z"
    }

    mock_client = AsyncMock()
    # When probed for cbx/pdf/epub endpoints, return 404
    mock_client.get = AsyncMock(return_value=httpx.Response(404))

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        from app.dto_utils import raw_app_book_to_dto, ensure_book_dto
        dto = raw_app_book_to_dto(raw_epub)
        ensure_book_dto(dto)

        assert dto["media"]["mediaProfile"] == "EPUB"
        assert dto["media"]["mediaType"] == "application/epub+zip"
        # 560 KB -> (560 - 60) / 2 = 250 pages
        assert dto["media"]["pagesCount"] == 250

        # Save to DB and check pages metadata endpoint
        db.save_book(dto)
        resp = client.get("/api/v1/books/epub-novel-1/pages", headers=AUTH_HEADER)
        assert resp.status_code == 200
        pages = resp.json()
        assert len(pages) == 250
        assert pages[0]["mediaType"] == "application/xhtml+xml"
        assert pages[0]["fileName"].endswith(".xhtml")
        assert pages[-1]["number"] == 250


def test_read_and_unread_books_filtering():
    """Verify filtering by readStatus (READ, UNREAD, IN_PROGRESS) in GET, POST, and Series books."""
    books = [
        {
            "id": "b-read-1",
            "name": "Finished Book",
            "seriesId": "s-test",
            "media": {"pagesCount": 100, "mediaType": "application/x-cbz"},
            "readProgress": {"page": 100, "completed": True, "readDate": "2026-09-24T11:00:00Z"}
        },
        {
            "id": "b-in-progress-1",
            "name": "Reading Book",
            "seriesId": "s-test",
            "media": {"pagesCount": 100, "mediaType": "application/x-cbz"},
            "readProgress": {"page": 35, "completed": False, "readDate": "2026-09-24T11:30:00Z"}
        },
        {
            "id": "b-unread-1",
            "name": "Unread Book",
            "seriesId": "s-test",
            "media": {"pagesCount": 100, "mediaType": "application/x-cbz"},
            "readProgress": None
        }
    ]
    db.save_books_batch(books)

    # 1. POST /api/v1/books/list with readStatus=["READ"]
    resp_read = client.post("/api/v1/books/list", json={"readStatus": ["READ"]}, headers=AUTH_HEADER)
    assert resp_read.status_code == 200
    content_read = resp_read.json()["content"]
    assert len(content_read) == 1
    assert content_read[0]["id"] == "b-read-1"

    # 2. POST /api/v1/books/list with readStatus=["UNREAD"]
    resp_unread = client.post("/api/v1/books/list", json={"readStatus": ["UNREAD"]}, headers=AUTH_HEADER)
    assert resp_unread.status_code == 200
    content_unread = resp_unread.json()["content"]
    assert len(content_unread) == 1
    assert content_unread[0]["id"] == "b-unread-1"

    # 3. GET /api/v1/books?read_status=READ
    get_read = client.get("/api/v1/books?read_status=READ", headers=AUTH_HEADER)
    assert get_read.status_code == 200
    assert len(get_read.json()["content"]) == 1
    assert get_read.json()["content"][0]["id"] == "b-read-1"

    # 4. GET /api/v1/books?read_status=UNREAD
    get_unread = client.get("/api/v1/books?read_status=UNREAD", headers=AUTH_HEADER)
    assert get_unread.status_code == 200
    assert len(get_unread.json()["content"]) == 1
    assert get_unread.json()["content"][0]["id"] == "b-unread-1"

    # 5. GET /api/v1/series/s-test/books?read_status=UNREAD
    series_unread = client.get("/api/v1/series/s-test/books?read_status=UNREAD", headers=AUTH_HEADER)
    assert series_unread.status_code == 200
    assert len(series_unread.json()["content"]) == 1
    assert series_unread.json()["content"][0]["id"] == "b-unread-1"


def test_read_book_progress_100_percent():
    """Verify that books marked as read in Grimmory have 100% progress and page == pagesCount."""
    raw_read = {
        "id": "book-read-100",
        "title": "Completed Manga",
        "primaryFileType": "CBX",
        "fileSizeKb": 1024,
        "readStatus": "READ",
        "dateFinished": "2026-09-24T12:00:00Z"
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json={
        "readStatus": "READ",
        "dateFinished": "2026-09-24T12:00:00Z",
        "cbxProgress": {"page": 1, "percentage": 100}
    }))

    from app.dto_utils import raw_app_book_to_dto, ensure_book_dto
    dto = raw_app_book_to_dto(raw_read)
    dto["media"]["pagesCount"] = 180
    ensure_book_dto(dto)

    assert dto["readProgress"] is not None
    assert dto["readProgress"]["completed"] is True
    # Progress page must equal pagesCount so Komic shows 100%
    assert dto["readProgress"]["page"] == 180


def test_book_file_download_with_fallback_and_proper_extensions():
    """Verify downloading book file falls back to Grimmory native endpoints and gives correct filename extension."""
    epub_book = {
        "id": "epub-dl-1",
        "name": "Overlord Volume 1",
        "media": {"mediaType": "application/epub+zip", "pagesCount": 350}
    }
    db.save_book(epub_book)

    fake_epub_bytes = b"PK\x03\x04fakepubcontent"

    async def mock_native_get(url, **kwargs):
        if "/app/books/epub-dl-1/file" in url:
            return httpx.Response(200, content=fake_epub_bytes, headers={"Content-Type": "application/epub+zip"})
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_native_get)

    with patch.object(grimmory_client, "komga_request", new_callable=AsyncMock) as mock_komga, \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # Grimmory Komga endpoint returns 404 for /books/{id}/file
        mock_komga.return_value = httpx.Response(404)

        resp = client.get("/api/v1/books/epub-dl-1/file", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.content == fake_epub_bytes
        assert resp.headers["Content-Type"] == "application/epub+zip"
        assert "Overlord Volume 1.epub" in resp.headers["Content-Disposition"]


def test_user_library_access_cached_books():
    """Verify that cached books from libraries the user has no access to are forbidden."""
    db.save_book({
        "id": "book-allowed",
        "name": "Allowed Book",
        "libraryId": "lib-allowed",
        "media": {"pagesCount": 10}
    })
    db.save_book({
        "id": "book-forbidden",
        "name": "Forbidden Book",
        "libraryId": "lib-secret",
        "media": {"pagesCount": 10}
    })

    async def mock_komga_req(method, path, user, pwd, **kwargs):
        if path == "/api/v1/libraries":
            return httpx.Response(200, json=[{"id": "lib-allowed", "name": "Allowed"}])
        return httpx.Response(404)

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga_req):
        # 1. Allowed book should succeed
        resp_allowed = client.get("/api/v1/books/book-allowed", headers=AUTH_HEADER)
        assert resp_allowed.status_code == 200
        assert resp_allowed.json()["id"] == "book-allowed"

        # 2. Forbidden book should return 404
        resp_forbidden = client.get("/api/v1/books/book-forbidden", headers=AUTH_HEADER)
        assert resp_forbidden.status_code == 404

        # 3. Read status query should only return books from lib-allowed
        resp_unread = client.get("/api/v1/books?read_status=UNREAD", headers=AUTH_HEADER)
        assert resp_unread.status_code == 200
        content = resp_unread.json()["content"]
        book_ids = [b["id"] for b in content]
        assert "book-allowed" in book_ids
        assert "book-forbidden" not in book_ids


@pytest.mark.anyio
async def test_sync_service_skips_when_no_credentials():
    """Verify background sync pauses gracefully without spamming 401s if no credentials exist."""
    from app.sync_service import sync_service
    from app.config import settings

    old_user = settings.DEFAULT_USERNAME
    old_pwd = settings.DEFAULT_PASSWORD
    old_last = grimmory_client.last_credentials
    try:
        settings.DEFAULT_USERNAME = ""
        settings.DEFAULT_PASSWORD = ""
        grimmory_client.last_credentials = None

        stats = await sync_service.run_full_sync()
        assert stats["status"] == "skipped"
        assert "No Grimmory credentials configured" in stats["message"]
    finally:
        settings.DEFAULT_USERNAME = old_user
        settings.DEFAULT_PASSWORD = old_pwd
        grimmory_client.last_credentials = old_last


@pytest.mark.anyio
async def test_sync_service_unauthorized_detected():
    """Verify background sync catches 401 and reports unauthorized cleanly."""
    from app.sync_service import sync_service

    async def mock_komga_401(method, path, user, pwd, **kwargs):
        return httpx.Response(401, json={"message": "Unauthorized"})

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga_401):
        stats = await sync_service.run_full_sync("wronguser", "wrongpass")
        assert stats["status"] == "unauthorized"
        assert "401" in stats["error"]


def test_user_mapping_and_aliases():
    """Verify USER_MAPPING parses various formats and maps client username to Grimmory credentials."""
    from app.config import parse_user_mappings, settings

    # Test comma-separated mapping
    m1 = parse_user_mappings("unraid_user:grimmory_admin:secret123,reader:grim_reader:pass456")
    assert m1["unraid_user"] == ("grimmory_admin", "secret123")
    assert m1["reader"] == ("grim_reader", "pass456")

    # Test equal-sign mapping
    m2 = parse_user_mappings("unraid_user=grimmory_admin:secret123")
    assert m2["unraid_user"] == ("grimmory_admin", "secret123")

    # Test JSON mapping
    m3 = parse_user_mappings('{"komic": ["grimmory_user", "mypass"]}')
    assert m3["komic"] == ("grimmory_user", "mypass")

    # Test client extraction with user mapping
    settings.USER_MAPPINGS = {"mapped_client": ("real_grimmory_user", "real_grimmory_pwd")}
    auth_header = "Basic " + base64.b64encode(b"mapped_client:anyclientpass").decode()
    u, p = grimmory_client.extract_credentials(auth_header)
    assert u == "real_grimmory_user"
    assert p == "real_grimmory_pwd"


def test_snappy_performance_zero_network_when_cached():
    """Verify that browsing series, library, and series books uses SQLite cache with zero network requests."""
    # Seed SQLite DB
    db.save_series({
        "id": "s-snappy-1",
        "libraryId": "lib-snappy",
        "name": "Snappy Manga",
        "booksCount": 2,
        "metadata": {"title": "Snappy Manga", "titleSort": "Snappy Manga"}
    })
    db.save_books_batch([
        {"id": "b-snappy-1", "seriesId": "s-snappy-1", "libraryId": "lib-snappy", "name": "Chapter 1", "number": 1.0, "media": {"pagesCount": 25}},
        {"id": "b-snappy-2", "seriesId": "s-snappy-1", "libraryId": "lib-snappy", "name": "Chapter 2", "number": 2.0, "media": {"pagesCount": 30}}
    ])

    # If any network call is made to Grimmory, fail immediately!
    mock_komga = AsyncMock(side_effect=RuntimeError("Network request should NOT occur when data is cached in SQLite!"))
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=RuntimeError("Client GET should NOT occur when data is cached in SQLite!"))

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga), \
         patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_user_library_ids", new_callable=AsyncMock, return_value={"lib-snappy"}):

        # 1. GET /api/v1/series?library_id=lib-snappy
        resp_series = client.get("/api/v1/series?library_id=lib-snappy", headers=AUTH_HEADER)
        assert resp_series.status_code == 200
        assert len(resp_series.json()["content"]) == 1
        assert resp_series.json()["content"][0]["id"] == "s-snappy-1"

        # 2. POST /api/v1/series/list
        resp_series_post = client.post("/api/v1/series/list", json={"library_id": "lib-snappy"}, headers=AUTH_HEADER)
        assert resp_series_post.status_code == 200
        assert len(resp_series_post.json()["content"]) == 1
        assert resp_series_post.json()["content"][0]["id"] == "s-snappy-1"

        # 3. GET /api/v1/series/s-snappy-1
        resp_single_series = client.get("/api/v1/series/s-snappy-1", headers=AUTH_HEADER)
        assert resp_single_series.status_code == 200
        assert resp_single_series.json()["name"] == "Snappy Manga"

        # 4. GET /api/v1/series/s-snappy-1/books
        resp_books = client.get("/api/v1/series/s-snappy-1/books", headers=AUTH_HEADER)
        assert resp_books.status_code == 200
        assert len(resp_books.json()["content"]) == 2
        assert resp_books.json()["content"][0]["id"] == "b-snappy-1"

        # 5. POST /api/v1/books/list with series_id
        resp_books_post = client.post("/api/v1/books/list", json={"series_id": "s-snappy-1"}, headers=AUTH_HEADER)
        assert resp_books_post.status_code == 200
        assert len(resp_books_post.json()["content"]) == 2

        # 6. GET /api/v1/books?library_id=lib-snappy
        resp_all_books = client.get("/api/v1/books?library_id=lib-snappy", headers=AUTH_HEADER)
        assert resp_all_books.status_code == 200
        assert len(resp_all_books.json()["content"]) == 2


@pytest.mark.anyio
async def test_continue_reading_and_ondeck_never_show_finished_books():
    """Verify that books with 100% progress or readStatus='READ' never appear in Weiterlesen or Als nächstes lesen."""
    from app.grimmory_client import read_progress_cache

    # Series 1: Lena (1 book, 100% READ)
    book_finished = {
        "id": "1001",
        "seriesId": "s-lena",
        "seriesTitle": "Lena",
        "libraryId": "14",
        "name": "Lena 1",
        "number": 1,
        "readStatus": "READ",
        "readProgress": {"page": 50, "completed": True, "readDate": "2026-09-24T12:00:00Z"},
        "media": {"pagesCount": 50},
        "metadata": {"numberSort": 1.0}
    }
    # Series 2: Manga (Book 1 READ, Book 2 IN_PROGRESS at 10%, Book 3 UNREAD)
    book_manga_1 = {
        "id": "2001",
        "seriesId": "s-manga",
        "seriesTitle": "Manga",
        "libraryId": "14",
        "name": "Manga Vol 1",
        "number": 1,
        "readStatus": "READ",
        "readProgress": {"page": 100, "completed": True, "readDate": "2026-09-24T11:00:00Z"},
        "media": {"pagesCount": 100},
        "metadata": {"numberSort": 1.0}
    }
    book_manga_2 = {
        "id": "2002",
        "seriesId": "s-manga",
        "seriesTitle": "Manga",
        "libraryId": "14",
        "name": "Manga Vol 2",
        "number": 2,
        "readStatus": "READING",
        "readProgress": {"page": 15, "completed": False, "readDate": "2026-09-24T13:00:00Z"},
        "media": {"pagesCount": 100},
        "metadata": {"numberSort": 2.0}
    }
    book_manga_3 = {
        "id": "2003",
        "seriesId": "s-manga",
        "seriesTitle": "Manga",
        "libraryId": "14",
        "name": "Manga Vol 3",
        "number": 3,
        "media": {"pagesCount": 100},
        "metadata": {"numberSort": 3.0}
    }

    # Save to SQLite
    db.save_books_batch([book_finished, book_manga_1, book_manga_2, book_manga_3])
    db.save_read_progress("1001", 50, True, "2026-09-24T12:00:00Z", book_finished["readProgress"])
    db.save_read_progress("2001", 100, True, "2026-09-24T11:00:00Z", book_manga_1["readProgress"])
    db.save_read_progress("2002", 15, False, "2026-09-24T13:00:00Z", book_manga_2["readProgress"])
    read_progress_cache["1001"] = book_finished["readProgress"]
    read_progress_cache["2001"] = book_manga_1["readProgress"]
    read_progress_cache["2002"] = book_manga_2["readProgress"]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json=[]))

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        # 1. Weiterlesen (Continue reading): GET /api/v1/books?read_status=IN_PROGRESS
        resp_cr = client.get("/api/v1/books?read_status=IN_PROGRESS", headers=AUTH_HEADER)
        assert resp_cr.status_code == 200
        cr_content = resp_cr.json()["content"]
        cr_ids = [b["id"] for b in cr_content]
        # Must contain book 2002 (in progress), and NEVER finished books 1001 or 2001
        assert "2002" in cr_ids
        assert "1001" not in cr_ids
        assert "2001" not in cr_ids
        assert "2003" not in cr_ids

        # 2. Als nächstes lesen (On Deck): GET /api/v1/books/ondeck
        resp_od = client.get("/api/v1/books/ondeck", headers=AUTH_HEADER)
        assert resp_od.status_code == 200
        od_content = resp_od.json()["content"]
        od_ids = [b["id"] for b in od_content]
        # Must contain 2002 (active in-progress book of s-manga)
        # Lena series (s-lena) is completely finished, so 1001 must NEVER appear!
        assert "1001" not in od_ids
        assert "2001" not in od_ids


@pytest.mark.anyio
async def test_reconcile_read_progress_heals_stale_cache():
    """Verify that reconcile_read_progress heals stale 'completed=0' SQLite records with Grimmory."""
    from app.grimmory_client import read_progress_cache

    # Simulate stale record in DB where completed = 0, but Grimmory has readStatus = READ
    stale_dto = {"page": 1, "completed": False, "readDate": "2026-09-24T09:00:00Z"}
    db.save_read_progress("stale-101", 1, False, "2026-09-24T09:00:00Z", stale_dto)
    read_progress_cache["stale-101"] = stale_dto

    # Grimmory returns readStatus: 'READ'
    async def mock_grimmory_progress(url, **kwargs):
        if "/api/v1/app/books/stale-101/progress" in url:
            return httpx.Response(200, json={"readStatus": "READ", "lastReadTime": "2026-09-24T18:00:00Z"})
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=mock_grimmory_progress)

    with patch.object(grimmory_client, "get_client", return_value=mock_client), \
         patch.object(grimmory_client, "get_native_token", new_callable=AsyncMock, return_value="dummy-token"):

        await grimmory_client.reconcile_read_progress("test_user", "test_pwd")

        # After reconciliation, DB must have completed = 1!
        cached_p = db.get_read_progress("stale-101")
        assert cached_p is not None
        assert cached_p["completed"] is True
        assert read_progress_cache["stale-101"]["completed"] is True

        # And it should no longer be returned by get_all_in_progress()!
        in_prog = db.get_all_in_progress()
        assert not any(b_id == "stale-101" for b_id, _ in in_prog)


def test_series_less_japanese_book_creates_eponymous_series():
    """Verify that books without a series (including Japanese titles) create an eponymous series with book name, never 'Unknown Series'."""
    from app.dto_utils import raw_app_book_to_dto
    japanese_title = "進撃の巨人 特典小冊子"
    raw_book = {
        "id": 9999,
        "libraryId": 14,
        "title": japanese_title,
        "seriesName": None,
        "addedOn": "2026-09-24T12:00:00Z"
    }
    dto = raw_app_book_to_dto(raw_book)
    assert dto["seriesTitle"] == japanese_title
    assert "Unknown Series" not in dto["seriesTitle"]
    assert "unknown" not in dto["seriesId"]
    assert dto["seriesId"] == "14-standalone-9999"
    assert dto["oneshot"] is True

    s_dto = grimmory_client.custom_series.get("14-standalone-9999")
    assert s_dto is not None
    assert s_dto["name"] == japanese_title
    assert s_dto["dto"]["name"] == japanese_title


@pytest.mark.anyio
async def test_multi_user_read_progress_isolation():
    """Verify that read progress and read/unread filters are strictly PER USER."""
    book_id = "isolation-book-1"
    book_dto = {
        "id": book_id,
        "name": "Shared Manga Vol 1",
        "libraryId": "14",
        "seriesId": "series-iso",
        "seriesTitle": "Shared Manga",
        "media": {"pagesCount": 100}
    }
    db.save_book(book_dto)

    user1 = "alice"
    user2 = "bob"

    # Alice reads to page 25 (in progress)
    await grimmory_client.update_read_progress(book_id, page=25, completed=False, user=user1, pwd="p1")

    # Bob completes the book (100% finished)
    await grimmory_client.update_read_progress(book_id, page=100, completed=True, user=user2, pwd="p2")

    # Verify SQLite DB isolation
    p1 = db.get_read_progress(user1, book_id)
    p2 = db.get_read_progress(user2, book_id)
    assert p1 is not None and p1["page"] == 25 and p1["completed"] is False
    assert p2 is not None and p2["page"] == 100 and p2["completed"] is True

    # Verify status checks per user
    assert grimmory_client._is_book_in_progress(book_dto, user=user1) is True
    assert grimmory_client._is_book_finished(book_dto, user=user1) is False

    assert grimmory_client._is_book_in_progress(book_dto, user=user2) is False
    assert grimmory_client._is_book_finished(book_dto, user=user2) is True

    # Verify read status filtering per user
    read_for_bob = await grimmory_client.get_books_by_read_status(statuses=["READ"], user=user2, pwd="p2")
    assert any(b["id"] == book_id for b in read_for_bob["content"])

    read_for_alice = await grimmory_client.get_books_by_read_status(statuses=["READ"], user=user1, pwd="p1")
    assert not any(b["id"] == book_id for b in read_for_alice["content"])

    inp_for_alice = await grimmory_client.get_books_by_read_status(statuses=["IN_PROGRESS"], user=user1, pwd="p1")
    assert any(b["id"] == book_id for b in inp_for_alice["content"])

    inp_for_bob = await grimmory_client.get_books_by_read_status(statuses=["IN_PROGRESS"], user=user2, pwd="p2")
    assert not any(b["id"] == book_id for b in inp_for_bob["content"])


def test_thumbnail_caching_and_timeout_resilience():
    """Verify that thumbnails are cached with immutable headers and timeouts gracefully return 404."""
    from app.grimmory_client import thumbnail_cache
    thumbnail_cache.clear()

    dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    call_count = 0

    async def mock_komga_request(method, path, user, pwd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "timeout-book" in path:
            raise httpx.ConnectTimeout("Connection timed out")
        return httpx.Response(200, content=dummy_jpeg, headers={"Content-Type": "image/jpeg"})

    with patch.object(grimmory_client, "komga_request", side_effect=mock_komga_request):
        # 1. First fetch for book-thumb-1 -> calls Grimmory and caches
        resp1 = client.get("/api/v1/books/book-thumb-1/thumbnail", headers=AUTH_HEADER)
        assert resp1.status_code == 200
        assert resp1.content == dummy_jpeg
        assert "max-age=604800" in resp1.headers.get("Cache-Control", "")
        assert call_count == 1

        # 2. Second fetch for book-thumb-1 -> served from cache, no network call
        resp2 = client.get("/api/v1/books/book-thumb-1/thumbnail", headers=AUTH_HEADER)
        assert resp2.status_code == 200
        assert resp2.content == dummy_jpeg
        assert call_count == 1

        # 3. Fetch for timeout-book -> ConnectTimeout handled gracefully, returns 404 (not 500)
        resp3 = client.get("/api/v1/books/timeout-book/thumbnail", headers=AUTH_HEADER)
        assert resp3.status_code == 404


if __name__ == "__main__":
    pytest.main(["-v", __file__])




