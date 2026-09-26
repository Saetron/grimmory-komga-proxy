import asyncio
import logging
import time
from typing import Dict, Any, Optional, Tuple
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

    def get_sync_credentials(self) -> Tuple[str, str]:
        """Get dedicated background sync credentials. Strictly used for background sync."""
        u = settings.SYNC_USERNAME or settings.DEFAULT_USERNAME
        p = settings.SYNC_PASSWORD or settings.DEFAULT_PASSWORD
        return u.strip(), p.strip()

    def maybe_trigger_sync_on_login(self, user: str, pwd: str):
        """If no successful background sync has completed yet, trigger it now.
        Always prefers dedicated sync credentials if configured.
        """
        if not self.has_synced_successfully and not self.is_syncing:
            sync_user, sync_pwd = self.get_sync_credentials()
            if sync_user and sync_pwd:
                logger.info(f"[BackgroundSync] Triggering background sync using dedicated sync user '{sync_user}'...")
                asyncio.create_task(self.run_full_sync(user=sync_user, pwd=sync_pwd))
            elif user and pwd:
                logger.info(f"[BackgroundSync] Authenticated user '{user}' detected (no dedicated sync user configured). Starting initial background sync...")
                asyncio.create_task(self.run_full_sync(user=user, pwd=pwd))

    async def run_full_sync(self, user: Optional[str] = None, pwd: Optional[str] = None) -> Dict[str, Any]:
        """Perform a full check of Grimmory and validate/update the SQLite cache."""
        if self.is_syncing:
            logger.info("[BackgroundSync] Sync already in progress, skipping duplicate run.")
            return {"status": "in_progress", "message": "Sync already in progress"}

        # 1. Resolve credentials (dedicated background sync credentials)
        if not user or not pwd:
            user, pwd = self.get_sync_credentials()
        elif user and pwd and user.lower() != (settings.SYNC_USERNAME or "").strip().lower():
            db.save_user(user, pwd)

        if not user or not pwd:
            logger.info(
                "[BackgroundSync] No sync credentials configured (SYNC_USERNAME / SYNC_PASSWORD). "
                "Background sync paused until credentials are provided in environment or a user connects via Komic."
            )
            return {
                "status": "skipped",
                "message": "No Grimmory credentials configured yet (SYNC_USERNAME / SYNC_PASSWORD)"
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
            libs = await grimmory_client.get_libraries(user, pwd)
            user_libs = await grimmory_client.get_user_library_ids(user, pwd)
            if user_libs is None and user:
                logger.error(
                    f"[BackgroundSync] Authentication failed connecting to Grimmory with user '{user}'. "
                    f"Please verify GRIMMORY_USERNAME and GRIMMORY_PASSWORD (or USER_MAPPING) match a valid Grimmory account."
                )
                stats["status"] = "unauthorized"
                stats["error"] = f"HTTP 401 Unauthorized for user '{user}'"
                self.last_sync_stats = stats
                return stats

            # 3. Sync all series and books from Grimmory catalog in bulk (pure metadata, zero archive unpacking)
            all_books = await grimmory_client.fetch_and_cache_native_books(user, pwd)
            all_series = await grimmory_client.get_all_series(user, pwd)
            stats["seriesCount"] = len(all_series)
            if all_series:
                db.save_series_batch(all_series)

            total_books_synced = len(all_books) if all_books else len(db.get_all_books())
            stats["booksCount"] = total_books_synced
            logger.info(
                f"[BackgroundSync] Bulk catalog sync complete: {stats['seriesCount']} series and "
                f"{total_books_synced} books indexed into SQLite without unpacking archive files."
            )

            # 4. Sync Read Progress for All Logged-in Users
            user_sync_stats = await self.sync_all_users_reading_state()
            stats["userSync"] = user_sync_stats
            stats["readProgressCount"] = user_sync_stats.get("syncedProgressCount", 0)
            duration = round(time.time() - start_time, 2)
            stats["durationSeconds"] = duration
            self.last_sync_time = time.time()
            self.last_sync_stats = stats
            self.has_synced_successfully = True
            logger.info(
                f"[BackgroundSync] Full sync complete in {duration}s: "
                f"{stats['seriesCount']} series, {stats['booksCount']} books, {stats['readProgressCount']} in-progress."
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

    async def sync_all_users_reading_state(self) -> Dict[str, Any]:
        """Iterate over all stored users and sync their reading progress in the background.
        If a stored user returns 401 Unauthorized, automatically purge their credentials and reading progress.
        """
        users = db.get_all_users()
        if not users:
            return {"syncedUsers": 0, "deletedUsers": 0, "syncedProgressCount": 0}

        sync_u = (settings.SYNC_USERNAME or "").strip().lower()
        synced_users = 0
        deleted_users = 0

        for u_record in users:
            u_name = u_record["username"]
            u_pwd = u_record["password"]

            if sync_u and u_name.lower() == sync_u.lower():
                continue

            success = await grimmory_client.sync_user_reading_state(u_name, u_pwd)
            if not success:
                logger.warning(
                    f"[BackgroundSync] Authentication failed (401 Unauthorized) for saved user '{u_name}'. "
                    f"Deleting user credentials and cached reading progress."
                )
                db.delete_user_data(u_name)
                grimmory_client.evict_user_cache(u_name, u_pwd)
                deleted_users += 1
            else:
                synced_users += 1
                logger.info(f"[BackgroundSync] Successfully synced reading state for user '{u_name}'.")

        return {
            "syncedUsers": synced_users,
            "deletedUsers": deleted_users,
            "syncedProgressCount": len(db.get_all_in_progress())
        }

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
