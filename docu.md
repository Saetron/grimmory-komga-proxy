# Grimmory-Komga Bridge Documentation (`docu.md`)

This document provides an in-depth technical overview of the **grimmory-komga-proxy** architecture, describing how Komga client requests are translated into Grimmory native REST calls, how data is cached and normalized, and how compatibility is ensured across third-party reader clients (such as Komic, Komelia, and Mihon).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Authentication & Multi-User Model](#2-authentication--multi-user-model)
3. [Libraries & Multi-Library Switcher](#3-libraries--multi-library-switcher)
4. [Series & Book Management](#4-series--book-management)
5. [Page Streaming & Aspect Ratio Synthesizer](#5-page-streaming--aspect-ratio-synthesizer)
6. [Book File Downloading & Offline Reading](#6-book-file-downloading--offline-reading)
7. [Reading Progress Synchronization](#7-reading-progress-synchronization)
8. [Comprehensive Endpoint Mapping](#8-comprehensive-endpoint-mapping)
9. [Database Schema (`bridge.db`)](#9-database-schema-bridgedb)
10. [Troubleshooting & Client Pitfalls](#10-troubleshooting--client-pitfalls)

---

## 1. Architecture Overview

```
┌─────────────────────────────────┐
│          Komga Client           │ (Komic iOS, Komelia, Mihon, etc.)
│  - HTTP Basic Auth              │
│  - Standard Komga OpenAPI v1/v2 │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│     grimmory-komga-proxy        │ (FastAPI / Python)
│  - SQLite Cache (`bridge.db`)   │
│  - Disk Thumbnails Cache        │
│  - DTO & Schema Normalizer      │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│        Grimmory Server          │ (Native REST API: `/api/v1/*`)
│  - JWT Bearer Authentication    │
│  - Native Book, CBX, PDF APIs   │
└─────────────────────────────────┘
```

### Why Bypass Grimmory's `/komga` Emulation?
Grimmory historically included an experimental Komga emulation layer under `/komga/api/*`. However, that layer is incomplete, deprecated, and frequently disabled in production setups.
- **Empty Pages**: The `/komga` layer returned empty page arrays (`[]`) and `pagesCount: 0`.
- **Missing Endpoints**: Key endpoints (`/api/v1/books/ondeck`, `POST /series/list`, `POST /books/list`) returned `501 Not Implemented`.
- **Read Progress**: Progress updates (`PATCH /api/v1/books/{id}/read-progress`) returned `404 Not Found`.

The proxy completely bypasses `/komga` and communicates exclusively with Grimmory's stable native REST endpoints (`/api/v1/*`), translating data into 100% compliant Komga DTOs.

---

## 2. Authentication & Multi-User Model

### Basic Auth to JWT Bearer Translation
Komga clients authenticate via standard HTTP `Authorization: Basic <base64(user:pass)>`.
1. The proxy decodes the Basic credentials.
2. If mapped credentials exist in `USER_MAPPINGS`, the proxy resolves them.
3. The proxy queries Grimmory's `/api/v1/auth/login` to obtain a JWT Bearer token (`accessToken`).
4. Tokens are cached in-memory with automatic expiration and re-login on any `401 Unauthorized`.

### User Permissions & Isolation
- User details (`GET /api/v1/users/me` and `GET /api/v2/users/me`) populate `sharedAllLibraries: true` and all accessible `sharedLibrariesIds` as string arrays.
- Reading progress is strictly segregated in the SQLite database and in-memory caches using the authenticated username as a namespace (e.g. `username:bookId`).
- Client requests only use their explicit Basic Auth credentials. Background sync credentials are never used for client sessions or unauthenticated requests.

---

## 3. Libraries & Multi-Library Switcher

### Strict Swift Codable Compliance
Komic and other native iOS apps are written in Swift using `Codable` structs:
- **Type Mismatch Problem**: Grimmory returns library IDs as integers (`{"id": 14, "name": "Manga"}`). Swift's `JSONDecoder` throws a `typeMismatch` error if `let id: String` receives an integer, which previously broke the entire library listing and hid the library selector in Komic.
- **Normalization**: The proxy coerces all IDs to strings (`"14"`) and provides all 29 fields of Komga's `LibraryDto` with accurate boolean and enum defaults (`scanInterval="EVERY_6H"`, `seriesCover="FIRST"`, `unavailable=false`).

### Dynamic Discovery & Fallback
1. The proxy queries Grimmory's native `/api/v1/libraries`.
2. Libraries are saved into the `libraries` table in SQLite.
3. If upstream libraries are unavailable or empty, the proxy discovers distinct `library_id` values from cached books and series, ensuring the library switcher in Komic is always visible and functional.

---

## 4. Series & Book Management

### Persistent Caching & Background Sync
- **Dedicated Sync Account (`SYNC_USERNAME` / `SYNC_PASSWORD`)**: The background sync service uses its own dedicated credentials (typically an administrator account with access to all libraries). These credentials are used strictly and exclusively by the background sync service to build and validate the SQLite metadata cache (`bridge.db`), and are completely isolated from client API calls and client reading progress.
- **Background Sync Schedule**: Every 30 minutes (configurable via `SYNC_INTERVAL_MINUTES`), the proxy queries Grimmory for updated books, page counts, and series and reconciles them in SQLite.
- **Sub-Second Response**: Complex queries (`GET /api/v1/series`, `POST /api/v1/series/list`, `GET /api/v1/books`, `POST /api/v1/books/list`) query indexed SQLite tables, eliminating latency.

### Full-Text Search & Complex Filter Conditions
Clients like Komic send search and filter queries as POST requests with nested condition structures:
```json
{
  "condition": {
    "allOf": [
      { "libraryId": { "operator": "is", "value": "14" } },
      { "fullTextSearch": { "operator": "is", "value": "Spica" } }
    ]
  }
}
```
The proxy's query parser recursively unwraps nested conditions (`allOf`, `anyOf`, `noneOf`), extracts library IDs and search terms, and maps them to parameterized SQL `LIKE` queries across title, series name, and author fields.

### Standalone Books & One-Shots
Books in Grimmory that do not belong to an official series are automatically wrapped in a synthetic series (`{lib_id}-standalone-{book_id}`) with `oneshot: true`. This allows comic and novel readers to access standalone items directly without encountering broken series relationships.

---

## 5. Page Streaming & Aspect Ratio Synthesizer

For comic and manga readers (CBZ, CBR, PDF):
1. **Page Metadata (`GET /api/v1/books/{id}/pages`)**:
   - Queries Grimmory's `/api/v1/cbx/{id}/page-dimensions` to obtain the exact pixel dimensions (`width`, `height`) of every page.
   - Synthesizes an array of `PageDto` objects with accurate `mediaType`, page numbers, and dimensions.
2. **Page Image Streaming (`GET /api/v1/books/{id}/pages/{pageNumber}`)**:
   - Fetches the raw image from Grimmory's `/api/v1/cbx/{id}/page/{pageNumber}` or `/api/v1/pdf/{id}/page/{pageNumber}`.
   - Streams the binary data with appropriate `Content-Type: image/jpeg` or `image/png`.

---

## 6. Book File Downloading & Offline Reading

When reading EPUBs in Komic (via Readium) or downloading books for offline reading, the client requests `GET /api/v1/books/{id}/file`.

### File Resolution Pipeline
The proxy queries the following native endpoints on Grimmory in priority order:
1. `GET /api/v1/books/{clean_id}/content` (Primary binary content endpoint supporting byte ranges)
2. `GET /api/v1/books/{clean_id}/download` (Streaming download endpoint)
3. `GET /api/v1/app/books/{clean_id}/file`

### Solving the iOS Destination Directory Collision
- **The Bug**: iOS `URLSessionDownloadTask` downloads files to a temporary path (e.g. `CFNetworkDownload_xxx.tmp`). When moving the file to its destination directory (`.../books/`), if the filename is empty, the target URL becomes `.../books` (the directory itself), causing the error:
  `"CFNetworkDownload_xxx.tmp" konnte nicht nach "books" bewegt werden, da letzteres bereits existiert.`
- **The Fix**:
  1. **Book URL Field**: `BookDto.url` is normalized to `f"/books/{lib_id}/{filename}.{ext}"` (e.g. `"/books/14/[Oshi no Ko] Spica the First Star.epub"`), providing the exact filename and extension for clients that derive filenames from `book.url`.
  2. **RFC 6266 / RFC 5987 Dual Content-Disposition**:
     ```http
     Content-Disposition: attachment; filename="Oshi no Ko Spica the First Star.epub"; filename*=UTF-8''%5BOshi%20no%20Ko%5D%20Spica%20the%20First%20Star.epub
     ```
     Providing both standard ASCII `filename="..."` and UTF-8 `filename*=` ensures compatibility across Apple Foundation URLSession, Readium, and web browsers.
  3. **HEAD Method Support**: Supports both `GET` and `HEAD` for `/{book_id}/file` and `/{book_id}/file/{filename}`.

---

## 7. Reading Progress Synchronization

### Bi-Directional Mapping
Komga clients report progress via `PATCH /api/v1/books/{id}/read-progress`:
```json
{
  "page": 42,
  "completed": false
}
```

The proxy maps this update:
1. **CBX / Manga**: Computes percentage based on `page / pagesCount * 100` and dispatches to Grimmory's `/api/v1/reading-sessions` and `/api/v1/cbx/{id}/progress`.
2. **EPUB / Novels**: Computes percentage and updates Grimmory's native progress tracking.
3. **Local Cache**: Updates SQLite `read_progress` table and memory cache instantly so Home Screen "On Deck" and "Continue Reading" update without lag.

---

## 8. Comprehensive Endpoint Mapping

| Komga Endpoint | HTTP Method | Upstream Grimmory Native API | Purpose |
| :--- | :--- | :--- | :--- |
| `/api/v1/users/me`, `/api/v2/users/me` | GET | `/api/v1/auth/login` | User profile & library authorization |
| `/api/v1/libraries` | GET | `/api/v1/libraries` | List all libraries with string IDs |
| `/api/v1/libraries/{id}` | GET | `/api/v1/libraries` | Get single library DTO |
| `/api/v1/series`, `POST /series/list` | GET, POST | SQLite (`series`) & `/api/v1/app/books/recently-added` | Filtered, sorted series list |
| `/api/v1/series/{id}` | GET | SQLite (`series`) & `/api/v1/books` | Single series details |
| `/api/v1/series/{id}/books` | GET | SQLite (`books`) & `/api/v1/books` | Books in series |
| `/api/v1/series/{id}/thumbnail` | GET | `/api/v1/media/book/{first_book_id}/cover` | Cached series thumbnail |
| `/api/v1/series/latest`, `/new`, `/updated`| GET | Chronological book activity aggregation | Home screen series feeds |
| `/api/v1/books`, `POST /books/list` | GET, POST | SQLite (`books`) & `/api/v1/books` | Filtered, sorted book list |
| `/api/v1/books/{id}` | GET | SQLite (`books`) & `/api/v1/books/{id}` | Single book details with pagesCount |
| `/api/v1/books/{id}/thumbnail` | GET | `/api/v1/media/book/{id}/cover` | Cover image with disk cache |
| `/api/v1/books/{id}/pages` | GET | `/api/v1/cbx/{id}/page-dimensions` | Synthesized `PageDto` array |
| `/api/v1/books/{id}/pages/{n}` | GET | `/api/v1/cbx/{id}/page/{n}`, `/pdf/{id}/page/{n}` | Single page image stream |
| `/api/v1/books/{id}/file` | GET, HEAD | `/api/v1/books/{id}/content`, `/download` | Raw book file download (EPUB, CBZ, PDF) |
| `/api/v1/books/ondeck` | GET, POST | In-progress cache & `/api/v1/books` | "Continue Reading" home feed |
| `/api/v1/books/latest` | GET, POST | SQLite (`books`) by `created DESC` | "Recently Added" home feed |
| `/api/v1/books/{id}/read-progress` | GET, PATCH | SQLite & Grimmory `/api/v1/reading-sessions` | Reading progress synchronization |

---

## 9. Database Schema (`bridge.db`)

SQLite is located at `/app/data/bridge.db` and contains the following tables:

- **`libraries`**: `(id TEXT PRIMARY KEY, name TEXT, dto_json TEXT, updated_at REAL)`
- **`series`**: `(id TEXT PRIMARY KEY, library_id TEXT, name TEXT, books_count INTEGER, dto_json TEXT, updated_at REAL)`
- **`books`**: `(id TEXT PRIMARY KEY, series_id TEXT, library_id TEXT, name TEXT, number_sort REAL, pages_count INTEGER, dto_json TEXT, updated_at REAL)`
- **`book_pages`**: `(book_id TEXT PRIMARY KEY, page_count INTEGER, pages_json TEXT, updated_at REAL)`
- **`read_progress`**: `(user TEXT, book_id TEXT, page INTEGER, completed INTEGER, progress_json TEXT, updated_at REAL, PRIMARY KEY (user, book_id))`
- **`key_value`**: `(key TEXT PRIMARY KEY, value TEXT, updated_at REAL)`

---

## 10. Troubleshooting & Client Pitfalls

### 1. Library switcher missing in Komic
- **Cause**: Komic decodes `id: String`. If numeric IDs are returned, decoding fails.
- **Solution**: Handled automatically by the proxy. All IDs are coerced to strings.

### 2. "CFNetworkDownload_...tmp konnte nicht nach books bewegt werden"
- **Cause**: Komic attempted to move the downloaded file to the directory path because the derived filename was empty.
- **Solution**: Handled by setting `book.url` to `/books/{lib_id}/{filename}.{ext}` and providing explicit `Content-Disposition` with both `filename="..."` and `filename*=UTF-8''...`.

### 3. Readium Error when opening EPUBs
- **Cause**: Readium requires a local file with a valid `.epub` extension and valid ZIP structure.
- **Solution**: The proxy forwards the raw binary stream from `/api/v1/books/{id}/content` with `Content-Type: application/epub+zip`.
