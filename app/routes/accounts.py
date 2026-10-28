import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from .. import service
from ..db import get_conn
from ..models import (
    Account,
    AccountCreate,
    Balance,
    Transaction,
    TransactionCreate,
    TransactionPage,
    TxType,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=Account, status_code=201, summary="Create an account (balance starts at 0)")
def create_account(body: AccountCreate, conn: sqlite3.Connection = Depends(get_conn)):
    return service.create_account(conn, body.name)


@router.get("/{account_id}", response_model=Account, summary="Get an account")
def get_account(account_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_account(conn, account_id)


@router.get("/{account_id}/balance", response_model=Balance, summary="Get the current balance")
def get_balance(account_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_balance(conn, account_id)


@router.post(
    "/{account_id}/transactions",
    response_model=Transaction,
    status_code=201,
    summary="Add a credit or debit transaction",
)
def add_transaction(
    account_id: str, body: TransactionCreate, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.add_transaction(conn, account_id, body)


@router.get(
    "/{account_id}/transactions",
    response_model=TransactionPage,
    summary="Transaction history, latest first, filterable and paginated",
)
def list_transactions(
    account_id: str,
    type: Optional[TxType] = Query(None, description="credit or debit; omit for both"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return service.list_transactions(conn, account_id, type, page, limit)
