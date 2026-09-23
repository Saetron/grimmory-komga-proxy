# grimmory-komga-proxy

> Lightweight proxy server which allows you to use your Komga clients (like Komic, Komelia, Mihon/Tachiyomi) with your Grimmory library!

---

## Why is this Bridge needed?

While Grimmory includes an experimental Komga emulation layer under `/komga/api/*`, connecting third-party Komga apps directly currently encounters critical blockers:
1. **Empty Pages Bug**: Grimmory's `/komga/api/v1/books/{id}/pages` returns an empty array `[]` and `pagesCount: 0`. As a result, readers report that the book has 0 pages and cannot open it.
2. **Missing / Broken Endpoints**:
   - `GET /api/v1/books/ondeck` (Continue Reading on the home screen) returns `501 Not Implemented`.
   - `GET /api/v1/books/latest` and `/series/latest` return `500 Server Error`.
   - `POST /api/v1/series/list` and `POST /api/v1/books/list` (used by Komic and Komelia to browse libraries) return `501 Not Implemented`.
   - `GET /api/v1/series/alphabetical-groups` returns `400 Bad Request`.
3. **No Reading Progress Sync**: Grimmory's Komga layer returns `404 Not Found` for `PATCH /api/v1/books/{id}/read-progress`.
4. **URL Path Structure**: Komga clients query standard paths (`/api/v1/*` and `/api/v2/*`) at root, while Grimmory hosts its emulation under `/komga/api/*` and reserves `/api/*` for internal web UI JWT cookies.

**This bridge solves all of these issues transparently:**
- 📄 **Fixes Pages & Dimensions**: Queries Grimmory's internal `/api/v1/cbx/{id}/page-dimensions` and synthesizes compliant Komga `PageDto` objects so all pages load with correct aspect ratios.
- 🔄 **Home Screen Support**: Translates `/api/v1/books/ondeck` to Grimmory's continue-reading list and `/api/v1/books/latest` to recently added books.
- ⚡ **POST List Translation**: Intercepts `POST /series/list` and `POST /books/list` requests and translates them into Grimmory queries.
- 📖 **Full Progress Sync**: Maps Komga `PATCH /books/{id}/read-progress` directly to Grimmory's native reading progress API.
- 🚀 **High Speed**: Built with async Python (FastAPI + HTTPX) with in-memory caching for lightning-fast page flipping and low latency.
- 👥 **Multi-User**: Forwards HTTP Basic Auth transparently so each user logs in with their own Grimmory account and maintains separate reading progress.
- 📚 **Standalone Book Protection**: Safely wraps books without series metadata into virtual one-shot series so Komga clients never crash.

---

## Quick Start

### 1. Using Docker Compose (Recommended)

1. Clone this repository to your server:
   ```bash
   git clone https://github.com/Saetron/grimmory-komga-proxy.git
   cd grimmory-komga-proxy
   ```

2. Adjust `GRIMMORY_URL` in `docker-compose.yml` (e.g. `http://your-grimmory-server:8080`).

3. Start the container:
   ```bash
   docker compose up -d
   ```

### 2. Using Pre-built Docker Image from GHCR

The GitHub Actions workflow automatically builds and publishes multi-arch images (`linux/amd64`, `linux/arm64`) to GitHub Container Registry:

```bash
docker run -d \
  --name grimmory-komga-proxy \
  -p 8080:8080 \
  -e GRIMMORY_URL=http://your-grimmory-server:8080 \
  --restart unless-stopped \
  ghcr.io/saetron/grimmory-komga-proxy:latest
```

---

## Connecting with Komic (iOS / iPadOS)

1. Open **Komic** on your iPhone or iPad.
2. Tap **Add Server** and select **Komga**.
3. Fill in the connection settings:
   - **Server URL**: `http://<YOUR_BRIDGE_IP_OR_HOSTNAME>:8080` *(do not add `/komga`)*
   - **Username**: Your Grimmory username (e.g. `your_username`)
   - **Password**: Your Grimmory password (e.g. `your_password`)
4. Tap **Connect** / **Save**.
5. Your Grimmory libraries, series, and books will now appear in Komic with full cover art, page streaming, offline downloads, and reading progress sync!

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GRIMMORY_URL` | `http://localhost:8080` | The URL of your Grimmory instance (without trailing slash) |
| `PORT` | `8080` | Port the proxy listens on inside the container |
| `HOST` | `0.0.0.0` | Bind host address |
| `CACHE_TTL` | `300` | TTL in seconds for cached page metadata and book details |
| `LOG_LEVEL` | `info` | Logging verbosity (`debug`, `info`, `warning`, `error`) |
| `GRIMMORY_USERNAME` | *(empty)* | Optional default username fallback if client doesn't send Basic Auth |
| `GRIMMORY_PASSWORD` | *(empty)* | Optional default password fallback |

---

## Supported Endpoints

- `GET /actuator/info` (Komga server health and build info)
- `GET /api/v2/users/me` (Authentication and user permissions)
- `GET /api/v1/login/set-cookie` & `/api/logout`
- `GET /api/v1/libraries` & `/api/v1/libraries/{id}`
- `GET /api/v1/series` & `POST /api/v1/series/list`
- `GET /api/v1/series/{id}` & `/api/v1/series/{id}/books` & `/thumbnail`
- `GET /api/v1/series/latest`, `/new`, `/updated`, `/alphabetical-groups`, `/genres`
- `GET /api/v1/books` & `POST /api/v1/books/list`
- `GET /api/v1/books/ondeck` (Continue Reading)
- `GET /api/v1/books/latest` (Recently added)
- `GET /api/v1/books/{id}` (Enriched with accurate `pagesCount` & `readProgress`)
- `GET /api/v1/books/{id}/thumbnail`
- `GET /api/v1/books/{id}/pages` (Synthesized `PageDto` array with dimensions)
- `GET /api/v1/books/{id}/pages/{pageNumber}` (Streaming page images)
- `GET /api/v1/books/{id}/file` (Streaming book downloads)
- `GET /api/v1/books/{id}/read-progress` (Reading progress retrieval)
- `PATCH /api/v1/books/{id}/read-progress` (Reading progress update)
- `DELETE /api/v1/books/{id}/read-progress` (Mark unread / reset progress)
- `GET /api/v1/books/{id}/manifest` & `/manifest/divina` (Divina WebPub manifest)
- `GET /api/v1/readlists` & `/api/v1/collections`
- `GET /api/v1/authors` & `/authors/names` & `/authors/roles`
- Catch-all fallback for any remaining `/api/v1/*` endpoints
