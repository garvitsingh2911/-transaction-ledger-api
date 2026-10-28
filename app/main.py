from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import errors
from .db import init_db
from .routes import accounts, transfers


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Transaction Ledger API",
    version="1.0.0",
    description="Accounts, credit/debit transactions, balances, history and transfers.",
    lifespan=lifespan,
)
errors.install(app)
app.include_router(accounts.router)
app.include_router(transfers.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
