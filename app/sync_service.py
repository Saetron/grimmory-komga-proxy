import asyncio
import logging
import time
from typing import Dict, Any, Optional
from app.config import settings
from app.grimmory_client import grimmory_client
from app.db import db
from app.dto_utils import ensure_book_dto

logger = logging.getLogger("grimmory-komga-bridge")

class SyncService:
    def __init__(self):
        self.is_syncing: bool = False
        self.last_sync_time: Optional[float] = None
        self.last_sync_stats: Dict[str, Any] = {}
        self.has_synced_successfully: bool = False
        self._stop_event = asyncio.Event()

    def stop(self):
        self._stop_event.set()

    def maybe_trigger_sync_on_login(self, user: str, pwd: str):
        """If no successful background sync has completed yet, trigger it now with user's validated credentials."""
        if not self.has_synced_successfully and not self.is_syncing and user and pwd:
            logger.info(f"[BackgroundSync] Authenticated user '{user}' detected. Starting initial background sync...")
            asyncio.create_task(self.run_full_sync(user=user, pwd=pwd))

    async def run_full_sync(self, user: Optional[str] = None, pwd: Optional[str] = None) -> Dict[str, Any]:
        """Perform a full check of Grimmory and validate/update the SQLite cache."""
        if self.is_syncing:
            logger.info("[BackgroundSync] Sync already in progress, skipping duplicate run.")
            return {"status": "in_progress", "message": "Sync already in progress"}

        # 1. Resolve credentials
        if not user or not pwd:
            user, pwd = grimmory_client.extract_credentials(None)

        if not user or not pwd:
            logger.info(
                "[BackgroundSync] No Grimmory credentials configured (GRIMMORY_USERNAME / GRIMMORY_PASSWORD). "
                "Background sync paused until credentials are provided in environment or a user connects via Komic."
            )
            return {
                "status": "skipped",
                "message": "No Grimmory credentials configured yet"
            }

        self.is_syncing = True
        start_time = time.time()
        logger.info(f"[BackgroundSync] Starting full Grimmory database sync and cache validation for user '{user}'...")

        stats = {
            "status": "success",
            "seriesCount": 0,
            "booksCount": 0,
            "readProgressCount": 0,
            "durationSeconds": 0.0,
            "error": None
        }

        try:
            # 2. Verify authentication with Grimmory first
            auth_check = await grimmory_client.komga_request("GET", "/api/v1/libraries", user, pwd)
            if auth_check.status_code == 401:
                logger.error(
                    f"[BackgroundSync] Authentication failed (HTTP 401 Unauthorized) connecting to Grimmory with user '{user}'. "
                    f"Please verify GRIMMORY_USERNAME and GRIMMORY_PASSWORD (or USER_MAPPING) match a valid Grimmory account."
                )
                stats["status"] = "unauthorized"
                stats["error"] = f"HTTP 401 Unauthorized for user '{user}'"
                self.last_sync_stats = stats
                return stats
            elif auth_check.status_code >= 400:
                logger.warning(
                    f"[BackgroundSync] Grimmory returned HTTP {auth_check.status_code} while checking libraries. "
                    f"Sync may be partial or Grimmory may be temporarily unavailable."
                )

            # 3. Sync all series from Grimmory (Komga + custom disambiguated)
            all_series = await grimmory_client.get_all_series(user, pwd)
            stats["seriesCount"] = len(all_series)
            if all_series:
                db.save_series_batch(all_series)
            logger.info(f"[BackgroundSync] Validated {len(all_series)} series from Grimmory.")

            # 3. For each series, validate and sync books & page counts concurrently
            total_books_synced = 0
            completed_series_count = 0
            total_series = len(all_series)
            concurrency = max(1, settings.SYNC_CONCURRENCY)
            sem = asyncio.Semaphore(concurrency)
            lock = asyncio.Lock()

            async def _sync_single_series(idx: int, s: Dict[str, Any]):
                nonlocal total_books_synced, completed_series_count
                async with sem:
                    if self._stop_event.is_set():
                        return
                    s_id = str(s.get("id"))
                    s_name = s.get("name") or s.get("metadata", {}).get("title") or s_id
                    try:
                        if "-u-" in s_id:
                            series_books = await grimmory_client.get_series_books_custom(s_id, user, pwd)
                        else:
                            resp = await grimmory_client.komga_request("GET", f"/api/v1/series/{s_id}/books?size=500", user, pwd)
                            if resp.status_code == 200:
                                data = resp.json()
                                series_books = data.get("content", []) if isinstance(data, dict) else []
                            else:
                                series_books = []

                        if series_books:
                            await grimmory_client.enrich_books_page_count(series_books, user, pwd)
                            for b in series_books:
                                ensure_book_dto(b)
                            db.save_books_batch(series_books)

                        async with lock:
                            completed_series_count += 1
                            total_books_synced += len(series_books)
                            pct = int((completed_series_count / total_series) * 100) if total_series > 0 else 100
                            logger.info(
                                f"[BackgroundSync] Progress: [{completed_series_count}/{total_series}] ({pct}%) "
                                f"- Synced series '{s_name}' ({len(series_books)} books, total books: {total_books_synced})"
                            )
                    except Exception as err:
                        async with lock:
                            completed_series_count += 1
                            pct = int((completed_series_count / total_series) * 100) if total_series > 0 else 100
                            logger.warning(
                                f"[BackgroundSync] Progress: [{completed_series_count}/{total_series}] ({pct}%) "
                                f"- Error syncing series '{s_name}': {err}"
                            )

            if all_series:
                logger.info(f"[BackgroundSync] Starting concurrent sync of {total_series} series with concurrency={concurrency}...")
                await asyncio.gather(*[_sync_single_series(i, s) for i, s in enumerate(all_series, start=1)], return_exceptions=True)

            stats["booksCount"] = total_books_synced
            logger.info(f"[BackgroundSync] Validated {total_books_synced} books across all series.")

            # 4. Sync Read Progress & On Deck
            in_prog_count = 0
            try:
                native_headers = await grimmory_client.get_native_headers(user, pwd)
                cr_resp = await grimmory_client.client.get("/api/v1/app/books/continue-reading?size=100", headers=native_headers)
                if cr_resp.status_code == 200:
                    cr_data = cr_resp.json()
                    cr_books = cr_data if isinstance(cr_data, list) else cr_data.get("content", []) if isinstance(cr_data, dict) else []
                    for b in cr_books:
                        b_id = str(b.get("id"))
                        if b_id:
                            prog = await grimmory_client.get_read_progress(b_id, user, pwd)
                            if prog:
                                in_prog_count += 1
            except Exception as e:
                logger.debug(f"[BackgroundSync] Continue-reading sync note: {e}")

            stats["readProgressCount"] = in_prog_count
            duration = round(time.time() - start_time, 2)
            stats["durationSeconds"] = duration
            self.last_sync_time = time.time()
            self.last_sync_stats = stats
            self.has_synced_successfully = True
            logger.info(
                f"[BackgroundSync] Full sync complete in {duration}s: "
                f"{stats['seriesCount']} series, {stats['booksCount']} books, {in_prog_count} in-progress."
            )
            return stats

        except Exception as e:
            logger.warning(f"[BackgroundSync] Error during Grimmory full sync: {e}")
            stats["status"] = "error"
            stats["error"] = str(e)
            stats["durationSeconds"] = round(time.time() - start_time, 2)
            self.last_sync_stats = stats
            return stats
        finally:
            self.is_syncing = False

    async def run_periodic_sync(self):
        """Infinite loop executing full sync every SYNC_INTERVAL_MINUTES."""
        interval_minutes = settings.SYNC_INTERVAL_MINUTES
        if interval_minutes <= 0:
            logger.info("[BackgroundSync] Periodic sync is disabled (SYNC_INTERVAL_MINUTES <= 0).")
            return

        interval_seconds = interval_minutes * 60
        logger.info(f"[BackgroundSync] Periodic sync enabled. Running every {interval_minutes} minutes.")

        # Initial startup sync after short initial delay (10s) to let server bind ports
        if settings.SYNC_ON_STARTUP:
            try:
                await asyncio.sleep(10)
                if not self._stop_event.is_set():
                    await self.run_full_sync()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[BackgroundSync] Startup sync warning: {e}")

        while not self._stop_event.is_set():
            try:
                # Wait for interval or stop event
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
                break
            except asyncio.TimeoutError:
                try:
                    await self.run_full_sync()
                except Exception as e:
                    logger.warning(f"[BackgroundSync] Periodic sync cycle error: {e}")
            except asyncio.CancelledError:
                break

sync_service = SyncService()
