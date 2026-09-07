"""One-time availability merger for RosterBuddy and Otto Sport player exports."""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from .pipeline import clean, ensure_private_output_file, write_csv

OTTO_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
AVAILABILITY_CODES = {
    "E": "Available early only - 5:30 pm to 7:30 pm",
    "L": "Available late only - 7:00 pm to 9:00 pm",
    "O": "Available either slot",
    "X": "Unavailable",
}


def parse_rosterbuddy_availability(value: str) -> dict[str, str]:
    codes = [code.strip().upper() for code in value.split(",")]
    if len(codes) != len(OTTO_DAYS) or any(code not in AVAILABILITY_CODES for code in codes):
        raise ValueError(
            "RosterBuddy Availability must contain five comma-separated E, L, O, or X codes"
        )
    return dict(zip(OTTO_DAYS, (AVAILABILITY_CODES[code] for code in codes), strict=True))


def read_raw_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header row")
        return reader.fieldnames, [{key: "" if value is None else value for key, value in row.items()} for row in reader]


def merge_availability(
    players_path: Path,
    rosterbuddy_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, int]:
    player_fields, players = read_raw_csv(players_path)
    rosterbuddy_fields, rosterbuddy_rows = read_raw_csv(rosterbuddy_path)
    required_player_fields = {"Reg Number", *OTTO_DAYS}
    missing_player_fields = required_player_fields - set(player_fields)
    if missing_player_fields:
        raise ValueError(f"Otto players export is missing columns: {', '.join(sorted(missing_player_fields))}")
    for field in ("RecordID", "Availability"):
        if field not in rosterbuddy_fields:
            raise ValueError(f"RosterBuddy export is missing column: {field}")

    rb_by_id: dict[str, list[dict[str, str]]] = {}
    for row in rosterbuddy_rows:
        rb_by_id.setdefault(row["RecordID"], []).append(row)
    player_id_counts = Counter(row["Reg Number"] for row in players)
    report: list[dict[str, str]] = []
    updated_rows: list[dict[str, str]] = []
    matched_ids: set[str] = set()

    for player in players:
        reg_number = player["Reg Number"]
        matches = rb_by_id.get(reg_number, [])
        row = dict(player)
        if not reg_number:
            status, detail = "INVALID_OTTO_ID", "Otto row has no Reg Number; availability was retained."
        elif player_id_counts[reg_number] > 1:
            status, detail = "DUPLICATE_OTTO_ID", "Otto Reg Number occurs more than once; availability was retained."
        elif not matches:
            status, detail = "OTTO_ONLY", "No RosterBuddy RecordID match; availability was retained."
        elif len(matches) > 1:
            status, detail = "DUPLICATE_ROSTERBUDDY_ID", "RosterBuddy RecordID occurs more than once; availability was retained."
        else:
            try:
                row.update(parse_rosterbuddy_availability(matches[0]["Availability"]))
                status, detail = "UPDATED", "Availability was replaced from RosterBuddy E/L/O/X codes."
                matched_ids.add(reg_number)
            except ValueError as error:
                status, detail = "INVALID_ROSTERBUDDY_AVAILABILITY", f"{error}; availability was retained."
        updated_rows.append(row)
        report.append({
            "status": status,
            "reg_number": reg_number,
            "record_id": matches[0]["RecordID"] if len(matches) == 1 else "",
            "player_name": " ".join((player.get("Player First Name", ""), player.get("Player Last Name", ""))).strip(),
            "detail": detail,
        })

    for record_id, rows in sorted(rb_by_id.items()):
        if record_id not in matched_ids and record_id not in player_id_counts:
            report.append({
                "status": "ROSTERBUDDY_ONLY",
                "reg_number": "",
                "record_id": record_id,
                "player_name": " ".join((rows[0].get("FirstName", ""), rows[0].get("LastName", ""))).strip(),
                "detail": "No Otto Reg Number match; likely withdrawal and not added to updated players.",
            })

    write_csv(output_path, player_fields, updated_rows)
    write_csv(report_path, ["status", "reg_number", "record_id", "player_name", "detail"], report)
    return dict(Counter(row["status"] for row in report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Otto player availability from a RosterBuddy export.")
    parser.add_argument("--input-dir", type=Path, default=Path("../otto-data"))
    parser.add_argument("--rosterbuddy-file", type=Path)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args(argv)
    rosterbuddy_file = args.rosterbuddy_file or args.input_dir / "rosterbuddy" / "Player-For-Mail-Merge.csv"
    output_file = args.output_file or args.input_dir / "updated-players.csv"
    report_file = args.report_file or args.input_dir / "updated-players-merge-report.csv"
    try:
        summary = merge_availability(
            args.input_dir / "players.csv",
            rosterbuddy_file,
            output_file,
            report_file,
        )
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print("Availability merge complete: " + ", ".join(f"{status}={count}" for status, count in sorted(summary.items())))
    return 0
