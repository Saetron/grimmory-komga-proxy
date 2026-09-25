import logging
import asyncio
import time
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.config import settings
from app.grimmory_client import grimmory_client
from app.sync_service import sync_service
from app.routers import auth, libraries, series, books, readlists

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger("grimmory-komga-bridge")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Grimmory-Komga Bridge pointing to: {settings.GRIMMORY_URL}")
    sync_task = None
    if settings.SYNC_INTERVAL_MINUTES > 0:
        sync_task = asyncio.create_task(sync_service.run_periodic_sync())
    yield
    sync_service.stop()
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
    await grimmory_client.client.aclose()
    logger.info("Grimmory-Komga Bridge shut down.")

app = FastAPI(
    title="Grimmory-Komga Bridge",
    description="Translator service enabling Komic and other Komga clients to read books from Grimmory",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for cross-platform clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import collections
from datetime import datetime

recent_debug_logs = collections.deque(maxlen=300)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    method = request.method
    path = request.url.path
    query = request.url.query
    full_url = f"{path}?{query}" if query else path

    body_str = None
    if not path.startswith("/api/v1/debug") and method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            body_bytes = await request.body()
            if len(body_bytes) < 65536:
                body_str = body_bytes.decode("utf-8", errors="replace")
                if "password" in body_str.lower():
                    import re
                    body_str = re.sub(r'("password"\s*:\s*)"[^"]*"', r'\1"[REDACTED]"', body_str, flags=re.IGNORECASE)
            # Recreate receive stream
            async def receive():
                return {"type": "http.request", "body": body_bytes}
            request = Request(request.scope, receive=receive)
        except Exception:
            pass

    try:
        response = await call_next(request)
        duration_ms = round((time.time() - start_time) * 1000, 1)
        log_msg = f"{method} {full_url} -> {response.status_code} ({duration_ms}ms)"
        if body_str:
            log_msg += f" | Body: {body_str[:300]}"
        logger.info(log_msg)

        if not path.startswith("/api/v1/debug"):
            recent_debug_logs.append({
                "time": datetime.utcnow().isoformat() + "Z",
                "method": method,
                "url": full_url,
                "path": path,
                "status": response.status_code,
                "durationMs": duration_ms,
                "userAgent": request.headers.get("user-agent", ""),
                "body": body_str
            })
        return response
    except Exception as e:
        duration_ms = round((time.time() - start_time) * 1000, 1)
        logger.error(f"{method} {full_url} -> EXCEPTION: {e} ({duration_ms}ms)", exc_info=True)
        if not path.startswith("/api/v1/debug"):
            recent_debug_logs.append({
                "time": datetime.utcnow().isoformat() + "Z",
                "method": method,
                "url": full_url,
                "path": path,
                "status": 500,
                "durationMs": duration_ms,
                "userAgent": request.headers.get("user-agent", ""),
                "body": body_str,
                "error": str(e)
            })
        raise

# Include API Routers
app.include_router(auth.router)
app.include_router(libraries.router)
app.include_router(series.router)
app.include_router(books.router)
app.include_router(readlists.router)

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Grimmory-Komga Bridge",
        "version": "1.0.0",
        "grimmoryUrl": settings.GRIMMORY_URL,
        "syncIntervalMinutes": settings.SYNC_INTERVAL_MINUTES,
        "lastSync": sync_service.last_sync_stats,
        "docs": "/docs"
    }

@app.post("/api/v1/sync")
@app.get("/api/v1/sync")
async def trigger_sync(request: Request):
    auth_header = request.headers.get("Authorization")
    user, pwd = grimmory_client.extract_credentials(auth_header)
    stats = await sync_service.run_full_sync(user=user, pwd=pwd)
    return stats

@app.get("/api/v1/debug/logs")
async def get_debug_logs(limit: int = 100):
    logs = list(recent_debug_logs)
    return {"total": len(logs), "logs": logs[-limit:]}

@app.delete("/api/v1/debug/logs")
async def clear_debug_logs():
    recent_debug_logs.clear()
    return {"status": "cleared"}

# Fallback catch-all for any other /api/v1/* route
@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def catch_all_api_v1(path: str, request: Request):
    auth_header = request.headers.get("Authorization")
    user, pwd = grimmory_client.extract_credentials(auth_header)
    params = dict(request.query_params)
    method = request.method
    
    body = None
    if method in ["POST", "PUT", "PATCH"]:
        try:
            body = await request.json()
        except Exception:
            body = await request.body()

    resp = await grimmory_client.komga_request(
        method=method,
        path=f"/api/v1/{path}",
        user=user,
        pwd=pwd,
        params=params,
        json_data=body if isinstance(body, dict) else None
    )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, log_level=settings.LOG_LEVEL)
