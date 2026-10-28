import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match


class APIError(Exception):
    """Domain error rendered as {"error": {"code", "message", "details"}}."""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details


def not_found(account_id: str) -> APIError:
    return APIError(404, "ACCOUNT_NOT_FOUND", f"Account '{account_id}' does not exist.")


def _body(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


# A validation failure on one of these fields gets its own specific error code.
_FIELD_CODES = {
    "type": "INVALID_TRANSACTION_TYPE",
    "amount": "INVALID_AMOUNT",
    "page": "INVALID_PAGINATION",
    "limit": "INVALID_PAGINATION",
}


def _validation_code(errors: list) -> str:
    # Field-specific only when every error maps to that same code; anything mixed
    # (e.g. bad amount AND missing reference_id) is a generic VALIDATION_ERROR.
    codes = {_FIELD_CODES.get(str(e["loc"][-1])) if e.get("loc") else None for e in errors}
    code = codes.pop() if len(codes) == 1 else None
    return code or "VALIDATION_ERROR"


def _clean(errors: list) -> list:
    return [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body") or "body", "message": e["msg"]}
        for e in errors
    ]


def install(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(e["type"] == "json_invalid" for e in errors):
            return JSONResponse(
                _body("INVALID_REQUEST_BODY", "Request body is not valid JSON."), status_code=400
            )
        if any(e["type"] == "model_attributes_type" or e["loc"] == ("body",) for e in errors):
            return JSONResponse(
                _body("INVALID_REQUEST_BODY", "Request body must be a JSON object.", _clean(errors)),
                status_code=400,
            )
        return JSONResponse(
            _body(_validation_code(errors), "Request validation failed.", _clean(errors)),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        headers = getattr(exc, "headers", None)
        if exc.status_code == 405:
            # Starlette lists only the first matching route's methods; a 405 must
            # name every method the path supports.
            allowed = {
                m
                for route in request.app.routes
                if route.matches(request.scope)[0] != Match.NONE
                for m in getattr(route, "methods", None) or ()
            }
            headers = {"Allow": ", ".join(sorted(allowed))}
        return JSONResponse(
            _body(codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail)),
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy(_: Request, exc: sqlite3.OperationalError):
        # Write lock not acquired within busy_timeout: nothing was written, so the
        # client can safely retry (reference_id makes the retry idempotent).
        if "locked" in str(exc) or "busy" in str(exc):
            return JSONResponse(
                _body("SERVICE_BUSY", "The ledger is busy. Retry shortly."),
                status_code=503,
                headers={"Retry-After": "1"},
            )
        raise exc

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        return JSONResponse(_body("INTERNAL_ERROR", "An unexpected error occurred."), status_code=500)
