import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.config import settings
from app.grimmory_client import grimmory_client
from app.routers import auth, libraries, series, books, readlists

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger("grimmory-komga-bridge")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Grimmory-Komga Bridge pointing to: {settings.GRIMMORY_URL}")
    yield
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
        "docs": "/docs"
    }

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
