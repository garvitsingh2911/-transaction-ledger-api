from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from typing_extensions import Annotated

# Up to 10^13 - 0.01 in major units: far beyond any real balance, and well inside
# SQLite's signed 64-bit INTEGER once converted to minor units.
def _no_digit_separators(v):
    # Python's Decimal parses "1_000" as 1000; money input must be written plainly.
    if isinstance(v, str) and "_" in v:
        raise ValueError("amount must not contain '_' digit separators")
    return v


Amount = Annotated[
    Decimal,
    BeforeValidator(_no_digit_separators),
    Field(gt=0, max_digits=15, decimal_places=2, examples=[50.00]),
]
TRANSFER_REF_PREFIX = "transfer:"


class TxType(str, Enum):
    credit = "credit"
    debit = "debit"


class _Strict(BaseModel):
    # Unknown fields are rejected, so a client cannot sneak in `balance`, `id`,
    # `created_at`, etc. Strings are trimmed before validation.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountCreate(_Strict):
    name: str = Field(..., min_length=1, max_length=200, examples=["Asha Verma"])


class TransactionCreate(_Strict):
    type: TxType
    amount: Amount
    reference_id: str = Field(..., min_length=1, max_length=100, examples=["INV-1001"])
    description: Optional[str] = Field(None, max_length=500)

    @field_validator("reference_id")
    @classmethod
    def _not_reserved(cls, v: str):
        # Transfer legs use `transfer:<transfer_id>`; clients may not imitate them.
        if v.startswith(TRANSFER_REF_PREFIX):
            raise ValueError(f"the '{TRANSFER_REF_PREFIX}' prefix is reserved for transfers")
        return v


class TransferCreate(_Strict):
    from_account_id: str = Field(..., min_length=1)
    to_account_id: str = Field(..., min_length=1)
    amount: Amount
    reference_id: str = Field(..., min_length=1, max_length=100, examples=["TRF-2001"])
    description: Optional[str] = Field(None, max_length=500)

    @field_validator("to_account_id")
    @classmethod
    def _distinct(cls, v: str, info):
        if v == info.data.get("from_account_id"):
            raise ValueError("must differ from from_account_id")
        return v


class Account(BaseModel):
    id: str
    name: str
    balance: float
    created_at: str


class Balance(BaseModel):
    account_id: str
    balance: float


class Transaction(BaseModel):
    id: str
    account_id: str
    type: TxType
    amount: float
    description: Optional[str]
    reference_id: str
    transfer_id: Optional[str] = None
    created_at: str


class Pagination(BaseModel):
    page: int
    limit: int
    total_records: int
    total_pages: int
    has_next: bool
    has_prev: bool


class TransactionPage(BaseModel):
    data: List[Transaction]
    pagination: Pagination


class Transfer(BaseModel):
    id: str
    from_account_id: str
    to_account_id: str
    amount: float
    reference_id: str
    description: Optional[str]
    created_at: str
    debit_transaction_id: str
    credit_transaction_id: str
