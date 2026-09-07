"""Merge repeatable practice-availability form responses into an Otto player export."""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

from .rosterbuddy_merge import OTTO_DAYS, read_raw_csv
from .pipeline import write_csv

UUID_FIELD = "UUID"
TIMESTAMP_FIELD = "Registration Date/Time"
TIMESTAMP_FORMAT = "%m/%d/%Y %H:%M:%S"
VALID_AVAILABILITY_VALUES = frozenset({
    "Available early only - 5:30 pm to 7:30 pm",
    "Available late only - 7:00 pm to 9:00 pm",
    "Available either slot",
    "Unavailable",
})


def parse_registration_datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), TIMESTAMP_FORMAT)
    except ValueError as error:
        raise ValueError(
            f"{TIMESTAMP_FIELD} must use {TIMESTAMP_FORMAT!r}"
        ) from error


def validate_availability_row(row: dict[str, str]) -> dict[str, str]:
    values = {day: row[day].strip() for day in OTTO_DAYS}
    invalid_days = [day for day, value in values.items() if value not in VALID_AVAILABILITY_VALUES]
    if invalid_days:
        raise ValueError(
            "Invalid availability for "
            + ", ".join(invalid_days)
            + "; each day must use an availability value from the form."
        )
    return values


def merge_latest_availability(
    players_path: Path,
    updates_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, int]:
    canonical_path = players_path.resolve()
    if canonical_path in {output_path.resolve(), report_path.resolve()}:
        raise ValueError("Output and report files must differ from the canonical players file.")
    if output_path.resolve() == report_path.resolve():
        raise ValueError("Output and report files must differ from each other.")

    player_fields, players = read_raw_csv(players_path)
    update_fields, updates = read_raw_csv(updates_path)
    required_player_fields = {UUID_FIELD, *OTTO_DAYS}
    required_update_fields = {UUID_FIELD, TIMESTAMP_FIELD, *OTTO_DAYS}
    missing_player_fields = required_player_fields - set(player_fields)
    missing_update_fields = required_update_fields - set(update_fields)
    if missing_player_fields:
        raise ValueError(
            f"Otto players export is missing columns: {', '.join(sorted(missing_player_fields))}"
        )
    if missing_update_fields:
        raise ValueError(
            f"Availability updates export is missing columns: {', '.join(sorted(missing_update_fields))}"
        )

    player_ids = Counter(player[UUID_FIELD].strip() for player in players)
    valid_updates: dict[str, list[tuple[int, datetime, dict[str, str]]]] = {}
    report: list[dict[str, str]] = []
    selected_updates: dict[str, dict[str, str]] = {}

    for index, update in enumerate(updates, start=2):
        uuid = update[UUID_FIELD].strip()
        timestamp = update[TIMESTAMP_FIELD].strip()
        if not uuid:
            status, detail = "INVALID_UUID", "Availability update has no UUID; it was ignored."
        elif uuid not in player_ids:
            status, detail = "UNKNOWN_UUID", "UUID is not present in the canonical players file; it was ignored."
        elif player_ids[uuid] > 1:
            status, detail = "DUPLICATE_PLAYER_UUID", "Canonical players file has this UUID more than once; it was ignored."
        else:
            try:
                parsed_timestamp = parse_registration_datetime(timestamp)
                availability = validate_availability_row(update)
            except ValueError as error:
                status, detail = "INVALID_UPDATE", f"{error}; it was ignored."
            else:
                valid_updates.setdefault(uuid, []).append((index, parsed_timestamp, availability))
                continue
        report.append({
            "status": status,
            "uuid": uuid,
            "registration_date_time": timestamp,
            "source_row": str(index),
            "player_name": "",
            "detail": detail,
        })

    for uuid, candidates in valid_updates.items():
        latest_timestamp = max(timestamp for _, timestamp, _ in candidates)
        latest = [candidate for candidate in candidates if candidate[1] == latest_timestamp]
        if len(latest) > 1:
            for index, timestamp, _ in latest:
                report.append({
                    "status": "TIMESTAMP_TIE",
                    "uuid": uuid,
                    "registration_date_time": timestamp.strftime(TIMESTAMP_FORMAT),
                    "source_row": str(index),
                    "player_name": "",
                    "detail": "Multiple valid updates share the latest timestamp; availability was retained.",
                })
            for index, timestamp, _ in candidates:
                if timestamp != latest_timestamp:
                    report.append({
                        "status": "SUPERSEDED",
                        "uuid": uuid,
                        "registration_date_time": timestamp.strftime(TIMESTAMP_FORMAT),
                        "source_row": str(index),
                        "player_name": "",
                        "detail": "A later update has an ambiguous timestamp; availability was retained.",
                    })
            continue

        selected_index, selected_timestamp, selected_availability = latest[0]
        selected_updates[uuid] = selected_availability
        for index, timestamp, _ in candidates:
            status = "UPDATED" if index == selected_index else "SUPERSEDED"
            detail = (
                "Latest valid availability update was applied."
                if status == "UPDATED"
                else "A later valid availability update was applied."
            )
            report.append({
                "status": status,
                "uuid": uuid,
                "registration_date_time": timestamp.strftime(TIMESTAMP_FORMAT),
                "source_row": str(index),
                "player_name": "",
                "detail": detail,
            })

    updated_rows: list[dict[str, str]] = []
    names_by_uuid: dict[str, str] = {}
    for player in players:
        uuid = player[UUID_FIELD].strip()
        row = dict(player)
        player_name = " ".join(
            (player.get("Player First Name", ""), player.get("Player Last Name", ""))
        ).strip()
        names_by_uuid[uuid] = player_name
        if uuid in selected_updates:
            row.update(selected_updates[uuid])
        updated_rows.append(row)

    for row in report:
        row["player_name"] = names_by_uuid.get(row["uuid"], "")

    report.sort(key=lambda row: int(row["source_row"]))
    write_csv(output_path, player_fields, updated_rows)
    write_csv(
        report_path,
        ["status", "uuid", "registration_date_time", "source_row", "player_name", "detail"],
        report,
    )
    return dict(Counter(row["status"] for row in report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the latest valid practice-availability form response for each player UUID."
    )
    parser.add_argument("--input-dir", type=Path, default=Path("../otto-data"))
    parser.add_argument("--players-file", type=Path)
    parser.add_argument("--updates-file", type=Path)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args(argv)
    players_file = args.players_file or args.input_dir / "updated-players.csv"
    updates_file = args.updates_file or args.input_dir / "availability-updates.csv"
    output_file = args.output_file or args.input_dir / "updated-players-availability.csv"
    report_file = args.report_file or args.input_dir / "updated-players-availability-report.csv"
    try:
        summary = merge_latest_availability(players_file, updates_file, output_file, report_file)
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print(
        "Availability update merge complete: "
        + ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
    )
    return 0
