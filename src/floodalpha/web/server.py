"""FloodAlpha web app: landing page, dashboard and stocks page over one small JSON API.

Pages
  /            landing page (web/index.html)
  /dashboard   storm case study, live, bank exposure, market test, validation, methodology
  /stocks      bank / ETF / insurer / retail prices with each company's flood signal

API (all responses: {"success": bool, "data": ..., "error": str | null})
  /api/storms                     storm list with flood-map status
  /api/storms/{key}               case-study payload for one storm
  /api/storms/{key}/layers/{name} GeoJSON: polygons | coverage | track
  /api/live, /api/live/alerts     live summary and alert polygons
  /api/banks, /api/market, /api/validation, /api/methodology
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from floodalpha.config import ROOT
from floodalpha.web import live, research, stocks, storms

log = logging.getLogger("floodalpha.web")
WEB = ROOT / "web"
STOCKS_TEMPLATE = WEB / "stocks.html"


def ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": message}, status_code=status)


def _guard(fn: Callable[[], Any], what: str) -> Any:
    """Run a payload builder; unknown keys -> 404, anything else -> logged 500 with a readable message."""
    try:
        return ok(fn())
    except KeyError as exc:
        raise HTTPException(404, f"Unknown {what}: {exc.args[0] if exc.args else ''}") from exc
    except Exception as exc:  # noqa: BLE001 — log the real cause, show the user which output is broken
        log.exception("failed to build %s", what)
        raise HTTPException(500, f"Could not build {what} from the cached pipeline outputs ({type(exc).__name__}).") from exc


def _page(name: str) -> FileResponse:
    path = WEB / name
    if not path.exists():
        raise HTTPException(404, f"Page {name} is missing from web/.")
    return FileResponse(path, media_type="text/html")


def create_app() -> FastAPI:
    app = FastAPI(title="FloodAlpha", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return _fail(exc.status_code, str(exc.detail))
        return HTMLResponse(f"<!doctype html><title>FloodAlpha</title><body style='background:#050608;color:#f3f5f7;"
                            f"font-family:system-ui;padding:40px'><h1>{exc.status_code}</h1><p>{exc.detail}</p>"
                            f"<p><a style='color:#3fd6c6' href='/'>Back to FloodAlpha</a></p>", status_code=exc.status_code)

    # ------------------------------------------------------------ pages --
    @app.get("/", include_in_schema=False)
    def landing():
        return _page("index.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return _page("dashboard.html")

    @app.get("/stocks", include_in_schema=False, response_class=HTMLResponse)
    def stocks_page():
        if not STOCKS_TEMPLATE.exists():
            raise HTTPException(404, "Stocks template web/stocks.html is missing.")
        try:
            return HTMLResponse(stocks.render(STOCKS_TEMPLATE.read_text()))
        except stocks.PricesMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("stocks payload failed")
            raise HTTPException(500, f"Could not build the stocks view from cached outputs ({type(exc).__name__}).") from exc

    @app.get("/media/{storm}/{name}", include_in_schema=False)
    def media(storm: str, name: str):
        if storm not in storms.store.storms_config() or name not in storms.PREVIEWS:
            raise HTTPException(404, "Unknown image.")
        path: Path = storms.store.data_dir() / storm / name
        if not path.exists():
            raise HTTPException(404, f"{name} not generated yet for {storm}.")
        return FileResponse(path, media_type="image/png")

    # -------------------------------------------------------------- api --
    @app.get("/api/storms")
    def api_storms():
        return _guard(storms.storm_index, "storm list")

    @app.get("/api/storms/{key}")
    def api_storm(key: str):
        return _guard(lambda: storms.storm_case(key), "storm")

    @app.get("/api/storms/{key}/layers/{layer}")
    def api_layer(key: str, layer: str):
        return _guard(lambda: storms.storm_layer(key, layer), "layer")

    @app.get("/api/live")
    def api_live():
        return _guard(live.live_summary, "live summary")

    @app.get("/api/live/alerts")
    def api_alerts():
        return _guard(live.live_alerts, "alert polygons")

    @app.get("/api/banks")
    def api_banks(storm: str | None = None, definition: str | None = None):
        return _guard(lambda: research.bank_exposure(storm, definition), "bank exposure")

    @app.get("/api/market")
    def api_market():
        return _guard(research.market, "market test")

    @app.get("/api/validation")
    def api_validation():
        return _guard(research.validation, "validation")

    @app.get("/api/methodology")
    def api_methodology():
        return _guard(research.methodology, "methodology")

    return app


app = create_app()
