import base64
import pytest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.grimmory_client import grimmory_client, token_cache, page_cache, page_count_cache, read_progress_cache, book_cache, active_sessions

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
            json={"libraryIds": ["lib-1"], "search": "demo"},
            headers=AUTH_HEADER
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["content"]) == 1
        assert data["content"][0]["name"] == "Series 1"
        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        assert kwargs.get("params", {}).get("library_id") == "lib-1"


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
        assert id_rakuten not in grimmory_client.custom_series
        s_detail = client.get(f"/api/v1/series/{id_rakuten}", headers=AUTH_HEADER)
        assert s_detail.status_code == 200
        assert s_detail.json()["id"] == id_rakuten
        assert s_detail.json()["name"] == "COMIC快楽天"


if __name__ == "__main__":
    pytest.main(["-v", __file__])


