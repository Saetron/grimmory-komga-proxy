import sqlite3
import json
import os
import time
import tempfile
import logging
from typing import Optional, Dict, Any, List, Tuple, Set, Union
from app.config import settings

logger = logging.getLogger("grimmory-komga-bridge")

class Database:
    def __init__(self, db_path: Optional[str] = None):
        preferred_path = db_path or settings.DATABASE_PATH
        self.db_path = self._resolve_writable_db_path(preferred_path)
        self._init_db()

    def _resolve_writable_db_path(self, preferred_path: str) -> str:
        candidates = [
            preferred_path,
            os.path.join(tempfile.gettempdir(), "bridge.db"),
            "file:bridge_mem?mode=memory&cache=shared"
        ]
        for candidate in candidates:
            if candidate.startswith("file:"):
                logger.info("Using in-memory SQLite database as fallback")
                return candidate
            try:
                db_dir = os.path.dirname(os.path.abspath(candidate))
                os.makedirs(db_dir, exist_ok=True)
                test_conn = sqlite3.connect(candidate, timeout=5.0, uri=True)
                test_conn.execute("CREATE TABLE IF NOT EXISTS _test_write (id INTEGER PRIMARY KEY);")
                test_conn.execute("INSERT OR REPLACE INTO _test_write (id) VALUES (1);")
                test_conn.execute("DROP TABLE _test_write;")
                test_conn.commit()
                test_conn.close()
                if candidate != preferred_path:
                    logger.warning(
                        f"Database path '{preferred_path}' is not writable. Falling back to '{candidate}'."
                    )
                else:
                    logger.info(f"Using persistent SQLite database at '{candidate}'")
                return candidate
            except (sqlite3.OperationalError, PermissionError, OSError) as e:
                logger.warning(f"Could not open or write database at '{candidate}': {e}")
                continue

        return "file:bridge_mem?mode=memory&cache=shared"

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            try:
                conn.execute("PRAGMA journal_mode=DELETE;")
            except Exception:
                pass
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            # Migration check for read_progress table
            cursor = conn.execute("PRAGMA table_info(read_progress)")
            cols = [row["name"] for row in cursor.fetchall()]
            if cols and "user" not in cols:
                conn.execute("ALTER TABLE read_progress RENAME TO read_progress_old;")
                conn.execute("""
                    CREATE TABLE read_progress (
                        user TEXT NOT NULL,
                        book_id TEXT NOT NULL,
                        page INTEGER,
                        completed INTEGER,
                        read_date TEXT,
                        dto_json TEXT,
                        updated_at REAL,
                        PRIMARY KEY (user, book_id)
                    );
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO read_progress (user, book_id, page, completed, read_date, dto_json, updated_at)
                    SELECT 'default', book_id, page, completed, read_date, dto_json, updated_at FROM read_progress_old;
                """)
                conn.execute("DROP TABLE read_progress_old;")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS series (
                    id TEXT PRIMARY KEY,
                    library_id TEXT,
                    name TEXT,
                    books_count INTEGER,
                    dto_json TEXT,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_series_lib ON series (library_id);
                CREATE INDEX IF NOT EXISTS idx_series_name ON series (name);

                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY,
                    series_id TEXT,
                    library_id TEXT,
                    name TEXT,
                    number_sort REAL,
                    pages_count INTEGER,
                    dto_json TEXT,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_books_series ON books (series_id);
                CREATE INDEX IF NOT EXISTS idx_books_lib ON books (library_id);
                CREATE INDEX IF NOT EXISTS idx_books_name ON books (name);

                CREATE TABLE IF NOT EXISTS book_pages (
                    book_id TEXT PRIMARY KEY,
                    pages_json TEXT,
                    pages_count INTEGER,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS read_progress (
                    user TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    page INTEGER,
                    completed INTEGER,
                    read_date TEXT,
                    dto_json TEXT,
                    updated_at REAL,
                    PRIMARY KEY (user, book_id)
                );
                CREATE INDEX IF NOT EXISTS idx_read_progress_user ON read_progress (user);
                CREATE INDEX IF NOT EXISTS idx_read_progress_completed ON read_progress (user, completed);

                CREATE TABLE IF NOT EXISTS libraries (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    dto_json TEXT,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_libraries_name ON libraries (name);

                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    last_login REAL,
                    created_at REAL
                );
            """)

            # Sanitize books table: remove any embedded readProgress from shared dto_json
            try:
                cursor = conn.execute("SELECT id, dto_json FROM books WHERE dto_json LIKE '%readProgress%'")
                dirty_rows = cursor.fetchall()
                if dirty_rows:
                    cleaned_rows = []
                    for r in dirty_rows:
                        try:
                            d = json.loads(r["dto_json"])
                            if "readProgress" in d:
                                d.pop("readProgress", None)
                                for k in ("cbxProgress", "pdfProgress", "epubProgress", "koreaderProgress"):
                                    d.pop(k, None)
                                cleaned_rows.append((json.dumps(d), str(r["id"])))
                        except Exception:
                            pass
                    if cleaned_rows:
                        conn.executemany("UPDATE books SET dto_json = ? WHERE id = ?", cleaned_rows)
            except Exception:
                pass

            # Prune corrupt standalone series named 'Book XXXXX'
            try:
                conn.execute(
                    """
                    DELETE FROM series
                    WHERE id LIKE '%-standalone-%'
                      AND (name LIKE 'Book %' OR name = 'Book' OR name IS NULL OR name = '')
                    """
                )
            except Exception:
                pass

    # --- Private Helpers ---
    def _normalize_library_ids(self, library_id: Optional[Union[str, List[str], Set[str]]]) -> Optional[List[str]]:
        """Normalize library_id input to a list of ID strings, or None for no filter."""
        if library_id is None:
            return None
        if isinstance(library_id, str):
            if "," in library_id:
                ids = [x.strip() for x in library_id.split(",") if x.strip()]
                return ids if ids else None
            stripped = library_id.strip()
            return [stripped] if stripped else None
        if isinstance(library_id, (list, set, tuple)):
            ids = [str(x) for x in library_id if str(x).strip()]
            return ids if ids else None
        return None

    def _parse_dto_rows(self, rows) -> List[Dict[str, Any]]:
        """Parse dto_json from database rows, skipping invalid entries."""
        result = []
        for r in rows:
            try:
                result.append(json.loads(r["dto_json"]))
            except Exception:
                pass
        return result

    def _query_with_library_filter(
        self,
        conn: sqlite3.Connection,
        base_query: str,
        lib_ids: Optional[List[str]],
        extra_where: str = "",
        extra_params: Optional[list] = None,
        order_by: str = ""
    ) -> list:
        """Execute a query with optional library_id filtering."""
        params = []
        where_parts = []

        if lib_ids:
            if len(lib_ids) == 1:
                where_parts.append("library_id = ?")
                params.append(lib_ids[0])
            else:
                placeholders = ",".join("?" * len(lib_ids))
                where_parts.append(f"library_id IN ({placeholders})")
                params.extend(lib_ids)

        if extra_where:
            where_parts.append(extra_where)
            if extra_params:
                params.extend(extra_params)

        query = base_query
        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)
        if order_by:
            query += f" ORDER BY {order_by}"

        return conn.execute(query, params).fetchall()

    # --- Library Operations ---
    def save_library(self, lib: Dict[str, Any]) -> None:
        lib_id = str(lib.get("id", ""))
        if not lib_id:
            return
        name = str(lib.get("name") or f"Library {lib_id}")
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO libraries (id, name, dto_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    dto_json = excluded.dto_json,
                    updated_at = excluded.updated_at
                """,
                (lib_id, name, json.dumps(lib), now)
            )

    def save_libraries_batch(self, libs: List[Dict[str, Any]]) -> None:
        if not libs:
            return
        now = time.time()
        rows = []
        for lib in libs:
            lib_id = str(lib.get("id", ""))
            if not lib_id:
                continue
            name = str(lib.get("name") or f"Library {lib_id}")
            rows.append((lib_id, name, json.dumps(lib), now))
        if not rows:
            return
        with self._get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO libraries (id, name, dto_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    dto_json = excluded.dto_json,
                    updated_at = excluded.updated_at
                """,
                rows
            )

    def get_library(self, lib_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT dto_json FROM libraries WHERE id = ?", (str(lib_id),)).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
            row_chk = conn.execute(
                "SELECT 1 FROM books WHERE library_id = ? UNION SELECT 1 FROM series WHERE library_id = ?",
                (str(lib_id), str(lib_id))
            ).fetchone()
            if row_chk:
                name = settings.CUSTOM_LIBRARY_NAMES.get(str(lib_id), f"Library {lib_id}")
                return {"id": str(lib_id), "name": name}
        return None

    def get_all_libraries(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT dto_json FROM libraries ORDER BY name ASC, id ASC")
            libs = []
            for row in cursor.fetchall():
                try:
                    libs.append(json.loads(row["dto_json"]))
                except Exception:
                    pass
            existing_ids = {str(l.get("id")) for l in libs if l.get("id")}
            cursor = conn.execute(
                """
                SELECT DISTINCT library_id FROM books WHERE library_id IS NOT NULL AND library_id != ''
                UNION
                SELECT DISTINCT library_id FROM series WHERE library_id IS NOT NULL AND library_id != ''
                """
            )
            for row in cursor.fetchall():
                l_id = str(row["library_id"])
                if l_id and l_id not in existing_ids:
                    name = settings.CUSTOM_LIBRARY_NAMES.get(l_id, f"Library {l_id}")
                    libs.append({"id": l_id, "name": name})
                    existing_ids.add(l_id)
            return libs

    def get_all_library_ids(self) -> Set[str]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id FROM libraries
                UNION
                SELECT DISTINCT library_id FROM books WHERE library_id IS NOT NULL AND library_id != ''
                UNION
                SELECT DISTINCT library_id FROM series WHERE library_id IS NOT NULL AND library_id != ''
                """
            )
            return {str(r[0]) for r in cursor.fetchall() if r[0]}

    # --- Series Operations ---
    def _extract_series_fields(self, s: Dict[str, Any]) -> tuple:
        s_id = str(s.get("id"))
        lib_id = str(s.get("libraryId", ""))
        name = str(s.get("name") or s.get("metadata", {}).get("title") or "")
        books_count = int(s.get("booksCount", 0))
        return s_id, lib_id, name, books_count

    def save_series(self, series_dto: Dict[str, Any]):
        s_id, lib_id, name, books_count = self._extract_series_fields(series_dto)
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO series (id, library_id, name, books_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (s_id, lib_id, name, books_count, json.dumps(series_dto), now)
            )

    def save_series_batch(self, series_list: List[Dict[str, Any]]):
        now = time.time()
        rows = []
        for s in series_list:
            s_id, lib_id, name, books_count = self._extract_series_fields(s)
            rows.append((s_id, lib_id, name, books_count, json.dumps(s), now))
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO series (id, library_id, name, books_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                rows
            )

    def get_series(self, series_id: str) -> Optional[Dict[str, Any]]:
        s_id = str(series_id)
        candidates = [s_id]
        from urllib.parse import unquote
        cur = s_id
        for _ in range(3):
            dec = unquote(cur)
            if dec == cur:
                break
            if dec not in candidates:
                candidates.append(dec)
            cur = dec

        with self._get_connection() as conn:
            for cid in candidates:
                row = conn.execute("SELECT dto_json FROM series WHERE id = ?", (cid,)).fetchone()
                if row:
                    try:
                        return json.loads(row["dto_json"])
                    except Exception:
                        pass
        return None

    def get_all_series(self, library_id: Optional[Union[str, List[str], Set[str]]] = None) -> List[Dict[str, Any]]:
        lib_ids = self._normalize_library_ids(library_id)
        with self._get_connection() as conn:
            rows = self._query_with_library_filter(
                conn, "SELECT dto_json FROM series", lib_ids,
                order_by="name ASC, id ASC"
            )
            return self._parse_dto_rows(rows)

    def search_series(self, query: str, library_id: Optional[Union[str, List[str], Set[str]]] = None) -> List[Dict[str, Any]]:
        term = f"%{query.strip()}%"
        lib_ids = self._normalize_library_ids(library_id)
        with self._get_connection() as conn:
            rows = self._query_with_library_filter(
                conn, "SELECT dto_json FROM series", lib_ids,
                extra_where="(name LIKE ? OR dto_json LIKE ?)",
                extra_params=[term, term],
                order_by="name ASC, id ASC"
            )
            return self._parse_dto_rows(rows)

    # --- Books Operations ---
    def _extract_book_fields(self, b: Dict[str, Any]) -> tuple:
        b_id = str(b.get("id"))
        s_raw = b.get("seriesId")
        s_id = str(s_raw).strip() if s_raw and str(s_raw).strip() not in ("", "null", "None") else ""
        lib_raw = b.get("libraryId")
        lib_id = str(lib_raw).strip() if lib_raw and str(lib_raw).strip() not in ("", "null", "None") else ""
        name = str(b.get("name") or b.get("metadata", {}).get("title") or "")
        try:
            num_sort = float(b.get("metadata", {}).get("numberSort", b.get("number", 1.0)))
        except Exception:
            num_sort = 1.0
        pages_count = int(b.get("media", {}).get("pagesCount", 1))
        return b_id, s_id, lib_id, name, num_sort, pages_count

    def _clean_book_dto_for_db(self, b: Dict[str, Any]) -> Dict[str, Any]:
        """Strip user-specific read progress and raw progress attributes before saving to shared books table."""
        b_clean = dict(b)
        b_clean.pop("readProgress", None)
        for k in ("cbxProgress", "pdfProgress", "epubProgress", "koreaderProgress", "readStatus", "dateFinished"):
            b_clean.pop(k, None)
        return b_clean

    def save_book(self, book_dto: Dict[str, Any]):
        b_id, s_id, lib_id, name, num_sort, pages_count = self._extract_book_fields(book_dto)
        b_clean = self._clean_book_dto_for_db(book_dto)
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO books (id, series_id, library_id, name, number_sort, pages_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (b_id, s_id, lib_id, name, num_sort, pages_count, json.dumps(b_clean), now)
            )

    def save_books_batch(self, books_list: List[Dict[str, Any]], user: Optional[str] = None, save_progress: bool = True):
        now = time.time()
        rows = []
        u = str(user or "testuser").lower().strip()
        for b in books_list:
            b_id, s_id, lib_id, name, num_sort, pages_count = self._extract_book_fields(b)
            prog = b.get("readProgress")
            if save_progress and isinstance(prog, dict):
                now_iso = prog.get("readDate") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.save_read_progress(u, b_id, prog.get("page", 1), prog.get("completed", False), now_iso, prog)
            b_clean = self._clean_book_dto_for_db(b)
            rows.append((b_id, s_id, lib_id, name, num_sort, pages_count, json.dumps(b_clean), now))
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO books (id, series_id, library_id, name, number_sort, pages_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows
            )

    def cleanup_stale_series(self, active_series_ids: Optional[Set[str]] = None) -> None:
        """Remove bogus or orphaned standalone series from the database."""
        with self._get_connection() as conn:
            # 1. Remove series named 'Book %' or with id like '%-standalone-%' that has bogus name
            conn.execute(
                """
                DELETE FROM series
                WHERE (name LIKE 'Book %' OR name = 'Book' OR name IS NULL OR name = '')
                  AND id LIKE '%-standalone-%'
                """
            )
            # 2. Also remove any standalone series whose books have been assigned to a real series
            conn.execute(
                """
                DELETE FROM series
                WHERE id LIKE '%-standalone-%'
                  AND id NOT IN (SELECT DISTINCT series_id FROM books WHERE series_id IS NOT NULL AND series_id != '')
                """
            )
            # 3. If active_series_ids is given, prune any standalone series not in the active set
            if active_series_ids is not None:
                cursor = conn.execute("SELECT id FROM series WHERE id LIKE '%-standalone-%'")
                for row in cursor.fetchall():
                    s_id = row["id"]
                    if s_id not in active_series_ids:
                        conn.execute("DELETE FROM series WHERE id = ?", (s_id,))

    def get_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        b_id = str(book_id)
        candidates = [b_id]
        from urllib.parse import unquote
        cur = b_id
        for _ in range(3):
            dec = unquote(cur)
            if dec == cur:
                break
            if dec not in candidates:
                candidates.append(dec)
            cur = dec

        with self._get_connection() as conn:
            for cid in candidates:
                row = conn.execute("SELECT dto_json FROM books WHERE id = ?", (cid,)).fetchone()
                if row:
                    try:
                        return json.loads(row["dto_json"])
                    except Exception:
                        pass
        return None

    def _get_adjacent_book(self, book_id: str, direction: str) -> Optional[Dict[str, Any]]:
        """Get the previous or next book in the same series by number_sort."""
        current = self.get_book(book_id)
        if not current:
            return None
        series_id = current.get("seriesId")
        if not series_id:
            return None
        try:
            num_sort = float(current.get("metadata", {}).get("numberSort", current.get("number", 1.0)))
        except Exception:
            num_sort = 1.0

        if direction == "previous":
            query = "SELECT dto_json FROM books WHERE series_id = ? AND number_sort < ? ORDER BY number_sort DESC, id DESC LIMIT 1"
        else:
            query = "SELECT dto_json FROM books WHERE series_id = ? AND number_sort > ? ORDER BY number_sort ASC, id ASC LIMIT 1"

        with self._get_connection() as conn:
            row = conn.execute(query, (str(series_id), num_sort)).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
        return None

    def get_previous_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        return self._get_adjacent_book(book_id, "previous")

    def get_next_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        return self._get_adjacent_book(book_id, "next")

    def get_books_by_series(self, series_id: str) -> List[Dict[str, Any]]:
        s_id = str(series_id)
        candidates = [s_id]
        from urllib.parse import unquote
        cur = s_id
        for _ in range(3):
            dec = unquote(cur)
            if dec == cur:
                break
            if dec not in candidates:
                candidates.append(dec)
            cur = dec

        with self._get_connection() as conn:
            for cid in candidates:
                rows = conn.execute(
                    "SELECT dto_json FROM books WHERE series_id = ? ORDER BY number_sort ASC, name ASC, id ASC",
                    (cid,)
                ).fetchall()
                if rows:
                    return self._parse_dto_rows(rows)
            return []

    def get_all_books(self, library_id: Optional[Union[str, List[str], Set[str]]] = None) -> List[Dict[str, Any]]:
        lib_ids = self._normalize_library_ids(library_id)
        with self._get_connection() as conn:
            rows = self._query_with_library_filter(
                conn, "SELECT dto_json FROM books", lib_ids,
                order_by="number_sort ASC, name ASC, id ASC"
            )
            return self._parse_dto_rows(rows)

    def get_latest_books(self, library_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if library_id:
                rows = conn.execute(
                    "SELECT dto_json FROM books WHERE library_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (str(library_id), limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dto_json FROM books ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return self._parse_dto_rows(rows)

    def search_books(self, query: str, library_id: Optional[Union[str, List[str], Set[str]]] = None) -> List[Dict[str, Any]]:
        term = f"%{query.strip()}%"
        lib_ids = self._normalize_library_ids(library_id)
        with self._get_connection() as conn:
            rows = self._query_with_library_filter(
                conn, "SELECT dto_json FROM books", lib_ids,
                extra_where="(name LIKE ? OR dto_json LIKE ?)",
                extra_params=[term, term],
                order_by="name ASC, id ASC"
            )
            return self._parse_dto_rows(rows)

    def get_books_with_release_date(self, library_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if library_id:
                rows = conn.execute("SELECT dto_json FROM books WHERE library_id = ?", (str(library_id),)).fetchall()
            else:
                rows = conn.execute("SELECT dto_json FROM books").fetchall()
            result = []
            for r in rows:
                try:
                    book = json.loads(r["dto_json"])
                    rd = book.get("metadata", {}).get("releaseDate")
                    if rd and str(rd).strip() not in ("", "null", "None"):
                        result.append(book)
                except Exception:
                    pass
            return result

    # --- Pages & Dimensions Operations ---
    def save_book_pages(self, book_id: str, pages: List[Dict[str, Any]], pages_count: int):
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO book_pages (book_id, pages_json, pages_count, updated_at) VALUES (?, ?, ?, ?)",
                (str(book_id), json.dumps(pages), pages_count, now)
            )

    def get_book_pages(self, book_id: str) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        b_id = str(book_id)
        candidates = [b_id]
        from urllib.parse import unquote
        cur = b_id
        for _ in range(3):
            dec = unquote(cur)
            if dec == cur:
                break
            if dec not in candidates:
                candidates.append(dec)
            cur = dec

        with self._get_connection() as conn:
            for cid in candidates:
                row = conn.execute("SELECT pages_json, pages_count FROM book_pages WHERE book_id = ?", (cid,)).fetchone()
                if row:
                    try:
                        return json.loads(row["pages_json"]), int(row["pages_count"])
                    except Exception:
                        pass
        return None

    def get_book_page_count(self, book_id: str) -> Optional[int]:
        b_id = str(book_id)
        candidates = [b_id]
        from urllib.parse import unquote
        cur = b_id
        for _ in range(3):
            dec = unquote(cur)
            if dec == cur:
                break
            if dec not in candidates:
                candidates.append(dec)
            cur = dec

        with self._get_connection() as conn:
            for cid in candidates:
                row = conn.execute("SELECT pages_count FROM book_pages WHERE book_id = ?", (cid,)).fetchone()
                if row:
                    return int(row["pages_count"])
                row_book = conn.execute("SELECT pages_count FROM books WHERE id = ?", (cid,)).fetchone()
                if row_book and row_book["pages_count"] and row_book["pages_count"] > 1:
                    return int(row_book["pages_count"])
        return None

    # --- Read Progress Operations ---
    def save_read_progress(self, user: str, book_id: str, page: int, completed: bool, read_date: str, dto: Dict[str, Any]):
        now = time.time()
        u = str(user or "default").lower().strip()
        comp_val = 1 if completed else 0
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO read_progress (user, book_id, page, completed, read_date, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (u, str(book_id), page, comp_val, read_date, json.dumps(dto), now)
            )

    def get_read_progress(self, user: str, book_id: str) -> Optional[Dict[str, Any]]:
        """Get read progress for a specific user and book."""
        with self._get_connection() as conn:
            u = str(user or "default").lower().strip()
            row = conn.execute(
                "SELECT dto_json FROM read_progress WHERE user = ? AND book_id = ?",
                (u, str(book_id))
            ).fetchone()
            if not row and u == "testuser":
                row = conn.execute(
                    "SELECT dto_json FROM read_progress WHERE user = 'default' AND book_id = ?",
                    (str(book_id),)
                ).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
        return None

    def get_all_in_progress(self, user: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
        with self._get_connection() as conn:
            if user:
                u = str(user).lower().strip()
                if u == "testuser":
                    rows = conn.execute(
                        """SELECT book_id, dto_json, updated_at FROM read_progress 
                           WHERE user = 'testuser' AND completed = 0 AND page > 0 
                           UNION 
                           SELECT book_id, dto_json, updated_at FROM read_progress 
                           WHERE user = 'default' AND completed = 0 AND page > 0 
                             AND book_id NOT IN (SELECT book_id FROM read_progress WHERE user = 'testuser')
                           ORDER BY updated_at DESC"""
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT book_id, dto_json, updated_at FROM read_progress WHERE user = ? AND completed = 0 AND page > 0 ORDER BY updated_at DESC",
                        (u,)
                    ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT book_id, dto_json, updated_at FROM read_progress WHERE completed = 0 AND page > 0 ORDER BY updated_at DESC"
                ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append((str(r["book_id"]), json.loads(r["dto_json"])))
                except Exception:
                    pass
            return result

    def get_all_read_progress_map(self, user: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        with self._get_connection() as conn:
            if user:
                u = str(user).lower().strip()
                if u == "testuser":
                    rows = conn.execute(
                        """SELECT book_id, dto_json FROM read_progress WHERE user = 'testuser'
                           UNION
                           SELECT book_id, dto_json FROM read_progress WHERE user = 'default'
                             AND book_id NOT IN (SELECT book_id FROM read_progress WHERE user = 'testuser')"""
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT book_id, dto_json FROM read_progress WHERE user = ?", (u,)).fetchall()
                result = {}
                for r in rows:
                    try:
                        result[str(r["book_id"])] = json.loads(r["dto_json"])
                    except Exception:
                        pass
                return result
            else:
                rows = conn.execute("SELECT user, book_id, dto_json FROM read_progress").fetchall()
                result = {}
                for r in rows:
                    try:
                        key = f"{r['user']}:{r['book_id']}"
                        result[key] = json.loads(r["dto_json"])
                        if r["user"] == "default":
                            result[str(r["book_id"])] = json.loads(r["dto_json"])
                    except Exception:
                        pass
                return result

    def delete_read_progress(self, user: str, book_id: str):
        u = str(user or "default").lower().strip()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM read_progress WHERE user = ? AND book_id = ?", (u, str(book_id)))

    def get_active_series_ids(self, user: Optional[str] = None) -> List[str]:
        with self._get_connection() as conn:
            if user:
                u = str(user).lower().strip()
                if u == "testuser":
                    rows = conn.execute(
                        "SELECT DISTINCT series_id FROM books WHERE id IN (SELECT book_id FROM read_progress WHERE user = 'testuser' OR user = 'default') AND series_id != ''"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT DISTINCT series_id FROM books WHERE id IN (SELECT book_id FROM read_progress WHERE user = ?) AND series_id != ''",
                        (u,)
                    ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT series_id FROM books WHERE id IN (SELECT book_id FROM read_progress) AND series_id != ''"
                ).fetchall()
            return [str(r["series_id"]) for r in rows if r["series_id"]]

    def get_all_read_book_ids(self, user: Optional[str] = None) -> Set[str]:
        with self._get_connection() as conn:
            if user:
                u = str(user).lower().strip()
                if u == "testuser":
                    rows = conn.execute(
                        """SELECT book_id FROM read_progress WHERE user = 'testuser' AND completed = 1
                           UNION
                           SELECT book_id FROM read_progress WHERE user = 'default' AND completed = 1
                             AND book_id NOT IN (SELECT book_id FROM read_progress WHERE user = 'testuser')"""
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT book_id FROM read_progress WHERE user = ? AND completed = 1", (u,)).fetchall()
            else:
                rows = conn.execute("SELECT book_id FROM read_progress WHERE completed = 1").fetchall()
            return {str(r["book_id"]) for r in rows}

    # --- User Authentication & Sync Storage ---
    def save_user(self, username: str, password: str):
        if not username or not password:
            return
        from app.config import settings
        sync_u = (settings.SYNC_USERNAME or "").strip().lower()
        u = str(username).lower().strip()
        if sync_u and u == sync_u:
            return  # Do not store sync user as a reader user
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, password, last_login, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET password=excluded.password, last_login=excluded.last_login",
                (u, str(password), now, now)
            )

    def get_all_users(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT username, password, last_login, created_at FROM users").fetchall()
            return [
                {
                    "username": str(r["username"]),
                    "password": str(r["password"]),
                    "last_login": r["last_login"],
                    "created_at": r["created_at"]
                }
                for r in rows
            ]

    def delete_user_data(self, username: str):
        u = str(username).lower().strip()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (u,))
            conn.execute("DELETE FROM read_progress WHERE user = ?", (u,))

    def clear_all(self):
        with self._get_connection() as conn:
            conn.executescript("""
                DELETE FROM series;
                DELETE FROM books;
                DELETE FROM book_pages;
                DELETE FROM read_progress;
                DELETE FROM libraries;
                DELETE FROM users;
            """)

db = Database()
