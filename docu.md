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

### Dedicated Sync User vs. Connecting Users
The bridge operates on a two-tier user model designed to keep shared library metadata fast and reliable while strictly isolating user permissions and reading states:

1. **Background Sync User (`SYNC_USERNAME` / `SYNC_PASSWORD`)**:
   - Background sync runs with dedicated administrator/sync credentials configured in environment variables.
   - Its sole purpose is to periodically pull the full library catalog (libraries, series, books, page counts) from Grimmory and index it into SQLite (`bridge.db`).
   - The shared `books` and `series` tables store pure metadata: all user-specific progress attributes (`readProgress`, `cbxProgress`, `pdfProgress`, `epubProgress`, `koreaderProgress`, `readStatus`, `dateFinished`) are explicitly stripped before insertion.
   - Sync credentials are never used for client sessions or unauthenticated requests.

2. **Connecting Client Users (e.g. Komic, Komelia)**:
   - Every client request must provide valid HTTP Basic Auth credentials (`Authorization: Basic <base64(user:pass)>`).
   - **Strict 401 Unauthorized**: Unauthenticated requests or invalid credentials immediately throw `HTTP 401 Unauthorized` (`WWW-Authenticate: Basic realm="Komga"`).
   - **Automatic Credential Registration**: Upon successful authentication with Grimmory, the user's credentials are saved in SQLite (`users` table). The dedicated sync user is excluded.
   - **Background Progress Synchronization**: During the periodic background sync cycle, the bridge iterates over all stored users and queries `/api/v1/app/books/continue-reading` to synchronize each user's reading state and On-Deck feed while they are offline.
   - **Automatic 401 Eviction**: If Grimmory returns HTTP 401 Unauthorized for a stored user (e.g., password changed or account deleted in Grimmory), the bridge automatically deletes that user's record from `users`, purges their cached records from `read_progress`, and evicts their in-memory session tokens.
   - Users are strictly gated to their allowed libraries and their own reading progress.

### User Permissions & Library Gating
- When a user logs in, `GET /api/v1/users/me` and `GET /api/v2/users/me` return their authorized profile:
  - If a user has restricted library access (access to a subset of libraries), `sharedAllLibraries` is set to `false` and `sharedLibrariesIds` lists only the IDs of their permitted libraries.
  - This prevents reader apps like Komic from displaying unauthorized libraries in the library selector.
  - Endpoints (`/api/v1/libraries`, `/api/v1/libraries/{id}`, `/api/v1/series`, `/api/v1/books`, `/api/v1/books/ondeck`) enforce library permissions, rejecting access to unpermitted libraries with `404 Not Found`.

---

## 3. Libraries & Multi-Library Switcher

### Strict Swift Codable Compliance
Komic and other native iOS apps are written in Swift using `Codable` structs:
- **Type Mismatch Problem**: Grimmory returns library IDs as integers (`{"id": 14, "name": "Manga"}`). Swift's `JSONDecoder` throws a `typeMismatch` error if `let id: String` receives an integer, which previously broke the entire library listing and hid the library selector in Komic.
- **Normalization**: The proxy coerces all IDs to strings (`"14"`) and provides all 29 fields of Komga's `LibraryDto` with accurate boolean and enum defaults (`scanInterval="EVERY_6H"`, `seriesCover="FIRST"`, `unavailable=false`).

### Dynamic Discovery & Access Filtering
1. The proxy queries Grimmory's native `/api/v1/libraries` with the authenticated user's credentials.
2. Libraries are saved into the `libraries` table in SQLite.
3. `/api/v1/libraries` filters the returned list strictly by the user's accessible library IDs (`user_libs`).
4. If a user requests a specific library (`GET /api/v1/libraries/{id}`) that they are not permitted to view, the bridge returns `404 Not Found`.

---

## 4. Series & Book Management

