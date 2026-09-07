from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from .pipeline import (
    ASSIGNED_PRACTICE_SLOT_FIELDS,
    Pipeline,
    assigned_practice_slot_rows,
    clean,
    load_config,
    players_with_no_friend_requests_met_count,
    read_csv,
    run_divisions,
    write_csv,
)


def practice_day_summary(teams: list[dict[str, str]]) -> str:
    days = (
        ("Monday", "M"),
        ("Tuesday", "T"),
        ("Wednesday", "W"),
        ("Thursday", "R"),
        ("Friday", "F"),
    )
    counts = {day: 0 for day, _ in days}
    for team in teams:
        if team["slot_id"]:
            counts[team["day"]] += 1
    return ", ".join(f"{label}: {counts[day]}" for day, label in days)


def load_projected_select_ids(path: Path, players_file: Path) -> frozenset[str]:
    projections = read_csv(path)
    if not projections or "reg_number" not in projections[0]:
        raise ValueError(f"{path} must include a reg_number column")
    ids = [clean(row["reg_number"]) for row in projections]
    if not all(ids):
        raise ValueError(f"{path} contains a blank reg_number")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} contains duplicate reg_number values")
    player_ids = {clean(row.get("Reg Number")) for row in read_csv(players_file)}
    unknown = sorted(set(ids) - player_ids)
    if unknown:
        raise ValueError(f"{path} contains registration numbers not found in {players_file}: {', '.join(unknown)}")
    return frozenset(ids)


def apply_forecast_team_counts(config: dict[str, object], path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        team_counts = json.load(fh)
    if not isinstance(team_counts, dict) or not team_counts:
        raise ValueError(f"{path} must contain a non-empty JSON object mapping division to team count")
    forecast = copy.deepcopy(config)
    configured = forecast["team_counts"]
    if not isinstance(configured, dict):
        raise ValueError("config team_counts must be an object")
    for division, count in team_counts.items():
        if division not in configured:
            raise ValueError(f"{path} contains unknown division {division!r}")
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{path} team count for {division!r} must be a positive integer")
        configured[division] = count
    return forecast


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build deterministic volleyball teams from CSV exports.")
    parser.add_argument("--division", required=True, help="One division, a comma-separated list, or 'all'. Practice slots are assigned across all selected divisions together.")
    parser.add_argument("--input-dir", type=Path, help="Defaults to ../otto-data.")
    parser.add_argument("--players-file", type=Path, help="Defaults to ../otto-data/updated-players.csv.")
    parser.add_argument("--output-dir", type=Path, help="Defaults to ../otto-data/runs/<division>. Single division only.")
    parser.add_argument("--config", type=Path, default=Path("config/2026.json"))
    parser.add_argument("--overrides", type=Path, help="Defaults to ../otto-data/2026-<division>-overrides.csv. Single division only.")
    parser.add_argument("--forecast-name", help="Name for an isolated Select forecast under runs/forecasts/.")
    parser.add_argument("--forecast-select", type=Path, help="Private CSV with one projected Select player reg_number per row.")
    parser.add_argument("--forecast-team-counts", type=Path, help="JSON object of scenario-only division-to-team-count overrides.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    forecast_requested = bool(args.forecast_name or args.forecast_select or args.forecast_team_counts)
    if forecast_requested:
        if not args.forecast_name:
            parser.error("--forecast-name is required with --forecast-select or --forecast-team-counts")
        if Path(args.forecast_name).name != args.forecast_name or args.forecast_name in {".", ".."}:
            parser.error("--forecast-name must be a single directory name")
        if args.output_dir:
            parser.error("--output-dir cannot be used for forecasts; forecast outputs are isolated automatically")
        if not args.forecast_select:
            parser.error("--forecast-select is required for a forecast")
        if args.forecast_team_counts:
            try:
                config = apply_forecast_team_counts(config, args.forecast_team_counts)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                parser.error(str(error))
    configured = list(config["team_counts"])
    requested = configured if args.division.strip().lower() == "all" else [d.strip() for d in args.division.split(",") if d.strip()]
    requested = list(dict.fromkeys(requested))
    for division in requested:
        if division not in configured: parser.error(f"Unknown division {division!r}; configured: {', '.join(configured)}")
    if not requested: parser.error("No division selected")
    if len(requested) > 1 and (args.output_dir or args.overrides):
        parser.error("--output-dir and --overrides apply to a single division only")
    data_dir = args.input_dir or Path("../otto-data")
    players_file = args.players_file or data_dir / "updated-players.csv"
    try:
        projected_select_ids = load_projected_select_ids(args.forecast_select, players_file) if forecast_requested else frozenset()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    runs_dir = ((data_dir / "runs" / "forecasts" / args.forecast_name) if forecast_requested else (data_dir / "runs")).expanduser().resolve()
    pipelines, skipped = [], []
    for division in requested:
        overrides = args.overrides or data_dir / f"2026-{division}-overrides.csv"
        # Confirmed head coaches come from the overrides file, so a division without one is not run.
        if not overrides.is_file():
            skipped.append({"division": division, "expected_overrides_file": str(overrides)})
            continue
        output_dir = (args.output_dir or runs_dir / division).expanduser().resolve()
        pipelines.append(Pipeline(division, data_dir, output_dir, config, overrides, players_file, projected_select_ids))
    if not pipelines:
        parser.error("No overrides file found for: " + ", ".join(s["division"] + " (" + s["expected_overrides_file"] + ")" for s in skipped))
    try:
        results = run_divisions(pipelines)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    for pl in pipelines:
        result = results[pl.division]
        no_friend_request_met = players_with_no_friend_requests_met_count(pl.request_outcomes)
        print(
            f"Built {result['teams']} {pl.division} teams for {result['players']} players; "
            f"unassigned: {result['unassigned']}; friend requesters with none met: "
            f"{no_friend_request_met}; practices: {practice_day_summary(pl.teams)}"
        )
    if len(pipelines) > 1:
        no_friend_request_met_total = sum(
            players_with_no_friend_requests_met_count(pl.request_outcomes)
            for pl in pipelines
        )
        print(
            "Friend requesters with none met across completed grades: "
            f"{no_friend_request_met_total}"
        )
    for row in skipped:
        print(f"Skipped {row['division']}: no overrides file at {row['expected_overrides_file']}")
    if len(requested) > 1:
        runs_dir.mkdir(parents=True, exist_ok=True)
        write_csv(runs_dir / "skipped_divisions.csv", ["division", "expected_overrides_file"], skipped)
        rows = sorted(({"slot_id": t["slot_id"], "day": t["day"], "time": t["time"], "location": t["location"], "division": pl.division, "team_id": t["team_id"]} for pl in pipelines for t in pl.teams if t["slot_id"]), key=lambda r: r["slot_id"])
        write_csv(runs_dir / "all_team_slots.csv", ["slot_id", "day", "time", "location", "division", "team_id"], rows)
        assigned_rows = assigned_practice_slot_rows(
            [team for pl in pipelines for team in pl.teams],
            {
                coach_id: coach
                for pl in pipelines
                for coach_id, coach in pl.coach_by_id.items()
            },
        )
        write_csv(
            runs_dir / "all_assigned_practice_slots.csv",
            ASSIGNED_PRACTICE_SLOT_FIELDS,
            assigned_rows,
        )
    return 0
