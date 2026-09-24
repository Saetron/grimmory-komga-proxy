# grimmory-komga-proxy

> High-performance, lightweight bridge enabling Komga clients (like Komic, Komelia, Mihon/Tachiyomi) to seamlessly browse, search, and read books from Grimmory with instant caching and bidirectional progress synchronization!

---

## Why is this Bridge needed?

While Grimmory includes an experimental Komga emulation layer under `/komga/api/*`, connecting third-party Komga apps directly currently encounters critical blockers:
1. **Empty Pages Bug**: Grimmory's `/komga/api/v1/books/{id}/pages` returns an empty array `[]` and `pagesCount: 0`. As a result, readers report that the book has 0 pages and cannot open it.
2. **Missing / Broken Endpoints**:
   - `GET /api/v1/books/ondeck` (Continue Reading on the home screen) returns `501 Not Implemented`.
   - `GET /api/v1/books/latest`, `/series/latest`, `/series/new`, and `/series/updated` return `500 Server Error` or freeze.
   - `POST /api/v1/series/list` and `POST /api/v1/books/list` (used by Komic and Komelia to browse libraries and execute search queries) return `501 Not Implemented`.
   - `GET /api/v1/series/alphabetical-groups` returns `400 Bad Request`.
3. **No Reading Progress Sync**: Grimmory's Komga layer returns `404 Not Found` for `PATCH /api/v1/books/{id}/read-progress`.
4. **URL Path Structure**: Komga clients query standard paths (`/api/v1/*` and `/api/v2/*`) at root, while Grimmory hosts its emulation under `/komga/api/*` and reserves `/api/*` for internal web UI JWT cookies.

---

## Features

- 📄 **Accurate Pages & Aspect Ratios**: Queries Grimmory's internal `/api/v1/cbx/{id}/page-dimensions` and synthesizes compliant Komga `PageDto` objects so all pages load with correct aspect ratios.
- ⚡ **Instant Persistent Caching**: Uses a lightweight embedded SQLite database (`bridge.db`) with background reconciliation so 2,000+ series and 10,000+ books load instantly in sub-second time.
- 🖼️ **Disk-Based Thumbnail Cache**: Caches cover thumbnails directly to persistent storage (`/app/data/thumbnails`) with strict cache headers (`max-age=604800, immutable`), saving client and server RAM.
- 🔍 **Full-Text Search**: Translates both query parameter searches and complex Komic filter payloads (`POST /series/list` and `POST /books/list` with nested `fullTextSearch` and `condition: allOf`) into fast SQLite index searches.
- 🔄 **Home Screen & On Deck**: Provides fast endpoints for `GET /api/v1/books/ondeck`, `GET /api/v1/series/new`, `GET /api/v1/series/updated`, and `GET /api/v1/books/latest`.
- 📖 **Bidirectional Progress Sync**: Maps Komga `PATCH /books/{id}/read-progress` directly to Grimmory's native reading progress API, strictly segregating progress per authenticated user.
- 👥 **Multi-User & Library Security**: Forwards HTTP Basic Auth transparently and dynamically verifies each user's assigned library access before returning books or series.
- 📚 **Standalone Book & Non-ASCII Series Support**: Automatically wraps standalone books without series metadata into individual one-shots and disambiguates non-ASCII / Japanese series titles.

---

## Quick Start

### 1. Using Docker Compose (Recommended)

1. Clone this repository:
   ```bash
   git clone https://github.com/Saetron/grimmory-komga-proxy.git
   cd grimmory-komga-proxy
   ```

2. Adjust `GRIMMORY_URL` in `docker-compose.yml` to point to your Grimmory server (e.g. `http://grimmory:6060` or `http://192.168.1.100:6060`).

3. Start the bridge:
   ```bash
   docker compose up -d
   ```

### 2. Using Pre-built Docker Image from GHCR

