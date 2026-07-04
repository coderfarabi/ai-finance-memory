import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

# Initialize the server
mcp = FastMCP("ai-finance-memory-db")

# Define database file path in the project root
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transactions.json")

def _read_db() -> list[dict[str, Any]]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error reading DB: {e}\n")
        return []

def _write_db(data: list[dict[str, Any]]):
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"Error writing DB: {e}\n")

@mcp.tool()
def save_transaction(
    date: str,
    type: str,
    amount: float,
    category: str,
    details: str,
    contact: str | None = "None",
    due_status: str | None = "None"
) -> str:
    """
    Saves a new financial transaction record to the persistent JSON database.

    Args:
        date: The transaction date in YYYY-MM-DD format.
        type: The type of transaction, must be 'Income' or 'Expense'.
        amount: The monetary value of the transaction.
        category: The category (e.g. Food, Groceries, Salary, Loan, Travel).
        details: A description of the transaction.
        contact: The person involved, or 'None' if none.
        due_status: Whether money is owed/expected: 'None', 'I Will Receive', 'I Will Pay'.
    """
    db = _read_db()
    tx_id = len(db) + 1
    new_tx = {
        "id": tx_id,
        "date": date,
        "type": type,
        "amount": amount,
        "category": category,
        "details": details,
        "contact": contact,
        "due_status": due_status
    }
    db.append(new_tx)
    _write_db(db)
    return f"Success: Transaction saved to database with ID {tx_id}."

@mcp.tool()
def list_transactions(type_filter: str | None = None) -> list[dict[str, Any]]:
    """
    Retrieves all transactions from the database, optionally filtered by type.

    Args:
        type_filter: Optional filter ('Income' or 'Expense').
    """
    db = _read_db()
    if type_filter:
        db = [tx for tx in db if tx.get("type", "").lower() == type_filter.lower()]
    return db

@mcp.tool()
def get_db_summary() -> dict[str, Any]:
    """
    Computes summary metrics: total income, total expense, net balance, and transaction count.
    """
    db = _read_db()
    total_income = sum(tx.get("amount", 0.0) for tx in db if tx.get("type") == "Income")
    total_expense = sum(tx.get("amount", 0.0) for tx in db if tx.get("type") == "Expense")
    balance = total_income - total_expense
    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "net_balance": balance,
        "total_transactions": len(db)
    }

if __name__ == "__main__":
    mcp.run()
