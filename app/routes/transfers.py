import sqlite3

from fastapi import APIRouter, Depends

from .. import service
from ..db import get_conn
from ..models import Transfer, TransferCreate

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.post("", response_model=Transfer, status_code=201, summary="Atomically move money between accounts")
def create_transfer(body: TransferCreate, conn: sqlite3.Connection = Depends(get_conn)):
    return service.transfer(conn, body)
