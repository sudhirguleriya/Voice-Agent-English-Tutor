"""Voice Coach: always-on English speaking coach. Run with:  uvicorn app.main:app --port 8000"""
import logging

import openai
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import routes, ws
from .config import ROOT
from .llm import ProviderError, friendly
from .storage import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-coach")
FRONTEND = ROOT / "frontend"

init_db()
app = FastAPI(title="Voice Coach")
app.include_router(routes.router)
app.include_router(ws.router)


@app.exception_handler(openai.OpenAIError)
@app.exception_handler(ProviderError)
async def _provider_error(request: Request, e: Exception):
    log.error("provider error on %s: %s", request.url.path, e)
    return JSONResponse({"detail": friendly(e)}, status_code=502)


@app.exception_handler(Exception)
async def _any_error(request: Request, e: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"Server error: {e}"}, status_code=500)


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
