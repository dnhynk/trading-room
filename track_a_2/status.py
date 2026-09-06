"""Read Track A-2 status without taking the writer lock or accessing credentials."""
import json
from decimal import Decimal as D
from pathlib import Path
import sqlite3

from track_a_2.settings import CONFIG, load, resolved_state_directory


def read(config_path=CONFIG):
    config = load(config_path)
    directory = resolved_state_directory(config)
    report = directory / "status.json"
    if report.exists():
        try:
            value = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("Track A-2 status report is unreadable") from None
        if not isinstance(value, dict) or value.get("track") != "A-2":
            raise RuntimeError("Track A-2 status report identity mismatch")
        return value
    database = directory / "a2-ledger.sqlite"
    if not database.exists():
        return dict(track="A-2", initialized=False, mode=config["mode"], status=config["status"])
    uri = "file:" + database.resolve().as_posix() + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError):
        raise RuntimeError("Track A-2 ledger is unreadable") from None
    finally:
        if connection is not None:
            connection.close()
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError("Track A-2 ledger identity mismatch")
    positions = {
        coin: str(sum((D(lot[0]) for lot in book.get("lots", [])), D(0)))
        for coin, book in state.get("books", {}).items()
        if book.get("lots")
    }
    active = sum(
        order.get("status") not in {
            "FILLED", "CANCELED", "NOT_TRIGGERED_CANCELED", "CANCELED_NO_ORDER",
            "CANCELED_LIMIT_PRICE_EXCEED", "CANCELED_UNDER_PRODUCT_UNIT", "REJECTED",
        }
        for order in state.get("orders", {}).values()
    )
    return dict(
        track="A-2", initialized=True, mode=config["mode"], status=config["status"],
        positions=positions, active_orders=active, halt=state.get("halt"),
        realized_krw=state.get("realized"), day_realized_krw=state.get("day_realized"),
        day_stops=state.get("day_stops", 0),
    )


def main():
    print(json.dumps(read(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
