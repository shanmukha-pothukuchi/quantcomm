"""FloodAlpha web app: landing page, dashboard and stocks page over one small JSON API.

Pages
  /, /stocks   Markets app: price charts, the agents' analysis, Capital One first (web/markets.html)
  /about       project landing page (web/index.html)
  /dashboard   research dashboard: storm case study, live, bank exposure, market test, validation

API (all responses: {"success": bool, "data": ..., "error": str | null})
  /api/markets, /api/markets/{ticker}   stock list; one company's chart, signal and past storms
  /api/advice                           cached agent analysis + whether it is stale
  /api/advice/run                       POST: run the agents now (needs LLM_* in .env)
  /api/storms, /api/storms/{key}, /api/storms/{key}/layers/{name}
  /api/live, /api/live/alerts
  /api/banks, /api/market, /api/validation, /api/methodology
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from floodalpha.agent.llm import LLMError
from floodalpha.config import ROOT
from floodalpha.web import live, markets, research, stocks, storms

log = logging.getLogger("floodalpha.web")
WEB = ROOT / "web"


def ok(data: Any) -> dict:
    return {"success": True, "data": data, "error": None}


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": message}, status_code=status)


def _guard(fn: Callable[[], Any], what: str) -> Any:
    """Run a payload builder; unknown keys -> 404, anything else -> logged 500 with a readable message."""
    try:
        return ok(fn())
    except stocks.PricesMissing as exc:
        raise HTTPException(503, str(exc)) from exc
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
    def markets_app():
        return _page("markets.html")

    @app.get("/stocks", include_in_schema=False)
    def stocks_alias():
        return _page("markets.html")

    @app.get("/about", include_in_schema=False)
    def landing():
        return _page("index.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return _page("dashboard.html")

    @app.get("/media/{storm}/{name}", include_in_schema=False)
    def media(storm: str, name: str):
        if storm not in storms.store.storms_config() or name not in storms.PREVIEWS:
            raise HTTPException(404, "Unknown image.")
        path: Path = storms.store.data_dir() / storm / name
        if not path.exists():
            raise HTTPException(404, f"{name} not generated yet for {storm}.")
        return FileResponse(path, media_type="image/png")

    # -------------------------------------------------------------- api --
    @app.get("/api/markets")
    def api_markets():
        return _guard(lambda: markets.list_view(stocks.payload()), "market list")

    @app.get("/api/markets/{ticker}")
    def api_market_ticker(ticker: str):
        return _guard(lambda: markets.ticker_view(stocks.payload(), ticker), "ticker")

    @app.get("/api/advice")
    def api_advice():
        return _guard(lambda: markets.advice_view(stocks.payload()), "agent analysis")

    @app.post("/api/advice/run")
    def api_advice_run():
        try:
            return ok(markets.run_analysis(stocks.payload()))
        except markets.AnalysisBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        except stocks.PricesMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:  # the page shows a readable message; the cause goes to the log
            log.exception("agent run failed")
            raise HTTPException(500, f"Agent run failed unexpectedly ({type(exc).__name__}); see the server log.") from exc

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