### Persistent Caching & Background Sync
- **Dedicated Sync Account**: The background sync service uses `SYNC_USERNAME` and `SYNC_PASSWORD` to populate `bridge.db`.
- **Zero-Unpack Catalog Sync (SSD Protection)**: The background sync leverages Grimmory's bulk metadata endpoint `/api/v1/app/books?size=1000` to ingest all series, books, file sizes, and metadata directly from Grimmory's database in seconds. It strictly avoids invoking archive inspection endpoints (`/api/v1/cbx/{id}/pages` or `/page-dimensions`) during background sync, completely eliminating archive unzipping into temporary cache directories and preventing premature SSD wear.
- **Background Sync Schedule**: Every 30 minutes (configurable via `SYNC_INTERVAL_MINUTES`), the proxy queries Grimmory for updated books and series, reconciling them in SQLite.
- **Sub-Second Response**: Complex queries (`GET /api/v1/series`, `POST /api/v1/series/list`, `GET /api/v1/books`, `POST /api/v1/books/list`) query indexed SQLite tables, eliminating latency.

### Series Naming & Book Disambiguation (Preventing "Book XXXXX" Fallback)
- **Problem**: When querying raw Grimmory endpoints (`/api/v1/books`), book titles and series names are often nested under `metadata` dictionaries or encoded in directory paths, rather than present as top-level fields. Previously, missing top-level names caused books to fall back to `Book {id}`, creating thousands of standalone series named `Book XXXXX` with 1 book each.
- **Solution**:
  1. The bridge prioritizes Grimmory's rich `/api/v1/app/books` endpoint over `/api/v1/books`.
  2. `raw_app_book_to_dto` checks nested `metadata.title`, `metadata.seriesName`, parent directory names, and regex title patterns (`Series Name Vol. 1`) before falling back.
  3. Automatic SQLite migration scripts prune corrupt `Book XXXXX` series upon startup and re-link books to their proper series.

### Standalone Books & One-Shots
Books in Grimmory that do not belong to an official series are automatically wrapped in a synthetic series (`{lib_id}-standalone-{book_id}`) with `oneshot: true`. This allows comic and novel readers to access standalone items directly without encountering broken series relationships.

---

## 5. Page Streaming & Aspect Ratio Synthesizer

For comic and manga readers (CBZ, CBR, PDF):
1. **Lazy Page Metadata (`GET /api/v1/books/{id}/pages`)**:
   - Page dimensions and counts are extracted **on-demand** only when a user actually opens a comic to read it.
   - Queries Grimmory's `/api/v1/cbx/{id}/page-dimensions` to obtain the exact pixel dimensions (`width`, `height`) of every page.
   - Results are cached in memory and in SQLite (`media.pagesCount`) so that subsequent reads and client requests never trigger duplicate archive extractions.
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
- **`read_progress`**: `(user TEXT, book_id TEXT, page INTEGER, completed INTEGER, read_date TEXT, dto_json TEXT, updated_at REAL, PRIMARY KEY (user, book_id))`
- **`users`**: `(username TEXT PRIMARY KEY, password TEXT NOT NULL, last_login REAL, created_at REAL)`
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

### 4. Series with Square Brackets or Special Characters Not Loading / Missing Covers
- **Cause**: When series titles or directory names contain characters like brackets (e.g. `21-[oshi-no-ko]`), client HTTP frameworks (such as Komic on iOS or OkHttp) URL-encode brackets (`[` -> `%5B`, `]` -> `%5D`). Furthermore, when constructing request URLs, the `%` sign can be encoded again (`%255B`), delivering double-encoded path parameters (`21-%255Boshi-no-ko%255D`).
- **Solution**: The proxy implements recursive URL decoding (`normalize_id`), automatically resolving single or double-encoded path and query parameters back to their canonical representation (`21-[oshi-no-ko]`). SQLite lookups check candidate encodings, and series thumbnail resolvers map to the series' underlying books to retrieve valid cover images from Grimmory.
