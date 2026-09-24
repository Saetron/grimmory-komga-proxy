import sqlite3
import json
import os
import time
from typing import Optional, Dict, Any, List, Tuple
from app.config import settings

class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DATABASE_PATH
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
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
                    book_id TEXT PRIMARY KEY,
                    page INTEGER,
                    completed INTEGER,
                    read_date TEXT,
                    dto_json TEXT,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_read_progress_completed ON read_progress (completed);
            """)

    # --- Series Operations ---
    def save_series(self, series_dto: Dict[str, Any]):
        s_id = str(series_dto.get("id"))
        lib_id = str(series_dto.get("libraryId", ""))
        name = str(series_dto.get("name") or series_dto.get("metadata", {}).get("title") or "")
        books_count = int(series_dto.get("booksCount", 0))
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
            s_id = str(s.get("id"))
            lib_id = str(s.get("libraryId", ""))
            name = str(s.get("name") or s.get("metadata", {}).get("title") or "")
            books_count = int(s.get("booksCount", 0))
            rows.append((s_id, lib_id, name, books_count, json.dumps(s), now))
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO series (id, library_id, name, books_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                rows
            )

    def get_series(self, series_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT dto_json FROM series WHERE id = ?", (str(series_id),)).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
        return None

    def get_all_series(self, library_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if library_id:
                rows = conn.execute("SELECT dto_json FROM series WHERE library_id = ? ORDER BY name ASC", (str(library_id),)).fetchall()
            else:
                rows = conn.execute("SELECT dto_json FROM series ORDER BY name ASC").fetchall()
            result = []
            for r in rows:
                try:
                    result.append(json.loads(r["dto_json"]))
                except Exception:
                    pass
            return result

    def search_series(self, query: str, library_id: Optional[str] = None) -> List[Dict[str, Any]]:
        term = f"%{query.strip()}%"
        with self._get_connection() as conn:
            if library_id:
                rows = conn.execute(
                    "SELECT dto_json FROM series WHERE library_id = ? AND (name LIKE ? OR dto_json LIKE ?) ORDER BY name ASC",
                    (str(library_id), term, term)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dto_json FROM series WHERE (name LIKE ? OR dto_json LIKE ?) ORDER BY name ASC",
                    (term, term)
                ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(json.loads(r["dto_json"]))
                except Exception:
                    pass
            return result

    # --- Books Operations ---
    def save_book(self, book_dto: Dict[str, Any]):
        b_id = str(book_dto.get("id"))
        s_id = str(book_dto.get("seriesId", ""))
        lib_id = str(book_dto.get("libraryId", ""))
        name = str(book_dto.get("name") or book_dto.get("metadata", {}).get("title") or "")
        try:
            num_sort = float(book_dto.get("metadata", {}).get("numberSort", book_dto.get("number", 1.0)))
        except Exception:
            num_sort = 1.0
        pages_count = int(book_dto.get("media", {}).get("pagesCount", 1))
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO books (id, series_id, library_id, name, number_sort, pages_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (b_id, s_id, lib_id, name, num_sort, pages_count, json.dumps(book_dto), now)
            )

    def save_books_batch(self, books_list: List[Dict[str, Any]]):
        now = time.time()
        rows = []
        for b in books_list:
            b_id = str(b.get("id"))
            s_id = str(b.get("seriesId", ""))
            lib_id = str(b.get("libraryId", ""))
            name = str(b.get("name") or b.get("metadata", {}).get("title") or "")
            try:
                num_sort = float(b.get("metadata", {}).get("numberSort", b.get("number", 1.0)))
            except Exception:
                num_sort = 1.0
            pages_count = int(b.get("media", {}).get("pagesCount", 1))
            rows.append((b_id, s_id, lib_id, name, num_sort, pages_count, json.dumps(b), now))
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO books (id, series_id, library_id, name, number_sort, pages_count, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows
            )

    def get_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT dto_json FROM books WHERE id = ?", (str(book_id),)).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
        return None

    def get_books_by_series(self, series_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT dto_json FROM books WHERE series_id = ? ORDER BY number_sort ASC",
                (str(series_id),)
            ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(json.loads(r["dto_json"]))
                except Exception:
                    pass
            return result

    def search_books(self, query: str, library_id: Optional[str] = None) -> List[Dict[str, Any]]:
        term = f"%{query.strip()}%"
        with self._get_connection() as conn:
            if library_id:
                rows = conn.execute(
                    "SELECT dto_json FROM books WHERE library_id = ? AND (name LIKE ? OR dto_json LIKE ?) ORDER BY name ASC",
                    (str(library_id), term, term)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dto_json FROM books WHERE (name LIKE ? OR dto_json LIKE ?) ORDER BY name ASC",
                    (term, term)
                ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append(json.loads(r["dto_json"]))
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
        with self._get_connection() as conn:
            row = conn.execute("SELECT pages_json, pages_count FROM book_pages WHERE book_id = ?", (str(book_id),)).fetchone()
            if row:
                try:
                    return json.loads(row["pages_json"]), int(row["pages_count"])
                except Exception:
                    pass
        return None

    def get_book_page_count(self, book_id: str) -> Optional[int]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT pages_count FROM book_pages WHERE book_id = ?", (str(book_id),)).fetchone()
            if row:
                return int(row["pages_count"])
            row_book = conn.execute("SELECT pages_count FROM books WHERE id = ?", (str(book_id),)).fetchone()
            if row_book and row_book["pages_count"] and row_book["pages_count"] > 1:
                return int(row_book["pages_count"])
        return None

    # --- Read Progress Operations ---
    def save_read_progress(self, book_id: str, page: int, completed: bool, read_date: str, dto: Dict[str, Any]):
        now = time.time()
        comp_val = 1 if completed else 0
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO read_progress (book_id, page, completed, read_date, dto_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(book_id), page, comp_val, read_date, json.dumps(dto), now)
            )

    def get_read_progress(self, book_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT dto_json FROM read_progress WHERE book_id = ?", (str(book_id),)).fetchone()
            if row:
                try:
                    return json.loads(row["dto_json"])
                except Exception:
                    pass
        return None

    def get_all_in_progress(self) -> List[Tuple[str, Dict[str, Any]]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT book_id, dto_json FROM read_progress WHERE completed = 0 AND page > 0 ORDER BY updated_at DESC"
            ).fetchall()
            result = []
            for r in rows:
                try:
                    result.append((str(r["book_id"]), json.loads(r["dto_json"])))
                except Exception:
                    pass
            return result

    def get_all_read_progress_map(self) -> Dict[str, Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT book_id, dto_json FROM read_progress").fetchall()
            result = {}
            for r in rows:
                try:
                    result[r["book_id"]] = json.loads(r["dto_json"])
                except Exception:
                    pass
            return result

    def delete_read_progress(self, book_id: str):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM read_progress WHERE book_id = ?", (str(book_id),))

    def clear_all(self):
        with self._get_connection() as conn:
            conn.executescript("""
                DELETE FROM series;
                DELETE FROM books;
                DELETE FROM book_pages;
                DELETE FROM read_progress;
            """)

db = Database()