```bash
docker run -d \
  --name grimmory-komga-proxy \
  -p 8080:8080 \
  -e GRIMMORY_URL=http://your-grimmory-server:6060 \
  -v ./data:/app/data \
  --restart unless-stopped \
  ghcr.io/saetron/grimmory-komga-proxy:latest
```

> **Note**: Mounting `-v ./data:/app/data` is strongly recommended so the SQLite cache (`bridge.db`) and cover thumbnails (`/app/data/thumbnails`) persist across container updates.

---

## Connecting with Komic (iOS / iPadOS)

1. Open **Komic** on your iPhone or iPad.
2. Tap **Add Server** and select **Komga**.
3. Fill in the connection settings:
   - **Server URL**: `http://<YOUR_BRIDGE_HOST_OR_IP>:8080` *(do not add `/komga`)*
   - **Username**: Your Grimmory username
   - **Password**: Your Grimmory password
4. Tap **Connect** / **Save**.
5. Your Grimmory libraries, series, search, and reading progress will now be fully synchronized.

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIMMORY_URL` | `http://localhost:8080` | URL of your Grimmory instance (without trailing slash) |
| `DATABASE_PATH` | `/app/data/bridge.db` | Path to persistent SQLite cache database |
| `THUMBNAIL_DIR` | `/app/data/thumbnails` | Path to disk cache for cover thumbnails |
| `PORT` | `8080` | Port the proxy listens on inside the container |
| `HOST` | `0.0.0.0` | Bind host address |
| `CACHE_TTL` | `300` | TTL in seconds for in-memory page metadata caches |
| `LOG_LEVEL` | `info` | Logging verbosity (`debug`, `info`, `warning`, `error`) |
| `GRIMMORY_USERNAME` | *(empty)* | Optional default username fallback if client doesn't send Basic Auth |
| `GRIMMORY_PASSWORD` | *(empty)* | Optional default password fallback |

---

## Supported Endpoints

- **Auth & System**: `GET /actuator/info`, `GET /api/v1/users/me`, `GET /api/v2/users/me`, `GET /api/v1/login/set-cookie`, `GET /api/logout`
- **Libraries**: `GET /api/v1/libraries`, `GET /api/v1/libraries/{id}`
- **Series**:
  - `GET /api/v1/series`, `POST /api/v1/series/list` *(with search, pagination, sort, library filters)*
  - `GET /api/v1/series/{id}`, `GET /api/v1/series/{id}/books`, `GET /api/v1/series/{id}/thumbnail`
  - `GET /api/v1/series/latest`, `/new`, `/updated`, `/alphabetical-groups`, `/genres`, `/release-dates`
- **Books**:
  - `GET /api/v1/books`, `POST /api/v1/books/list` *(with search, pagination, sort, library filters)*
  - `GET /api/v1/books/ondeck`, `POST /api/v1/books/ondeck` *(Continue Reading)*
  - `GET /api/v1/books/latest`, `POST /api/v1/books/latest` *(Recently Added)*
  - `GET /api/v1/books/released`, `POST /api/v1/books/released`
  - `GET /api/v1/books/{id}` *(Enriched with accurate page counts and user read progress)*
  - `GET /api/v1/books/{id}/thumbnail`
  - `GET /api/v1/books/{id}/pages` *(Synthesized `PageDto` array with dimension metadata)*
  - `GET /api/v1/books/{id}/pages/{pageNumber}` *(Direct page image streaming)*
  - `GET /api/v1/books/{id}/file` *(Direct book download)*
  - `GET /api/v1/books/{id}/manifest`, `/manifest/divina`
- **Reading Progress**:
  - `GET /api/v1/books/{id}/read-progress`
  - `PATCH /api/v1/books/{id}/read-progress`
  - `DELETE /api/v1/books/{id}/read-progress`
- **Collections & ReadLists**: `GET /api/v1/readlists`, `GET /api/v1/collections`
- **Authors**: `GET /api/v1/authors`, `GET /api/v2/authors`, `GET /api/v1/authors/names`
